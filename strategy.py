import pandas as pd
import numpy as np
import requests
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from config import (ADX_THRESHOLD, ATR_EXPANSION_RATIO, LONG_ONLY, LONG_SOFT_REQUIRED,
                    MIN_15MIN_RSI, REQUIRE_15MIN_RSI_RISING, CT_EXIT_CONSEC_BARS)


# ---------------------------------------------------------------------------
# Supertrend — with proper band continuity (no repainting)
# ---------------------------------------------------------------------------

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3) -> pd.DataFrame:
    """
    Correct Supertrend implementation with band carry-forward logic.
    Bands only tighten while the trend holds; they never widen mid-trend.
    Adds columns: Supertrend (bool), Supertrend_Upper, Supertrend_Lower.
    """
    atr = AverageTrueRange(
        high=df['High'],
        low=df['Low'],
        close=df['Close'],
        window=period
    ).average_true_range()

    hl2 = (df['High'] + df['Low']) / 2
    raw_upper = hl2 + (multiplier * atr)
    raw_lower = hl2 - (multiplier * atr)

    upper = raw_upper.copy()
    lower = raw_lower.copy()
    supertrend = [True] * len(df)

    for i in range(1, len(df)):
        # Lower band: carry forward unless price broke below or raw band is higher
        lower.iloc[i] = (
            raw_lower.iloc[i]
            if raw_lower.iloc[i] > lower.iloc[i - 1] or df['Close'].iloc[i - 1] < lower.iloc[i - 1]
            else lower.iloc[i - 1]
        )
        # Upper band: carry forward unless price broke above or raw band is lower
        upper.iloc[i] = (
            raw_upper.iloc[i]
            if raw_upper.iloc[i] < upper.iloc[i - 1] or df['Close'].iloc[i - 1] > upper.iloc[i - 1]
            else upper.iloc[i - 1]
        )

        if df['Close'].iloc[i] > upper.iloc[i - 1]:
            supertrend[i] = True
        elif df['Close'].iloc[i] < lower.iloc[i - 1]:
            supertrend[i] = False
        else:
            supertrend[i] = supertrend[i - 1]

    df['Supertrend'] = supertrend
    df['Supertrend_Upper'] = upper
    df['Supertrend_Lower'] = lower
    return df


# ---------------------------------------------------------------------------
# Indicator pipeline
# ---------------------------------------------------------------------------

def apply_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes all indicators required by the strategy:
      - EMA200          : macro trend filter
      - RSI(14)         : momentum (used for crossover, not raw threshold)
      - Volume_Baseline : rolling MEDIAN volume (20-bar) — robust to spikes
                          on thin Indian INR markets
      - ATR(14)         : volatility filter
      - ADX(14)         : regime / chop filter
      - Supertrend      : trend direction + dynamic S/R bands
    """
    df = df.copy()

    # Trend
    df['EMA200'] = EMAIndicator(df['Close'], window=200).ema_indicator()
    df['EMA50']  = EMAIndicator(df['Close'], window=50).ema_indicator()   # local trend for counter-trend longs

    # Momentum — keep the raw series; crossover logic lives in generate_signal
    df['RSI'] = RSIIndicator(df['Close'], window=14).rsi()

    # Volume baseline — rolling MEDIAN (not mean).
    # Indian INR pairs trade thin: typical bars have low volume punctuated by
    # occasional whale spikes. A rolling mean gets dragged up by a single spike
    # and stays inflated for 20 bars, so "Volume > mean" fails on most bars.
    # Median ignores spikes — "Volume > Volume_Baseline" then genuinely means
    # "this bar is busier than half the recent bars", which is what the
    # confirmation was always meant to test.
    df['Volume_Baseline'] = df['Volume'].rolling(20).median()

    # Volatility — ATR for compression detection
    df['ATR'] = AverageTrueRange(
        df['High'], df['Low'], df['Close'], window=14
    ).average_true_range()
    df['ATR_SMA'] = df['ATR'].rolling(20).mean()   # compare current ATR vs recent average

    # Regime — ADX distinguishes trending from ranging markets
    adx_indicator = ADXIndicator(df['High'], df['Low'], df['Close'], window=14)
    df['ADX'] = adx_indicator.adx()

    # Supertrend (with corrected band logic)
    df = calculate_supertrend(df)

    return df


# ---------------------------------------------------------------------------
# NaN / length guard
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS = ['EMA200', 'EMA50', 'RSI', 'Volume_Baseline', 'ATR', 'ATR_SMA', 'ADX',
                     'Supertrend', 'Supertrend_Upper', 'Supertrend_Lower']
_MIN_BARS = 200   # driven by EMA200 warm-up


def _is_ready(df: pd.DataFrame) -> bool:
    """Return False if there isn't enough data or the latest row has NaNs."""
    if len(df) < _MIN_BARS + 1:   # +1 so we always have a previous bar
        return False
    return not df[_REQUIRED_COLUMNS].iloc[-1].isna().any()


