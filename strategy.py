import pandas as pd
import numpy as np
import requests
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from config import ADX_THRESHOLD, ATR_EXPANSION_RATIO, LONG_ONLY, LONG_SOFT_REQUIRED


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
# 15-minute momentum confirmation — entry timing gate
# ---------------------------------------------------------------------------
# Problem: 1H signals fire when all conditions align on the CLOSED 1H bar.
# By then, the move that triggered the signal may have already peaked.
# The bot enters on the next bar — potentially buying at the top of a rally.
#
# Fix: before entering, fetch the last 4 x 15-min candles (= 1 hour of
# intra-bar data) and verify momentum is still alive in the signal direction.
#
# For a LONG signal, momentum is alive if EITHER:
#   - The last 15-min close is ABOVE the previous 15-min close (price rising)
#   - OR the 15-min RSI of the last bar is ABOVE the previous bar (RSI rising)
# Both failing means the move peaked before our entry bar closed.
#
# This is a soft check — if the 15-min fetch fails for any reason (network,
# no data), we allow entry rather than missing a valid trade.

def fetch_15min(symbol: str, limit: int = 6) -> pd.DataFrame:
    """Fetch recent 15-min candles for a symbol from CoinDCX public API."""
    target, base = symbol.split("/")
    pair = f"I-{target}_{base}"
    try:
        r = requests.get(
            "https://public.coindcx.com/market_data/candles",
            params={"pair": pair, "interval": "15m", "limit": limit},
            timeout=8
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        if not data or len(data) < 3:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["close"]  = pd.to_numeric(df["close"])
        df["time"]   = pd.to_datetime(pd.to_numeric(df["time"]), unit="ms")
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def confirm_15min_momentum(symbol: str, signal_dir: str = "LONG") -> tuple[bool, str]:
    """
    Check whether 15-min price momentum still supports the 1H signal direction.

    Returns (allowed, reason_string).
    allowed=True  → momentum still alive, proceed with entry
    allowed=False → momentum faded, skip this bar
    allowed=True  → also returned on fetch failure (fail-open, don't miss trades)
    """
    df = fetch_15min(symbol, limit=6)

    if df.empty or len(df) < 3:
        # Can't fetch — fail open (allow entry, don't punish network issues)
        return True, "15min fetch failed — allowing entry (fail-open)"

    # Compute RSI on the 15-min closes using a simple 14-period window
    # Only meaningful if we have enough bars; otherwise just use price direction
    last_close  = float(df["close"].iloc[-1])
    prev_close  = float(df["close"].iloc[-2])
    prev2_close = float(df["close"].iloc[-3])

    # Stale data guard: if all closes are identical, CoinDCX 15-min feed is
    # frozen (price not updating). Treat as fetch failure — fail open.
    if prev2_close == prev_close == last_close:
        return True, (
            f"15min data stale (identical closes: {last_close:.4f}) "
            f"— allowing entry (fail-open)"
        )

    price_rising     = last_close > prev_close
    price_was_rising = prev_close > prev2_close

    # For LONG: at least one of the last two 15-min bars should show rising price
    # This allows for a one-bar pause without blocking entry
    if signal_dir == "LONG":
        momentum_ok = price_rising or price_was_rising
        if momentum_ok:
            direction = "↑" if price_rising else "→↑"
            return True, (
                f"15min momentum OK ({direction} "
                f"{prev2_close:.4f}→{prev_close:.4f}→{last_close:.4f})"
            )
        else:
            return False, (
                f"15min momentum FADED — price falling for 2+ bars "
                f"({prev2_close:.4f}→{prev_close:.4f}→{last_close:.4f}). "
                f"Entry skipped, re-evaluate next hour."
            )

    # For SHORT (future use): price should be falling
    if signal_dir == "SHORT":
        momentum_ok = (last_close < prev_close) or (prev_close < prev2_close)
        return momentum_ok, f"15min SHORT momentum {'OK' if momentum_ok else 'FADED'}"

    return True, "unknown direction — allowing entry"