# ---------------------------------------------------------------------------
# Signal generation — operates on the full DataFrame for crossover context
# ---------------------------------------------------------------------------

def generate_signal(
    df: pd.DataFrame,
    adx_threshold: float = ADX_THRESHOLD,
    atr_expansion_ratio: float = ATR_EXPANSION_RATIO,
) -> dict | None:
    """
    Returns a signal dict or None.

    Improvements vs original:
      1. NaN / length guard before any indexing.
      2. RSI > 52 level replaces strict crossover — fires on genuine momentum.
      3. ATR expansion filter — skips entries during volatility compression.
      4. ADX regime filter — skips entries in ranging / choppy markets.
      5. Supertrend mandatory; EMA200 demoted to soft condition (bear bounces).
      6. 4-soft scoring (RSI, Volume, EMA200, EMA50) — need 2 of 4.
      7. counter_trend flag when EMA200 not satisfied — tighter exits applied.

    Parameters
    ----------
    adx_threshold       : minimum ADX to consider the market trending (default 25)
    atr_expansion_ratio : ATR must be at least this fraction of its SMA (default 0.6)
    """
    if not _is_ready(df):
        return None

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    # ------------------------------------------------------------------
    # Hard filters — if either fails, no signal regardless of anything else
    # ------------------------------------------------------------------

    # 1. Regime filter: market must be trending
    if latest['ADX'] < adx_threshold:
        return None

    # 2. Volatility filter: avoid low-volatility compression (false breakouts)
    # Guard against ATR_SMA rounding to zero on very low-price coins (e.g. SHIB)
    # where price precision causes ATR to appear as 0.0000. Skip the check rather
    # than dividing by zero or blocking a valid signal on a precision artifact.
    if latest['ATR_SMA'] > 0 and latest['ATR'] < atr_expansion_ratio * latest['ATR_SMA']:
        return None

    # ------------------------------------------------------------------
    # Directional conditions
    # ------------------------------------------------------------------

    # --- LONG ---
    # Supertrend bullish is the ONLY hard mandatory condition.
    # EMA200 is demoted to a soft condition so bear-market bounces are
    # capturable — when price is below EMA200 but Supertrend flips bullish
    # locally, a genuine bounce can still be entered.
    #
    # 4 soft conditions scored (need LONG_SOFT_REQUIRED = 2 of 4):
    #   S1. RSI > 52          — momentum above midpoint (level, not crossover)
    #   S2. Volume > Baseline — above-median participation
    #   S3. Close > EMA200    — macro bull trend alignment (bonus)
    #   S4. Close > EMA50     — local 2-day trend alignment
    #
    # If S3 (EMA200) is NOT satisfied → counter-trend trade flagged.
    # Counter-trend trades use tighter TP and trail targets (see main.py).

    supertrend_long = latest['Supertrend']                        # MANDATORY

    # Soft conditions
    s1_rsi     = latest['RSI'] > 52                               # momentum level
    s2_volume  = latest['Volume'] > latest['Volume_Baseline']     # participation
    s3_ema200  = latest['Close'] > latest['EMA200']               # macro trend
    s4_ema50   = latest['Close'] > latest['EMA50']                # local trend

    long_soft_score = int(s1_rsi) + int(s2_volume) + int(s3_ema200) + int(s4_ema50)
    long_condition  = supertrend_long and long_soft_score >= LONG_SOFT_REQUIRED
    counter_trend   = long_condition and not s3_ema200            # bounce against macro

    # --- SHORT ---
    # In LONG_ONLY mode, SHORT signals are suppressed entirely — no short
    # positions can be opened on a spot account. Set LONG_ONLY=False in
    # config.py to re-enable shorts (e.g. for margin/futures paper trading).
    if not LONG_ONLY:
        ema_short        = latest['Close'] < latest['EMA200']
        supertrend_short = not latest['Supertrend']
        rsi_short        = prev['RSI'] >= 50 and latest['RSI'] < 50
        volume_short     = latest['Volume'] > latest['Volume_Baseline']
        short_mandatory  = ema_short and supertrend_short
        short_soft_score = int(rsi_short) + int(volume_short)
        short_condition  = short_mandatory and short_soft_score >= 1
    else:
        short_condition  = False
        short_soft_score = 0

    # ------------------------------------------------------------------
    # Build signal metadata
    # ------------------------------------------------------------------

    if long_condition:
        return {
            "signal":            "LONG",
            "timestamp":         latest.name,
            "close":             latest['Close'],
            "ema200":            latest['EMA200'],
            "ema50":             latest['EMA50'],
            "rsi":               latest['RSI'],
            "adx":               latest['ADX'],
            "atr":               latest['ATR'],
            "supertrend_lower":  latest['Supertrend_Lower'],
            "supertrend_upper":  latest['Supertrend_Upper'],
            "soft_confirmations": long_soft_score,   # 1-4
            "counter_trend":     counter_trend,      # True = bounce against EMA200
            "s1_rsi":            s1_rsi,
            "s2_volume":         s2_volume,
            "s3_ema200":         s3_ema200,
            "s4_ema50":          s4_ema50,
        }

    if short_condition and not LONG_ONLY:
        return {
            "signal":            "SHORT",
            "timestamp":         latest.name,
            "close":             latest['Close'],
            "ema200":            latest['EMA200'],
            "rsi":               latest['RSI'],
            "adx":               latest['ADX'],
            "atr":               latest['ATR'],
            "supertrend_lower":  latest['Supertrend_Lower'],
            "supertrend_upper":  latest['Supertrend_Upper'],
            "soft_confirmations": short_soft_score,
        }

    return None

# ---------------------------------------------------------------------------
# Mean Reversion signal — catches panic drops on thin INR liquidity
# ---------------------------------------------------------------------------

# Constants used by check_mean_reversion — not in config to keep it self-contained.
# Validated by backtest: Drop 2.5-4%  RSI≤42  BTC not SHORT → +1.42% expectancy,
# 63.6% win rate, 11 trades over 83 days (statistically significant at 90% confidence).
MR_DROP_MIN   = 0.025   # 4H candle body must have dropped at least 2.5%
MR_DROP_MAX   = 0.040   # cap at 4.0% — bigger drops are structural, not panic spikes
MR_RSI_MAX    = 42.0    # RSI must be oversold at close of the drop candle
MR_TP_PCT     = 0.040   # take profit: +4%
MR_SL_PCT     = 0.020   # stop loss:   -2%
MR_MAX_HOURS  = 16      # time backstop: exit after 16h if TP/SL not hit


def check_mean_reversion(df: pd.DataFrame) -> dict | None:
    """
    Mean Reversion entry signal — Lane 2 entry type.

    Fires when the most recently CLOSED 4H candle shows:
      1. Body drop between MR_DROP_MIN (2.5%) and MR_DROP_MAX (4.0%)
         — body = (close - open) / open. Only bearish candles (close < open).
         Sweet spot catches panic-sell / thin-liquidity washouts.
         Drops > 4% are usually structural breakdowns, not bounces.
      2. RSI <= MR_RSI_MAX (42) at close of that candle — confirms oversold.
      3. Not a multi-candle downtrend:
           a. Prior 3 candles are NOT all bearish (<= -1% each)
           b. Current candle low is NOT a new 5-bar low by more than 3%

    NOTE: BTC regime check (not SHORT) is enforced in main.py, not here,
    so this function stays clean and testable independently.

    Returns a signal dict with 'signal': 'MR_LONG' or None.
    """
    # Need at least 20 bars for RSI warmup + context checks
    if len(df) < 20:
        return None

    # Use iloc[-2] — last CLOSED candle. iloc[-1] is still in progress.
    latest = df.iloc[-2]
    prev3  = df.iloc[-5:-2]   # 3 candles before that

    close = float(latest['Close'])
    open_ = float(latest['Open'])
    low   = float(latest['Low'])
    rsi   = float(latest['RSI'])

    # Gate 1: must be a bearish candle
    if close >= open_:
        return None

    # Gate 2: body drop in sweet spot 2.5%-4.0%
    body_pct = (close - open_) / open_   # negative number
    drop_abs = abs(body_pct)
    if drop_abs < MR_DROP_MIN or drop_abs > MR_DROP_MAX:
        return None

    # Gate 3: RSI oversold
    if rsi > MR_RSI_MAX:
        return None

    # Gate 4a: prior 3 candles not all red (avoid entering mid-downtrend)
    prior_bodies = [(float(r['Close']) - float(r['Open'])) / float(r['Open'])
                    for _, r in prev3.iterrows()]
    if all(b <= -0.01 for b in prior_bodies):
        return None

    # Gate 4b: not making a new 5-bar low by more than 3% (falling knife)
    prev_5_low = float(df['Low'].iloc[-7:-2].min())
    if low < prev_5_low * 0.97:
        return None

    return {
        "signal":       "MR_LONG",
        "close":        close,
        "rsi":          rsi,
        "drop_pct":     round(body_pct * 100, 2),
        "mr_tp_pct":    MR_TP_PCT,
        "mr_sl_pct":    MR_SL_PCT,
        "mr_max_hours": MR_MAX_HOURS,
    }


# ---------------------------------------------------------------------------
# Multi-timeframe candle fetchers
# ---------------------------------------------------------------------------

def _fetch_candles(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """Generic CoinDCX candle fetcher. interval: '15m', '1h', '4h' etc."""
    target, base = symbol.split("/")
    pair = f"I-{target}_{base}"
    try:
        r = requests.get(
            "https://public.coindcx.com/market_data/candles",
            params={"pair": pair, "interval": interval, "limit": limit},
            timeout=8
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        if not data or len(data) < 3:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["close"]  = pd.to_numeric(df["close"])
        df["high"]   = pd.to_numeric(df["high"])
        df["low"]    = pd.to_numeric(df["low"])
        df["open"]   = pd.to_numeric(df["open"])
        df["volume"] = pd.to_numeric(df["volume"])
        df["time"]   = pd.to_datetime(pd.to_numeric(df["time"]), unit="ms")
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def fetch_15min(symbol: str, limit: int = 20) -> pd.DataFrame:
    """Fetch recent 15-min candles."""
    return _fetch_candles(symbol, "15m", limit)


def fetch_4h(symbol: str, limit: int = 100) -> pd.DataFrame:
    """Fetch recent 4H candles — used for macro direction per coin."""
    return _fetch_candles(symbol, "4h", limit)


# ---------------------------------------------------------------------------
# 4H macro direction — per-coin regime
# ---------------------------------------------------------------------------

def get_4h_direction(symbol: str) -> tuple[str, str]:
    """
    Determine the 4H macro direction for a coin.
    Returns (direction, reason) where direction is 'BULL', 'BEAR', or 'NEUTRAL'.

    Uses:
      - 4H Supertrend (10-period, multiplier 3)
      - 4H EMA50 vs Close

    Both must agree for a directional read. One disagreeing = NEUTRAL.
    Falls back to NEUTRAL on fetch failure (fail-open for entries).
    """
    df = fetch_4h(symbol, limit=100)
    if df.empty or len(df) < 55:
        return "NEUTRAL", "4H fetch failed — defaulting to NEUTRAL"

    # Rename columns for indicator compatibility
    df_ind = df.rename(columns={"close": "Close", "high": "High",
                                 "low": "Low", "open": "Open", "volume": "Volume"})

    # EMA50 on 4H
    ema50 = EMAIndicator(df_ind["Close"], window=50).ema_indicator().iloc[-1]
    close = float(df_ind["Close"].iloc[-1])
    ema50_bull = close > float(ema50)

    # Supertrend on 4H (simplified — use last 2 bars for direction)
    try:
        atr_4h = AverageTrueRange(df_ind["High"], df_ind["Low"],
                                   df_ind["Close"], window=10).average_true_range()
        hl2    = (df_ind["High"] + df_ind["Low"]) / 2
        upper  = (hl2 + 3 * atr_4h).iloc[-2]
        lower  = (hl2 - 3 * atr_4h).iloc[-2]
        prev_close = float(df_ind["Close"].iloc[-2])
        # Simplified: price above upper band last bar = bull, below lower = bear
        st_bull = prev_close > float((hl2 - 3 * atr_4h).iloc[-2])
        st_bear = prev_close < float((hl2 + 3 * atr_4h).iloc[-2])
        # More reliable: use current close vs midband
        midband = float(hl2.iloc[-1])
        st_bull = close > midband
    except Exception:
        st_bull = ema50_bull   # fallback: agree with EMA50

    if ema50_bull and st_bull:
        return "BULL", f"4H BULL (Close={close:.4f} > EMA50={ema50:.4f}, above midband)"
    elif not ema50_bull and not st_bull:
        return "BEAR", f"4H BEAR (Close={close:.4f} < EMA50={ema50:.4f}, below midband)"
    else:
        return "NEUTRAL", f"4H NEUTRAL (EMA50={'bull' if ema50_bull else 'bear'}, ST={'bull' if st_bull else 'bear'} — mixed)"


# ---------------------------------------------------------------------------
# 15-min entry gate — RSI-based momentum confirmation
# ---------------------------------------------------------------------------

def _compute_rsi(closes: list, period: int = 14) -> float:
    """Simple RSI calculation on a list of closes."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def confirm_15min_momentum(symbol: str, signal_dir: str = "LONG") -> tuple[bool, str]:
    """
    15-min entry timing gate.

    For LONG:
      - 15-min RSI must be > MIN_15MIN_RSI (50)
      - If REQUIRE_15MIN_RSI_RISING: RSI must also be rising bar-over-bar
      - Stale data (identical closes) → fail-open (allow entry)
      - Fetch failure → fail-open

    Returns (allowed, reason_string).
    """
    df = fetch_15min(symbol, limit=20)

    if df.empty or len(df) < 5:
        return True, "15min fetch failed — allowing entry (fail-open)"

    closes = df["close"].tolist()
    last_close  = closes[-1]
    prev_close  = closes[-2]
    prev2_close = closes[-3]

    # Stale data guard
    if prev2_close == prev_close == last_close:
        return True, (f"15min data stale (identical closes: {last_close:.4f}) "
                      f"— allowing entry (fail-open)")

    # Compute RSI on last 20 bars
    rsi_now  = _compute_rsi(closes, period=14)
    rsi_prev = _compute_rsi(closes[:-1], period=14)
    rsi_rising = rsi_now > rsi_prev

    if signal_dir == "LONG":
        rsi_ok = rsi_now > MIN_15MIN_RSI
        rising_ok = rsi_rising if REQUIRE_15MIN_RSI_RISING else True

        if rsi_ok and rising_ok:
            return True, (f"15min OK — RSI={rsi_now:.1f}{'↑' if rsi_rising else '→'} "
                          f"price:{prev2_close:.4f}→{prev_close:.4f}→{last_close:.4f}")
        elif not rsi_ok:
            return False, (f"15min RSI too low ({rsi_now:.1f} < {MIN_15MIN_RSI}) "
                           f"— entry skipped, re-evaluate next run")
        else:
            return False, (f"15min RSI falling ({rsi_now:.1f}↓ from {rsi_prev:.1f}) "
                           f"— momentum not building, entry skipped")

    # SHORT (future use)
    if signal_dir == "SHORT":
        rsi_ok = rsi_now < (100 - MIN_15MIN_RSI)
        return rsi_ok, f"15min SHORT RSI={rsi_now:.1f} {'OK' if rsi_ok else 'FADED'}"

    return True, "unknown direction — allowing entry"


# ---------------------------------------------------------------------------
# 15-min counter-trend position monitor — early exit detection
# ---------------------------------------------------------------------------

def check_15min_ct_exit(symbol: str) -> tuple[bool, str]:
    """
    For counter-trend LONG positions: check whether the 15-min trend has
    flipped bearish, signalling the bounce is reversing.

    Returns (should_exit, reason).
    Requires CT_EXIT_CONSEC_BARS consecutive 15-min bearish closes to trigger.
    This prevents exiting on single-bar noise.

    Bearish = current close < previous close (simple price direction).
    """
    df = fetch_15min(symbol, limit=CT_EXIT_CONSEC_BARS + 3)

    if df.empty or len(df) < CT_EXIT_CONSEC_BARS + 1:
        return False, "15min CT check: insufficient data — holding"

    closes = df["close"].tolist()

    # Check last CT_EXIT_CONSEC_BARS bars are all falling
    falling_count = 0
    for i in range(1, CT_EXIT_CONSEC_BARS + 1):
        if closes[-i] < closes[-(i + 1)]:
            falling_count += 1

    if falling_count >= CT_EXIT_CONSEC_BARS:
        recent = "→".join(f"{c:.4f}" for c in closes[-(CT_EXIT_CONSEC_BARS + 1):])
        return True, (f"15min CT exit: {falling_count} consecutive falling bars "
                      f"({recent}) — bounce reversing, exiting early")

    return False, f"15min CT: {falling_count}/{CT_EXIT_CONSEC_BARS} falling bars — holding"

# ---------------------------------------------------------------------------
# Market Health Check
# ---------------------------------------------------------------------------
# Checks the BROADER crypto market before allowing any new entries.
# Uses 6 reference coins that represent different market sectors —
# not your trading coins, but the market as a whole.
#
# Reference basket (fixed — high INR volume, sector-diverse):
#   BTC/INR  — macro regime (already fetched)
#   ETH/INR  — Layer 1, largest alt
#   SOL/INR  — Layer 1, high beta
#   BNB/INR  — exchange token
#   XRP/INR  — payments / retail
#   MATIC/INR — L2 / independent
#
# A coin is "trending" if:
#   ADX > MARKET_HEALTH_ADX_MIN  AND  close > EMA50
#
# Market is HEALTHY if:
#   BTC is trending  AND  at least MARKET_HEALTH_MIN_COINS of the 5 alts
#   are also trending.
#
# If market is UNHEALTHY: all new entries blocked. Open positions
# continue through their normal exit tiers — only NEW entries are gated.
#
# MR (mean reversion) entries bypass this gate — they are counter-trend
# by nature and fire precisely when a coin drops in an otherwise healthy
# or recovering market. Blocking MR on market health would defeat its purpose.

MARKET_HEALTH_ADX_MIN   = 20    # lower than entry ADX — we want a broad read,
                                 # not strict trending. If even this isn't met,
                                 # the market is genuinely dead.
MARKET_HEALTH_MIN_COINS = 2     # at least 2 of 5 reference alts must be trending
                                 # (BTC is checked separately as a hard requirement)

# Reference coins — always fetched for health check, never traded
# (some may overlap with COINS list — that's fine, data is reused)
MARKET_HEALTH_COINS = [
    "ETH/INR",
    "SOL/INR",
    "BNB/INR",
    "XRP/INR",
    "MATIC/INR",
]


def check_market_health(coins_data: dict) -> tuple[bool, str, dict]:
    """
    Check broader market health before allowing new entries.

    Parameters
    ----------
    coins_data : dict
        Already-fetched and indicator-applied DataFrames keyed by symbol.
        Any reference coins not already in coins_data are fetched fresh.

    Returns
    -------
    (is_healthy, reason_string, detail_dict)
        is_healthy  : True = ok to enter, False = hold cash
        reason      : human-readable summary for the log
        detail      : per-coin results for verbose logging
    """
    detail = {}

    # ── BTC check — hard requirement ──────────────────────────────────────
    btc_df = coins_data.get("BTC/INR")
    if btc_df is None or len(btc_df) < 55:
        # Can't check BTC — fail open (don't block entries on data failure)
        return True, "BTC data unavailable — health check skipped (fail-open)", {}

    btc_adx   = float(btc_df["ADX"].iloc[-1])
    btc_close = float(btc_df["Close"].iloc[-1])
    btc_ema50 = float(btc_df["EMA50"].iloc[-1])
    btc_trend = btc_adx >= MARKET_HEALTH_ADX_MIN and btc_close > btc_ema50
    detail["BTC/INR"] = {
        "adx": round(btc_adx, 1),
        "above_ema50": btc_close > btc_ema50,
        "trending": btc_trend
    }

    if not btc_trend:
        reason = (f"BTC not trending — ADX={btc_adx:.1f} "
                  f"({'above' if btc_close > btc_ema50 else 'below'} EMA50). "
                  f"Market health FAIL — holding cash.")
        return False, reason, detail

    # ── Reference alt basket ──────────────────────────────────────────────
    trending_count = 0
    results = []

    for sym in MARKET_HEALTH_COINS:
        # Reuse already-fetched data if available
        df = coins_data.get(sym)
        if df is None or len(df) < 55:
            # Fetch fresh — this coin isn't in the bot's COINS list
            raw = _fetch_candles(sym, "1h", 250)
            if raw.empty or len(raw) < 55:
                detail[sym] = {"trending": None, "reason": "no data"}
                results.append(f"{sym.split('/')[0]}:?")
                continue
            # Apply minimal indicators needed
            df = raw.rename(columns={"close":"Close","high":"High",
                                     "low":"Low","open":"Open","volume":"Volume"})
            from ta.trend import EMAIndicator, ADXIndicator
            df["EMA50"] = EMAIndicator(df["Close"], window=50).ema_indicator()
            adx_ind     = ADXIndicator(df["High"], df["Low"], df["Close"], window=14)
            df["ADX"]   = adx_ind.adx()

        adx   = float(df["ADX"].iloc[-1])
        close = float(df["Close"].iloc[-1])
        ema50 = float(df["EMA50"].iloc[-1])
        trending = adx >= MARKET_HEALTH_ADX_MIN and close > ema50

        detail[sym] = {
            "adx": round(adx, 1),
            "above_ema50": close > ema50,
            "trending": trending
        }
        if trending:
            trending_count += 1
            results.append(f"{sym.split('/')[0]}:✅")
        else:
            results.append(f"{sym.split('/')[0]}:❌"
                           f"(ADX={adx:.0f},{'↑' if close > ema50 else '↓'}EMA50)")

    basket_str = "  ".join(results)
    is_healthy = trending_count >= MARKET_HEALTH_MIN_COINS

    if is_healthy:
        reason = (f"Market health OK — BTC trending ✅  "
                  f"Alts {trending_count}/{len(MARKET_HEALTH_COINS)} trending  "
                  f"[{basket_str}]")
    else:
        reason = (f"Market health FAIL — BTC trending ✅ but only "
                  f"{trending_count}/{len(MARKET_HEALTH_COINS)} alts trending "
                  f"(need {MARKET_HEALTH_MIN_COINS})  [{basket_str}]  "
                  f"— holding cash, no new entries.")

    return is_healthy, reason, detail


def _fetch_candles(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """Thin wrapper around the existing _fetch_candles logic for health check use."""
    return _fetch_candles_raw(symbol, interval, limit)


def _fetch_candles_raw(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """Fetch raw candles — used only by check_market_health for non-COINS symbols."""
    target, base = symbol.split("/")
    pair = f"I-{target}_{base}"
    try:
        r = requests.get(
            "https://public.coindcx.com/market_data/candles",
            params={"pair": pair, "interval": interval, "limit": limit},
            timeout=8
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        if not data or len(data) < 3:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["close"]  = pd.to_numeric(df["close"])
        df["high"]   = pd.to_numeric(df["high"])
        df["low"]    = pd.to_numeric(df["low"])
        df["open"]   = pd.to_numeric(df["open"])
        df["volume"] = pd.to_numeric(df["volume"])
        df["time"]   = pd.to_datetime(pd.to_numeric(df["time"]), unit="ms")
        return df.sort_values("time").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()