import os
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone

from config import (
    TRADING_MODE,
    COINS, DATA_DIR, INITIAL_CAPITAL, RISK_PER_TRADE, STOP_LOSS_PCT,
    RISK_PER_TRADE_HIGH, RISK_PER_TRADE_LOW,
    MAX_ALLOCATION, MAX_POSITIONS_PER_DIR, MAX_POSITIONS_BULL, MAX_POSITIONS_BEAR,
    PARTIAL_TAKE_PROFIT_PCT, PARTIAL_EXIT_RATIO,
    PARTIAL_TP_1A_PCT, PARTIAL_TP_1B_PCT,
    PARTIAL_EXIT_RATIO_1A, PARTIAL_EXIT_RATIO_1B,
    TRAILING_STOP_PCT,
    COUNTER_TREND_TP_PCT, COUNTER_TREND_TRAIL_PCT,
    TIME_EXIT_STAGNANT_HOURS, TIME_EXIT_STAGNANT_HOURS_BULL,
    TIME_EXIT_LOSING_HOURS, TIME_EXIT_LOSING_HOURS_BEAR,
    TIME_EXIT_MIN_MOVE_PCT,
    TIME_EXIT_EXTENDED_HOURS, TIME_EXIT_TRAIL_HOURS,
    RSI_RESET_SHORT, RSI_RESET_LONG,
    LONG_ONLY, REGIME_ALLOWS_LONG_IN_NEUTRAL, REGIME_OVERRIDE_MIN_SCORE,
    CONFIRM_15MIN, USE_4H_REGIME, CT_EXIT_15MIN, VERBOSE_DIAG,
    BULL_TRAILING_STOP_PCT,
    REGIME_OVERRIDE_MAX_CORR, COIN_BTC_CORR,
    SIGNAL_DETERIORATION_EXIT, SIGNAL_EXIT_THRESHOLD,
    LONG_SOFT_REQUIRED, CT_SOFT_REQUIRED,
    REGIME_FLIP_EXIT, REGIME_FLIP_EXIT_CORR,
    CT_BLOCK_CORR_IN_SHORT,
    USE_4H_HARD_GATE,
    ADX_THRESHOLD, ATR_EXPANSION_RATIO,
)
from strategy import (apply_indicators, generate_signal,
                      confirm_15min_momentum, get_4h_direction,
                      check_15min_ct_exit, check_mean_reversion,
                      check_market_health, get_daily_trend)
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Mean Reversion eligible coins
# ---------------------------------------------------------------------------
# Backtested on NEAR/INR, DOT/INR, FIL/INR — these three showed the cleanest
# single-candle panic-spike-and-recover behaviour on CoinDCX INR pairs.
# NEAR is included but has the weakest signal; FIL and DOT are strongest.
# Extend this list only after running backtest_meanrev_v2.py on new coins.

MR_COINS = {"NEAR/INR", "DOT/INR", "FIL/INR"}

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _safe_symbol(symbol: str) -> str:
    """Replace '/' with '_' so 'ETH/INR' → 'ETH_INR' for use in filenames."""
    return symbol.replace("/", "_")


def get_position_file(symbol: str) -> str:
    return f"{DATA_DIR}/{_safe_symbol(symbol)}_position.csv"


def get_trade_file(symbol: str) -> str:
    return f"{DATA_DIR}/{_safe_symbol(symbol)}_trades.csv"


def load_position(symbol: str) -> dict | None:
    file = get_position_file(symbol)
    if os.path.exists(file):
        df = pd.read_csv(file)
        if df.empty:
            os.remove(file)
            return None
        pos = df.iloc[0].to_dict()
        # Stale file guard: if Quantity is missing or zero the position is already closed
        if float(pos.get('Quantity', 0)) <= 0:
            os.remove(file)
            return None
        return pos
    return None


def save_position(symbol: str, position: dict) -> None:
    pd.DataFrame([position]).to_csv(get_position_file(symbol), index=False)


def clear_position(symbol: str) -> None:
    file = get_position_file(symbol)
    if os.path.exists(file):
        os.remove(file)


def log_trade(symbol: str, trade: dict) -> None:
    file = get_trade_file(symbol)
    df   = pd.DataFrame([trade])
    if os.path.exists(file):
        df = pd.concat([pd.read_csv(file), df], ignore_index=True)
    df.to_csv(file, index=False)


def get_last_trade(symbol: str) -> dict | None:
    """Return the most recent FULL-CLOSE trade for this symbol, or None.
    All partial-profit rows are skipped — they are not full position closes."""
    file = get_trade_file(symbol)
    if not os.path.exists(file):
        return None
    df = pd.read_csv(file)
    # Exclude all partial rows — both stages of the two-tier partial
    partial_reasons = {"PARTIAL_PROFIT", "PARTIAL_PROFIT_1A", "PARTIAL_PROFIT_1B"}
    full_closes = df[~df["Exit Reason"].isin(partial_reasons)]
    if full_closes.empty:
        return None
    return full_closes.iloc[-1].to_dict()


# ---------------------------------------------------------------------------
# RSI reset check — re-entry filter after losing OR completed winning trades
# ---------------------------------------------------------------------------
# Two scenarios both require RSI to prove fresh momentum before re-entry:
#
# 1. After a LOSING trade (original behaviour):
#    The signal that just fired was wrong — RSI must reset past 55 (rising)
#    to confirm genuine new momentum, not the same fading move re-firing.
#
# 2. After a COMPLETED WINNING CYCLE (new):
#    When a coin just ran its full cycle (trail stop or trail timeout after
#    partial), the move is likely exhausted. Price is pulling back from the
#    peak. RSI must reset past 55 fresh before re-entry — same requirement
#    as after a loss, but triggered by a winning exit instead.
#    This prevents buying a coin at ₹242 that just peaked at ₹250.
#
#    "Completed winning cycle" = last full-close was TRAILING_STOP or
#    TIME_EXIT_TRAIL_TIMEOUT AND the trade was profitable (PnL > 0).
#    Simple TIME_EXIT_LOSING or TIME_EXIT_STAGNANT exits are already
#    handled by the loss path above.
#
#   LONG reset: RSI must be > RSI_RESET_LONG (55) AND rising bar-over-bar
# ---------------------------------------------------------------------------

_COMPLETED_CYCLE_REASONS = {"TRAILING_STOP", "TIME_EXIT_TRAIL_TIMEOUT"}

def rsi_reset_allows_entry(symbol: str, signal_dir: str, df: pd.DataFrame) -> tuple[bool, str]:
    last = get_last_trade(symbol)
    if last is None:
        return True, ""

    last_side   = str(last.get("Side", ""))
    last_pnl    = float(last.get("PnL", 0))
    last_reason = str(last.get("Exit Reason", ""))

    # Only applies when last full-close was same direction as new signal
    if last_side != signal_dir:
        return True, ""

    latest_rsi = float(df.iloc[-1]["RSI"])
    prev_rsi   = float(df.iloc[-2]["RSI"])
    arrow      = "↓" if latest_rsi < prev_rsi else "↑"

    # Determine if RSI reset is required
    need_reset = False
    reset_reason_label = ""

    if last_pnl < 0:
        # Case 1: last trade was a loss — original behaviour
        need_reset = True
        reset_reason_label = "losing trade"

    elif last_pnl > 0 and last_reason in _COMPLETED_CYCLE_REASONS:
        # Case 2: last trade completed a profitable cycle (trail fired)
        # The move is exhausted — require fresh RSI before re-entry
        need_reset = True
        reset_reason_label = f"completed winning cycle ({last_reason}, +{last_pnl:.2f})"

    if not need_reset:
        return True, ""

    if signal_dir == "SHORT":
        if latest_rsi < RSI_RESET_SHORT and latest_rsi < prev_rsi:
            return True, ""
        return False, (
            f"RSI reset required after {reset_reason_label} "
            f"(need RSI<{RSI_RESET_SHORT} & falling, got RSI={latest_rsi:.1f}{arrow})"
        )

    if signal_dir == "LONG":
        if latest_rsi > RSI_RESET_LONG and latest_rsi > prev_rsi:
            return True, ""
        return False, (
            f"RSI reset required after {reset_reason_label} "
            f"(need RSI>{RSI_RESET_LONG} & rising, got RSI={latest_rsi:.1f}{arrow})"
        )

    return True, ""


# ---------------------------------------------------------------------------
# Correlation guard — count open positions per direction across all coins
# ---------------------------------------------------------------------------

def count_open_by_direction() -> dict:
    """Returns {'LONG': n, 'SHORT': n} for all currently open positions."""
    counts = {"LONG": 0, "SHORT": 0}
    for s in COINS:
        p = load_position(s)
        if p is not None and p['Side'] in counts:
            counts[p['Side']] += 1
    return counts


def effective_max_positions(btc_regime: str | None) -> int:
    """Return regime-aware max open positions per direction."""
    if btc_regime == "LONG":
        return MAX_POSITIONS_BULL
    if btc_regime == "SHORT":
        return MAX_POSITIONS_BEAR
    return MAX_POSITIONS_PER_DIR   # NEUTRAL


# ---------------------------------------------------------------------------
# BTC regime filter
# ---------------------------------------------------------------------------

def get_btc_regime(coins_data: dict) -> tuple[str | None, dict]:
    """
    Returns (regime, breakdown) where regime is 'LONG', 'SHORT', or None.

    INR-denominated BTC candles are ~2x noisier than the USD pairs the strategy
    was originally tuned on, which causes EMA200 and Supertrend to disagree
    constantly on individual bars. A binary AND gate would produce permanent
    NEUTRAL and block all altcoin entries.

    3-indicator majority vote — REGIME_VOTE_THRESHOLD of 3 must agree:
      1. EMA200     : Close vs EMA200
      2. Supertrend : confirmed over REGIME_ST_CONFIRM_BARS consecutive bars
      3. EMA20/50   : fast EMA above slow EMA (short-term momentum)
    """
    from config import REGIME_ST_CONFIRM_BARS, REGIME_VOTE_THRESHOLD

    btc_df = coins_data.get("BTC/INR")
    if btc_df is None or len(btc_df) < REGIME_ST_CONFIRM_BARS + 1:
        return None, {}

    latest = btc_df.iloc[-1]

    required = ['EMA200', 'Supertrend']
    if any(pd.isna(latest.get(col)) for col in required):
        return None, {}

    # --- Indicator 1: EMA200 ---
    ema200_bull = latest['Close'] > latest['EMA200']
    ema200_bear = latest['Close'] < latest['EMA200']

    # --- Indicator 2: Supertrend (REGIME_ST_CONFIRM_BARS confirmation) ---
    confirm_window = btc_df['Supertrend'].iloc[-REGIME_ST_CONFIRM_BARS:]
    st_bull = bool(confirm_window.all())
    st_bear = bool((~confirm_window).all())

    # --- Indicator 3: EMA20 vs EMA50 short-term momentum ---
    ema20 = btc_df['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = btc_df['Close'].ewm(span=50, adjust=False).mean().iloc[-1]
    ema_fast_bull = ema20 > ema50
    ema_fast_bear = ema20 < ema50

    bull_score = int(ema200_bull) + int(st_bull) + int(ema_fast_bull)
    bear_score = int(ema200_bear) + int(st_bear) + int(ema_fast_bear)

    if bull_score >= REGIME_VOTE_THRESHOLD:
        regime = "LONG"
    elif bear_score >= REGIME_VOTE_THRESHOLD:
        regime = "SHORT"
    else:
        regime = None

    breakdown = {
        "close":      latest['Close'],
        "ema200":     latest['EMA200'],
        "ema20":      ema20,
        "ema50":      ema50,
        "v1_bull":    ema200_bull,   "v1_bear": ema200_bear,
        "v2_bull":    st_bull,       "v2_bear": st_bear,
        "v3_bull":    ema_fast_bull, "v3_bear": ema_fast_bear,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "adx":        latest.get('ADX'),
        "rsi":        latest.get('RSI'),
        "atr":        latest.get('ATR'),
        "atr_sma":    latest.get('ATR_SMA'),
        "st_upper":   latest.get('Supertrend_Upper'),
        "st_lower":   latest.get('Supertrend_Lower'),
    }

    return regime, breakdown


# ---------------------------------------------------------------------------
# Data fetching — CoinDCX public market data API
# ---------------------------------------------------------------------------
# Candles come from CoinDCX's public endpoint (no auth required).
# Volume is reported in target currency (e.g. BTC for BTC/INR), with proper
# density on Indian INR markets. Same venue used for paper trading P&L
# simulation and (eventually) live execution.


def _coindcx_pair(symbol: str) -> str:
    """Map 'BTC/INR' → 'I-BTC_INR' (CoinDCX pair format)."""
    target, base = symbol.split("/")
    return f"I-{target}_{base}"


def fetch_data(symbol: str) -> pd.DataFrame:
    """Fetch 1h candles from CoinDCX's public market data API.
    Returns an empty DataFrame on failure — caller must handle that."""
    pair = _coindcx_pair(symbol)
    url = "https://public.coindcx.com/market_data/candles"
    params = {"pair": pair, "interval": "1h", "limit": 1000}  # ~41 days at 1h

    last_exc = None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, params=params, timeout=10)
            break
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < 3:
                import time as _time
                print(f"  CoinDCX request failed (attempt {attempt}/3): {e}. Retrying...")
                _time.sleep(2 ** attempt)
    else:
        print(f"  CoinDCX all retries failed: {last_exc}")
        return pd.DataFrame()

    if response.status_code != 200:
        print(f"  CoinDCX error {response.status_code}: {response.text[:200]}")
        return pd.DataFrame()

    try:
        candles = response.json()
    except Exception as e:
        print(f"  CoinDCX JSON parse failed: {e}")
        return pd.DataFrame()

    if not isinstance(candles, list) or len(candles) == 0:
        print(f"  CoinDCX empty candles for {pair}")
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df.rename(columns={
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "close":  "Close",
        "volume": "Volume",
        "time":   "time",
    }, inplace=True)

    df["time"] = pd.to_datetime(pd.to_numeric(df["time"]), unit="ms")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col])

    df = df.sort_values("time")   # CoinDCX returns descending; we want ascending
    df.set_index("time", inplace=True)
    return df


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

def load_portfolio_state() -> dict:
    portfolio_file = f"{DATA_DIR}/portfolio.csv"
    if os.path.exists(portfolio_file):
        df = pd.read_csv(portfolio_file)
        if len(df) > 0:
            latest = df.iloc[-1]
            return {
                "cash":         float(latest['Cash']),
                "realized_pnl": float(latest['Realized PnL']),
                "total_trades": int(latest['Total Trades'])
            }
    return {"cash": INITIAL_CAPITAL, "realized_pnl": 0.0, "total_trades": 0}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

initialize_portfolio()

# ---------------------------------------------------------------------------
# Live trading startup — reconcile and read real balance if TRADING_MODE=live
# ---------------------------------------------------------------------------
if TRADING_MODE == "live":
    from exchange import (
        get_live_inr_balance, reconcile_positions, run_connectivity_test
    )
    print("=" * 50)
    print(f"TRADING MODE: LIVE — real orders will be placed")
    print("=" * 50)

    # Step 1: Reconcile local position files with actual exchange holdings.
    # This catches any drift caused by manual trades, crashes, or partial fills.
    print("Reconciling positions with CoinDCX account...")
    reconcile_positions(COINS, load_position, save_position, clear_position)

    # Step 2: Read actual INR balance from exchange.
    # In live mode, cash is always the real available INR — never from CSV.
    live_inr = get_live_inr_balance()
    print(f"Live INR balance: ₹{live_inr:.2f}")

    # Load realized P&L and trade count from CSV history (these are our records,
    # not the exchange's — exchange doesn't track these for us)
    state        = load_portfolio_state()
    cash         = live_inr          # OVERRIDE: use real balance, not CSV cash
    realized_pnl = state['realized_pnl']
    total_trades = state['total_trades']

else:
    print(f"TRADING MODE: PAPER — simulating trades, no real orders placed")
    state        = load_portfolio_state()
    cash         = state['cash']
    realized_pnl = state['realized_pnl']
    total_trades = state['total_trades']

trades_this_run = 0
run_events      = []   # human-readable event strings collected each run for portfolio.csv

# ------------------------------------------------------------------
# Step 1: Fetch + apply indicators for ALL coins upfront.
# This allows BTC regime to be read before processing altcoins,
# without a second API call.
# ------------------------------------------------------------------

print("=" * 50)
print("Fetching market data...")
print("=" * 50)

coins_data = {}
for symbol in COINS:
    df = fetch_data(symbol)
    if len(df) < 200:   # EMA200 needs 200 bars of warm-up
        print(f"  Not enough data ({len(df)} bars), skipping.")
        continue
    coins_data[symbol] = apply_indicators(df)
    # Raw volume distribution — sanity-check the data feed.
    v = df['Volume']
    if VERBOSE_DIAG:
        print(f"  [VOL DIAG] {symbol}: min={v.min():.4f}  median={v.median():.4f}  "
              f"max={v.max():.4f}  last={v.iloc[-1]:.4f}")

# ------------------------------------------------------------------
# Step 2: Determine BTC regime — single gate for all altcoin entries.
# ------------------------------------------------------------------

btc_regime, btc_breakdown = get_btc_regime(coins_data)
print(f"\n{'=' * 50}")
print(f"BTC Regime : {btc_regime or 'NEUTRAL (mixed — altcoin entries blocked)'}")
print(f"{'=' * 50}")

if VERBOSE_DIAG and btc_breakdown:
    bd = btc_breakdown
    print(f"[DIAG] BTC/INR Regime Votes:")
    print(f"  Close       : {bd['close']:.2f}")
    print(f"  EMA200      : {bd['ema200']:.2f}  ->  Vote1 BULL={bd['v1_bull']} BEAR={bd['v1_bear']}")
    print(f"  Supertrend  : confirmed  ->  Vote2 BULL={bd['v2_bull']} BEAR={bd['v2_bear']}")
    print(f"  EMA20/EMA50 : {bd['ema20']:.2f} / {bd['ema50']:.2f}  ->  Vote3 BULL={bd['v3_bull']} BEAR={bd['v3_bear']}")
    print(f"  Bull Score  : {bd['bull_score']}/3  |  Bear Score : {bd['bear_score']}/3  ->  Regime={btc_regime or 'NEUTRAL'}")
    print(f"  ADX         : {bd['adx']:.2f}  |  RSI={bd['rsi']:.2f}  |  ATR={bd['atr']:.2f}  |  ATR_SMA={bd['atr_sma']:.2f}")
    print(f"  ST_Upper    : {bd['st_upper']:.2f}  |  ST_Lower={bd['st_lower']:.2f}")
print()

# ---------------------------------------------------------------------------
# Regime flip detection — emergency exit on LONG → SHORT transition
# ---------------------------------------------------------------------------
_regime_prev_file = f"{DATA_DIR}/btc_regime_prev.txt"

def _load_prev_regime() -> str | None:
    if os.path.exists(_regime_prev_file):
        with open(_regime_prev_file) as f:
            val = f.read().strip()
            return val if val in ("LONG", "SHORT") else None
    return None

def _save_prev_regime(regime: str | None) -> None:
    with open(_regime_prev_file, "w") as f:
        f.write(regime or "NEUTRAL")

prev_regime = _load_prev_regime()
regime_flipped_to_bear = (prev_regime == "LONG" and btc_regime == "SHORT")

if REGIME_FLIP_EXIT and regime_flipped_to_bear:
    print("=" * 50)
    print("⚠️  REGIME FLIP: LONG → SHORT — running emergency exit check")
    print("=" * 50)
    for _sym in COINS:
        _pos = load_position(_sym)
        if _pos is None:
            continue
        _corr = COIN_BTC_CORR.get(_sym, 1.0)
        if _corr <= REGIME_FLIP_EXIT_CORR:
            print(f"  {_sym}: corr={_corr:.2f} ≤ {REGIME_FLIP_EXIT_CORR} — independent coin, skipping emergency exit")
            continue
        _df = coins_data.get(_sym)
        _latest = float(_df["Close"].iloc[-1]) if _df is not None else float(_pos["Entry Price"])
        _entry  = float(_pos["Entry Price"])
        _qty    = float(_pos["Quantity"])
        _side   = _pos["Side"]
        _move   = (_latest - _entry) / _entry if _side == "LONG" else (_entry - _latest) / _entry
        _partial = bool(int(_pos.get("Partial_Taken", 0)))

        # Exit if: losing, flat (< +0.5%), or counter-trend — regardless of partial status
        # Leave profitable trend positions with active trailing stop to trail out
        _should_exit = _move < 0.005 or bool(int(_pos.get("Counter_Trend", 0)))
        if _partial and _move >= 0.005:
            _should_exit = False  # profitable with trailing stop active — let it trail

        if _should_exit:
            _pnl = _move * _entry * _qty
            print(f"  🚨 {_sym}: regime flip exit | corr={_corr:.2f} | move={_move:.2%} | PnL: {_pnl:.2f}")
            run_events.append(f"{_sym} {_pnl:+.2f} REGIME_FLIP")
            realized_pnl    += _pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += _entry * _qty + _pnl
            log_trade(_sym, {
                "Coin":        _sym,
                "Side":        _side,
                "Entry Price": _entry,
                "Exit Price":  _latest,
                "Quantity":    _qty,
                "PnL":         round(_pnl, 4),
                "Exit Reason": "REGIME_FLIP_EXIT",
                "Exit Time":   datetime.now(timezone.utc),
            })
            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(_sym, "sell", _qty)
            clear_position(_sym)
        else:
            print(f"  ✅ {_sym}: profitable trend position (move={_move:.2%}) — letting trail exit handle it")
    dir_counts_cache = count_open_by_direction()
    print()

_save_prev_regime(btc_regime)

# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Step 3A: Evaluate all coins — collect signals, manage open positions.
# ------------------------------------------------------------------
# Two-pass architecture:
#   Pass 1 (Step 3A): iterate all coins in list order.
#                     - Manage exits on open positions (always processed).
#                     - Collect entry candidates (coins that are flat + have signal).
#   Pass 2 (Step 3B): rank candidates by signal quality, enter best ones first.
#
# This fixes entry-order bias: previously the first 5 coins in the list
# would always win when >5 signals fired simultaneously. Now the highest-
# conviction signal (score 5/5, tiebreak by ADX) enters first regardless
# of list position.
#
# Ranking key: (soft_confirmations DESC, adx DESC)
#   4/4 + ADX=38 beats 3/4 + ADX=25 which beats 2/4 + ADX=30
# ------------------------------------------------------------------

dir_counts_cache = count_open_by_direction()
entry_candidates = []   # list of (symbol, signal_data, df) to rank and enter

for symbol in COINS:
    if symbol not in coins_data:
        print(f"  Skipped (no data).\n")
        continue

    df           = coins_data[symbol]
    signal_data  = generate_signal(df)
    signal_dir   = signal_data["signal"] if signal_data else None
    latest_price = float(df["Close"].iloc[-1])
    position     = load_position(symbol)

    # ------------------------------------------------------------------
    # Lane 2: Mean Reversion signal check
    # Runs on MR_COINS only, when there is no open position and no
    # momentum signal. BTC must not be SHORT.
    # If it fires it is stored in mr_signal_data; the entry block below
    # treats it like a momentum entry but routes exits differently.
    # ------------------------------------------------------------------
    mr_signal_data = None
    if (symbol in MR_COINS
            and position is None
            and signal_dir is None
            and btc_regime != "SHORT"):
        mr_signal_data = check_mean_reversion(df)
        if mr_signal_data:
            print(f"  📉 MR signal — drop {mr_signal_data['drop_pct']:.1f}%  "
                  f"RSI={mr_signal_data['rsi']:.1f}  "
                  f"TP=+{mr_signal_data['mr_tp_pct']*100:.0f}%  "
                  f"SL=-{mr_signal_data['mr_sl_pct']*100:.0f}%  "
                  f"MaxHold={mr_signal_data['mr_max_hours']}h")

    # Determine effective TP and trail targets.
    # Counter-trend LONGs (bounces below EMA200) use tighter targets
    # so we bank profit quickly before the macro trend reasserts.
    # The flag is stored on the position so exits can reference it.
    # Counter-trend: 1H EMA200 not satisfied OR 4H is bearish
    _sig_ct = signal_data.get('counter_trend', False) if signal_data else False
    if USE_4H_REGIME and symbol != "BTC/INR":
        _4h_dir, _4h_reason = get_4h_direction(symbol)
        _4h_ct = (_4h_dir == "BEAR")
    else:
        _4h_dir, _4h_reason = "N/A", ""
        _4h_ct = False

    is_counter_trend = (
        bool(int(position.get('Counter_Trend', 0))) if position is not None
        else (_sig_ct or _4h_ct)
    )
    effective_tp    = COUNTER_TREND_TP_PCT    if is_counter_trend else PARTIAL_TAKE_PROFIT_PCT
    # In bull regime, use wider 3% trail for trend longs to capture bigger moves
    _base_trail     = BULL_TRAILING_STOP_PCT if (btc_regime == "LONG" and not is_counter_trend)                       else TRAILING_STOP_PCT
    effective_trail = COUNTER_TREND_TRAIL_PCT if is_counter_trend else _base_trail

    # ------------------------------------------------------------------
    # Per-coin single-line status — only printed if score >= 2 or open position
    # Shows all indicators with tick/cross on one line.
    # ------------------------------------------------------------------
    _row       = df.iloc[-1]
    _atr_ratio = (_row["ATR"] / _row["ATR_SMA"]) if _row["ATR_SMA"] > 0 else 0
    _adx_ok    = _row["ADX"] >= ADX_THRESHOLD
    _atr_ok    = _atr_ratio >= ATR_EXPANSION_RATIO
    _sq_off    = not bool(_row.get("Squeeze_On", False))
    _st_bull   = bool(_row["Supertrend"])
    _s1        = _row["RSI"] > 52
    _s2        = _row["Volume"] > _row["Volume_Baseline"]
    _s3        = latest_price > _row["EMA200"]
    _s4        = latest_price > _row["EMA50"]
    _prev3     = _row.get("Prev3_High", float("nan"))
    _s5        = (not pd.isna(_prev3)) and (latest_price > _prev3)
    _score     = int(_s1) + int(_s2) + int(_s3) + int(_s4) + int(_s5)

    # Only print if coin has an open position OR score >= 2 (worth watching)
    _worth_showing = position is not None or _score >= 2

    if _worth_showing:
        if position is not None:
            _move   = (latest_price - float(position['Entry Price'])) / float(position['Entry Price'])
            _status = f"📈 Holding {position['Side']} {_move:+.1%}"
        elif signal_data is not None or mr_signal_data is not None:
            _status = "📋 Candidate"
        else:
            if not _adx_ok:
                _reason = f"ADX={_row['ADX']:.1f}<{ADX_THRESHOLD}"
            elif not _atr_ok:
                _reason = "ATR compressed"
            elif not _st_bull:
                _reason = "SuperTrend=BEAR"
            elif not _sq_off:
                _reason = "Squeeze ON"
            elif _score < LONG_SOFT_REQUIRED:
                _reason = f"score={_score}/5 need {LONG_SOFT_REQUIRED}"
            else:
                _reason = "filtered"
            _status = f"⛔ {_reason}"

        print(f"--- {symbol} --- "
              f"ADX={_row['ADX']:.1f}{'✅' if _adx_ok else '❌'}  "
              f"RSI={_row['RSI']:.1f}{'✅' if _s1 else '❌'}  "
              f"SuperTrend={'✅' if _st_bull else '❌'}  "
              f"Squeeze={'✅' if _sq_off else '❌'}  "
              f"Vol={'✅' if _s2 else '❌'}  "
              f"EMA200={'✅' if _s3 else '❌'}  "
              f"EMA50={'✅' if _s4 else '❌'}  "
              f"HighHigh={'✅' if _s5 else '❌'}  "
              f"score={_score}/5  {_status}")

    if VERBOSE_DIAG and _worth_showing:
        print(f"  [DIAG] Price={latest_price:.4f}  EMA200={_row['EMA200']:.4f}")
        print(f"  [DIAG] ATR={_row['ATR']:.4f}  ATR_SMA={_row['ATR_SMA']:.4f}  "
              f"Ratio={_atr_ratio:.2f}")
        if _4h_dir != "N/A":
            print(f"  [DIAG] 4H={_4h_dir}  {_4h_reason}")

    # -------------------------------------------------------------------
    # Stop-loss — always checked first, no gate bypasses this
    # Applies to the full remaining quantity (whether or not Tier 1 fired)
    # -------------------------------------------------------------------
    if position is not None and not int(position.get("MR_Entry", 0)):
        side        = position['Side']
        entry_price = float(position['Entry Price'])
        quantity    = float(position['Quantity'])

        move_pct = (
            (latest_price - entry_price) / entry_price if side == "LONG"
            else (entry_price - latest_price) / entry_price
        )

        if move_pct <= -STOP_LOSS_PCT:
            pnl = move_pct * entry_price * quantity
            print(f"  ⛔ STOP-LOSS hit ({move_pct:.2%}) | PnL: {pnl:.2f}")
            run_events.append(f"{symbol} {pnl:+.2f} STOP_LOSS")

            realized_pnl    += pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += entry_price * quantity + pnl

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         round(pnl, 4),
                "Exit Reason": "STOP_LOSS",
                "Exit Time":   datetime.now(timezone.utc)
            })

            # Live mode: place real sell order
            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", quantity)
            clear_position(symbol)
            dir_counts_cache = count_open_by_direction()  # refresh after close
            position = None

    # -------------------------------------------------------------------
    # Lane 2 exit: MR positions use simple TP / SL / deadline logic.
    # They never enter the Tier 1/2/3/4 system — they're short bounce
    # trades, not trend rides. Routing them here ensures the tiered exits
    # below are completely untouched.
    # -------------------------------------------------------------------
    if position is not None and int(position.get("MR_Entry", 0)) == 1:
        mr_tp    = float(position.get("MR_TP_Price", 0))
        mr_sl    = float(position.get("MR_SL_Price", 0))
        mr_ddl   = str(position.get("MR_Deadline", ""))
        side     = position["Side"]
        ep       = float(position["Entry Price"])
        qty      = float(position["Quantity"])
        now_utc  = datetime.now(timezone.utc)

        mr_exit_price  = None
        mr_exit_reason = None

        # TP hit
        if latest_price >= mr_tp > 0:
            mr_exit_price  = mr_tp
            mr_exit_reason = "MR_TP"

        # SL hit
        elif latest_price <= mr_sl > 0:
            mr_exit_price  = mr_sl
            mr_exit_reason = "MR_SL"

        # Deadline (time backstop)
        elif mr_ddl:
            try:
                deadline = datetime.fromisoformat(mr_ddl)
                if now_utc >= deadline:
                    mr_exit_price  = latest_price
                    mr_exit_reason = "MR_TIMEOUT"
            except ValueError:
                pass

        if mr_exit_price is not None:
            pnl = (mr_exit_price - ep) / ep * ep * qty
            print(f"  {'✅' if pnl >= 0 else '⛔'} MR EXIT — {mr_exit_reason} "
                  f"| Entry={ep:.4f} → Exit={mr_exit_price:.4f} "
                  f"| PnL: {pnl:+.2f}")
            run_events.append(f"{symbol} {pnl:+.2f} {mr_exit_reason}")

            realized_pnl    += pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += ep * qty + pnl

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": ep,
                "Exit Price":  round(mr_exit_price, 6),
                "Quantity":    qty,
                "PnL":         round(pnl, 4),
                "Exit Reason": mr_exit_reason,
                "Exit Time":   now_utc,
            })

            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", qty)
            clear_position(symbol)
            dir_counts_cache = count_open_by_direction()
            position = None

        else:
            # Still holding — show MR position status
            move_pct = (latest_price - ep) / ep
            print(f"  📉 MR HOLDING | Entry={ep:.4f} | Now={latest_price:.4f} "
                  f"| Move={move_pct:+.2%} "
                  f"| TP={mr_tp:.4f} SL={mr_sl:.4f}")
            if mr_ddl:
                try:
                    deadline = datetime.fromisoformat(mr_ddl)
                    hrs_left = (deadline - now_utc).total_seconds() / 3600
                    print(f"     Deadline in {hrs_left:.1f}h")
                except ValueError:
                    pass

    # -------------------------------------------------------------------
    # Tier 1 — Two-stage partial profit-taking
    # -------------------------------------------------------------------
    # Stage 1A: at +2% → close 25% of original position (locks small profit)
    # Stage 1B: at +3% → close another 25% (matches original partial behaviour)
    # Trail half (50%) activates AFTER Tier 1B fires.
    #
    # Counter-trend trades skip Tier 1A and use a single partial at CT TP (1.5%)
    # on 50% of position — bounces don't have room for two-stage scale-out.
    # The legacy effective_tp variable still drives the CT path.
    # -------------------------------------------------------------------
    if position is not None and not int(position.get("MR_Entry", 0)):
        side             = position['Side']
        entry_price      = float(position['Entry Price'])
        quantity         = float(position['Quantity'])
        already_partial  = bool(int(position.get('Partial_Taken', 0)))    # = Tier 1B fired
        tier1a_taken     = bool(int(position.get('Tier1A_Taken', 0)))
        orig_qty         = float(position.get('Original_Qty', quantity))  # for ratio calc

        move_pct = (
            (latest_price - entry_price) / entry_price if side == "LONG"
            else (entry_price - latest_price) / entry_price
        )

        # ---------- CT path (single partial at +1.5%) ----------
        if is_counter_trend:
            if not already_partial and move_pct >= effective_tp:
                exit_qty    = quantity * PARTIAL_EXIT_RATIO   # 50% of current = 50% of orig
                remain_qty  = quantity - exit_qty
                partial_pnl = move_pct * entry_price * exit_qty

                realized_pnl    += partial_pnl
                total_trades    += 1
                trades_this_run += 1
                cash            += entry_price * exit_qty + partial_pnl

                run_events.append(f"{symbol} {partial_pnl:+.2f} PARTIAL_CT")
                print(f"  💰 TIER 1 partial exit (CT, {move_pct:.2%} gain) | "
                      f"Closed {PARTIAL_EXIT_RATIO:.0%} @ {latest_price:.4f} | "
                      f"PnL: +{partial_pnl:.2f} | Remaining qty: {remain_qty:.6f}")

                log_trade(symbol, {
                    "Coin":        symbol,
                    "Side":        side,
                    "Entry Price": entry_price,
                    "Exit Price":  latest_price,
                    "Quantity":    exit_qty,
                    "PnL":         round(partial_pnl, 4),
                    "Exit Reason": "PARTIAL_PROFIT",
                    "Exit Time":   datetime.now(timezone.utc)
                })

                if TRADING_MODE == "live":
                    from exchange import place_market_order
                    place_market_order(symbol, "sell", exit_qty)

                save_position(symbol, {
                    "Coin":          symbol,
                    "Side":          side,
                    "Entry Price":   entry_price,
                    "Quantity":      remain_qty,
                    "Timestamp":     position['Timestamp'],
                    "Partial_Taken": 1,
                    "Tier1A_Taken":  1,  # collapsed for CT (no separate 1A stage)
                    "Original_Qty":  orig_qty,
                    "Trail_HWM":     latest_price,
                    "Bars_Held":     int(position.get('Bars_Held', 0)),
                    "Counter_Trend": int(position.get('Counter_Trend', 0)),
                })
                position = load_position(symbol)

        # ---------- Trend path (two stages: +2% then +3%) ----------
        else:
            # Stage 1A: +2% — close 25% of ORIGINAL quantity
            if not tier1a_taken and move_pct >= PARTIAL_TP_1A_PCT:
                exit_qty    = orig_qty * PARTIAL_EXIT_RATIO_1A
                if exit_qty > quantity:    # safety
                    exit_qty = quantity
                remain_qty  = quantity - exit_qty
                partial_pnl = move_pct * entry_price * exit_qty

                realized_pnl    += partial_pnl
                total_trades    += 1
                trades_this_run += 1
                cash            += entry_price * exit_qty + partial_pnl

                run_events.append(f"{symbol} {partial_pnl:+.2f} PARTIAL_1A")
                print(f"  💰 TIER 1A partial exit ({move_pct:.2%} gain) | "
                      f"Closed {PARTIAL_EXIT_RATIO_1A:.0%} of original @ {latest_price:.4f} | "
                      f"PnL: +{partial_pnl:.2f} | Remaining qty: {remain_qty:.6f}")

                log_trade(symbol, {
                    "Coin":        symbol,
                    "Side":        side,
                    "Entry Price": entry_price,
                    "Exit Price":  latest_price,
                    "Quantity":    exit_qty,
                    "PnL":         round(partial_pnl, 4),
                    "Exit Reason": "PARTIAL_PROFIT_1A",
                    "Exit Time":   datetime.now(timezone.utc)
                })

                if TRADING_MODE == "live":
                    from exchange import place_market_order
                    place_market_order(symbol, "sell", exit_qty)

                save_position(symbol, {
                    "Coin":          symbol,
                    "Side":          side,
                    "Entry Price":   entry_price,
                    "Quantity":      remain_qty,
                    "Timestamp":     position['Timestamp'],
                    "Partial_Taken": 0,           # Tier 1B not yet, trail not active
                    "Tier1A_Taken":  1,
                    "Original_Qty":  orig_qty,
                    "Trail_HWM":     latest_price,   # seed HWM at 1A price
                    "Bars_Held":     int(position.get('Bars_Held', 0)),
                    "Counter_Trend": int(position.get('Counter_Trend', 0)),
                })
                position = load_position(symbol)
                # refresh local vars after reload
                quantity        = float(position['Quantity'])
                tier1a_taken    = True

            # Stage 1B: +3% — close another 25% of ORIGINAL quantity, activate trail
            if not already_partial and move_pct >= PARTIAL_TP_1B_PCT:
                exit_qty    = orig_qty * PARTIAL_EXIT_RATIO_1B
                if exit_qty > quantity:    # safety
                    exit_qty = quantity
                remain_qty  = quantity - exit_qty
                partial_pnl = move_pct * entry_price * exit_qty

                realized_pnl    += partial_pnl
                total_trades    += 1
                trades_this_run += 1
                cash            += entry_price * exit_qty + partial_pnl

                run_events.append(f"{symbol} {partial_pnl:+.2f} PARTIAL_1B")
                print(f"  💰 TIER 1B partial exit ({move_pct:.2%} gain) | "
                      f"Closed {PARTIAL_EXIT_RATIO_1B:.0%} of original @ {latest_price:.4f} | "
                      f"PnL: +{partial_pnl:.2f} | Remaining qty: {remain_qty:.6f}")

                log_trade(symbol, {
                    "Coin":        symbol,
                    "Side":        side,
                    "Entry Price": entry_price,
                    "Exit Price":  latest_price,
                    "Quantity":    exit_qty,
                    "PnL":         round(partial_pnl, 4),
                    "Exit Reason": "PARTIAL_PROFIT",   # legacy reason kept for trail half
                    "Exit Time":   datetime.now(timezone.utc)
                })

                if TRADING_MODE == "live":
                    from exchange import place_market_order
                    place_market_order(symbol, "sell", exit_qty)

                save_position(symbol, {
                    "Coin":          symbol,
                    "Side":          side,
                    "Entry Price":   entry_price,
                    "Quantity":      remain_qty,
                    "Timestamp":     position['Timestamp'],
                    "Partial_Taken": 1,        # trail now active
                    "Tier1A_Taken":  1,
                    "Original_Qty":  orig_qty,
                    "Trail_HWM":     latest_price,   # re-seed HWM at 1B price
                    "Bars_Held":     int(position.get('Bars_Held', 0)),
                    "Counter_Trend": int(position.get('Counter_Trend', 0)),
                })
                position = load_position(symbol)

    # -------------------------------------------------------------------
    # Tier 2 — Trailing stop on the remaining position
    # Only active after Tier 1 has fired (Partial_Taken == 1).
    # Updates the high-water mark each bar and exits if price drops
    # TRAILING_STOP_PCT below the peak.
    # -------------------------------------------------------------------
    if position is not None and not int(position.get("MR_Entry", 0)):
        side            = position['Side']
        entry_price     = float(position['Entry Price'])
        quantity        = float(position['Quantity'])
        already_partial = bool(int(position.get('Partial_Taken', 0)))

        if already_partial:
            # Update high-water mark
            hwm = float(position.get('Trail_HWM', entry_price))
            if side == "LONG":
                hwm = max(hwm, latest_price)
                trail_breach = latest_price < hwm * (1 - effective_trail)
            else:
                hwm = min(hwm, latest_price)
                trail_breach = latest_price > hwm * (1 + effective_trail)

            # Persist updated HWM each bar
            save_position(symbol, {
                "Coin":          symbol,
                "Side":          side,
                "Entry Price":   entry_price,
                "Quantity":      quantity,
                "Timestamp":     position['Timestamp'],
                "Partial_Taken": 1,
                "Tier1A_Taken":  int(position.get('Tier1A_Taken', 1)),
                "Original_Qty":  float(position.get('Original_Qty', quantity)),
                "Trail_HWM":     hwm,
                "Bars_Held":     int(position.get('Bars_Held', 0)),
                "Counter_Trend": int(position.get('Counter_Trend', 0)),
            })
            position = load_position(symbol)

            if trail_breach:
                move_pct = (
                    (latest_price - entry_price) / entry_price if side == "LONG"
                    else (entry_price - latest_price) / entry_price
                )
                pnl = move_pct * entry_price * quantity

                realized_pnl    += pnl
                total_trades    += 1
                trades_this_run += 1
                cash            += entry_price * quantity + pnl

                run_events.append(f"{symbol} {pnl:+.2f} TRAIL_STOP")
                print(f"  🔔 TIER 2 trailing stop hit | HWM={hwm:.4f} | "
                      f"Price={latest_price:.4f} | PnL: {pnl:.2f}")

                log_trade(symbol, {
                    "Coin":        symbol,
                    "Side":        side,
                    "Entry Price": entry_price,
                    "Exit Price":  latest_price,
                    "Quantity":    quantity,
                    "PnL":         round(pnl, 4),
                    "Exit Reason": "TRAILING_STOP",
                    "Exit Time":   datetime.now(timezone.utc)
                })

            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", quantity)
                clear_position(symbol)
                dir_counts_cache = count_open_by_direction()  # refresh after close
                position = None

    # -------------------------------------------------------------------
    # Counter-signal exit (existing logic, unchanged)
    # -------------------------------------------------------------------
    if position is not None and signal_dir is not None:
        side        = position['Side']
        entry_price = float(position['Entry Price'])
        quantity    = float(position['Quantity'])

        counter = (side == "LONG" and signal_dir == "SHORT") or \
                  (side == "SHORT" and signal_dir == "LONG")

        if counter:
            pnl = (
                (latest_price - entry_price) * quantity if side == "LONG"
                else (entry_price - latest_price) * quantity
            )

            realized_pnl    += pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += entry_price * quantity + pnl

            run_events.append(f"{symbol} {pnl:+.2f} COUNTER_SIGNAL")
            print(f"  🔁 Counter-signal exit | PnL: {pnl:.2f}")

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         round(pnl, 4),
                "Exit Reason": "COUNTER_SIGNAL",
                "Exit Time":   datetime.now(timezone.utc)
            })

            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", quantity)
            clear_position(symbol)
            dir_counts_cache = count_open_by_direction()  # refresh after close
            position = None

    # -------------------------------------------------------------------
    # -------------------------------------------------------------------
    # Tier 5 — Signal deterioration exit
    # -------------------------------------------------------------------
    # If the 1H signal that caused entry has materially reversed — score
    # dropped to SIGNAL_EXIT_THRESHOLD (≤1/5) — exit immediately.
    # No point holding a position whose entry thesis is gone.
    # Uses the already-fetched signal_data so zero extra API calls.
    # -------------------------------------------------------------------
    if position is not None and SIGNAL_DETERIORATION_EXIT and signal_data is not None:
        current_score = signal_data.get('soft_confirmations', 0)
        if current_score <= SIGNAL_EXIT_THRESHOLD:
            side        = position['Side']
            entry_price = float(position['Entry Price'])
            quantity    = float(position['Quantity'])
            move_pct    = (latest_price - entry_price) / entry_price if side == "LONG"                           else (entry_price - latest_price) / entry_price
            pnl = move_pct * entry_price * quantity

            realized_pnl    += pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += entry_price * quantity + pnl

            s1 = signal_data.get('s1_rsi', False)
            s2 = signal_data.get('s2_volume', False)
            s3 = signal_data.get('s3_ema200', False)
            s4 = signal_data.get('s4_ema50', False)
            run_events.append(f"{symbol} {pnl:+.2f} SIG_DETN")
            print(f"  📉 SIGNAL DETERIORATION exit — score dropped to {current_score}/5 "
                  f"(RSI={s1} Vol={s2} EMA200={s3} EMA50={s4}) | PnL: {pnl:.2f}")

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         round(pnl, 4),
                "Exit Reason": "SIGNAL_DETERIORATION",
                "Exit Time":   datetime.now(timezone.utc)
            })
            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", quantity)
            clear_position(symbol)
            dir_counts_cache = count_open_by_direction()
            position = None

    # -------------------------------------------------------------------
    # Tier 0 (new) — Counter-trend 15-min early exit
    # -------------------------------------------------------------------
    # For counter-trend LONGs only: monitor every 15-min run whether the
    # bounce is reversing. If CT_EXIT_CONSEC_BARS consecutive 15-min bars
    # are falling → exit before the reversal eats into the 1.5% target.
    # Normal trend LONGs skip this check entirely.
    # -------------------------------------------------------------------
    if position is not None and CT_EXIT_15MIN:
        pos_is_ct = bool(int(position.get('Counter_Trend', 0)))
        if pos_is_ct:
            ct_should_exit, ct_reason = check_15min_ct_exit(symbol)
            if VERBOSE_DIAG:
                print(f"  [CT-monitor] {ct_reason}")
            if ct_should_exit:
                side        = position['Side']
                entry_price = float(position['Entry Price'])
                quantity    = float(position['Quantity'])
                move_pct    = (latest_price - entry_price) / entry_price if side == "LONG"                               else (entry_price - latest_price) / entry_price
                pnl = move_pct * entry_price * quantity

                realized_pnl    += pnl
                total_trades    += 1
                trades_this_run += 1
                cash            += entry_price * quantity + pnl

                run_events.append(f"{symbol} {pnl:+.2f} CT_EXIT")
                print(f"  ⚡ CT EARLY EXIT — bounce reversing | PnL: {pnl:.2f}")
                log_trade(symbol, {
                    "Coin":        symbol,
                    "Side":        side,
                    "Entry Price": entry_price,
                    "Exit Price":  latest_price,
                    "Quantity":    quantity,
                    "PnL":         round(pnl, 4),
                    "Exit Reason": "CT_15MIN_REVERSAL",
                    "Exit Time":   datetime.now(timezone.utc)
                })
                if TRADING_MODE == "live":
                    from exchange import place_market_order
                    place_market_order(symbol, "sell", quantity)
                clear_position(symbol)
                dir_counts_cache = count_open_by_direction()
                position = None

    # -------------------------------------------------------------------
    # Tier 4 — Time-based exit backstop (HOUR-based, timeframe-agnostic)
    # -------------------------------------------------------------------
    # Uses hours since entry (from Timestamp) not bar count.
    # This means exits are correct whether cron runs every 15-min or 1h.
    #
    #   4A  Stagnant : hours_held >= TIME_EXIT_STAGNANT_HOURS  AND |move| < 0.5%
    #   4D  Losing   : hours_held >= TIME_EXIT_LOSING_HOURS    AND move < 0%
    #   4B  Stuck+   : hours_held >= TIME_EXIT_EXTENDED_HOURS  AND 0 < move < TP
    #   4C  Trail TO : Tier 1 fired AND hours_held >= TIME_EXIT_TRAIL_HOURS
    # -------------------------------------------------------------------
    if position is not None and not int(position.get("MR_Entry", 0)):
        side            = position['Side']
        entry_price     = float(position['Entry Price'])
        quantity        = float(position['Quantity'])
        already_partial = bool(int(position.get('Partial_Taken', 0)))

        # Hour-based elapsed time
        try:
            entry_ts  = pd.Timestamp(position['Timestamp']).tz_convert('UTC')
            now_ts    = datetime.now(timezone.utc)
            hours_held = (now_ts - entry_ts.to_pydatetime()).total_seconds() / 3600
        except Exception:
            hours_held = float(position.get('Bars_Held', 0))  # fallback

        move_pct = (
            (latest_price - entry_price) / entry_price if side == "LONG"
            else (entry_price - latest_price) / entry_price
        )

        tier4_exit   = False
        tier4_reason = ""

        # Regime-aware time exit thresholds
        _stagnant_hours = TIME_EXIT_STAGNANT_HOURS_BULL if btc_regime == "LONG" else TIME_EXIT_STAGNANT_HOURS
        _losing_hours   = TIME_EXIT_LOSING_HOURS_BEAR   if btc_regime != "LONG" else TIME_EXIT_LOSING_HOURS

        # 4A — stagnant: no meaningful move after _stagnant_hours (regime-aware)
        if not tier4_exit and hours_held >= _stagnant_hours \
                and abs(move_pct) < TIME_EXIT_MIN_MOVE_PCT:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_STAGNANT ({hours_held:.1f}h, move={move_pct:.2%})"

        # 4D — losing: thesis failed, cut early (faster in non-bull regime)
        if not tier4_exit and not already_partial \
                and hours_held >= _losing_hours \
                and move_pct < 0:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_LOSING ({hours_held:.1f}h, move={move_pct:.2%})"

        # 4B — stuck profitable: Tier 1 never fired, take small gain
        # Signal-aware: only exit if signal has weakened to <= 2/4.
        # If signal is still 3/5 or 4/5, hold — coin is still trending.
        # Selling a strong-signal position just to re-enter at a higher
        # price creates unnecessary tax events in live trading.
        _current_score = signal_data.get('soft_confirmations', 0) if signal_data else 0
        _signal_weak   = _current_score <= 2
        if not tier4_exit and not already_partial \
                and hours_held >= TIME_EXIT_EXTENDED_HOURS \
                and 0 < move_pct < effective_tp \
                and _signal_weak:
            tier4_exit   = True
            tier4_reason = (f"TIME_EXIT_STUCK_PROFIT ({hours_held:.1f}h, "
                            f"move={move_pct:.2%}, score={_current_score}/5)")

        # 4C — trailing half timeout
        if not tier4_exit and already_partial                 and hours_held >= TIME_EXIT_TRAIL_HOURS:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_TRAIL_TIMEOUT ({hours_held:.1f}h)"

        if tier4_exit:
            pnl = move_pct * entry_price * quantity

            realized_pnl    += pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += entry_price * quantity + pnl

            run_events.append(f"{symbol} {pnl:+.2f} {tier4_reason.split("(")[0].strip()}")
            print(f"  ⏱️  TIER 4 exit | {tier4_reason} | PnL: {pnl:.2f}")

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         round(pnl, 4),
                "Exit Reason": tier4_reason.split(" (")[0],
                "Exit Time":   datetime.now(timezone.utc)
            })

            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", quantity)
            clear_position(symbol)
            dir_counts_cache = count_open_by_direction()
            position = None
        else:
            # Persist position — keep Bars_Held for display purposes only
            bars_held = int(position.get('Bars_Held', 0)) + 1
            save_position(symbol, {
                "Coin":          symbol,
                "Side":          side,
                "Entry Price":   entry_price,
                "Quantity":      quantity,
                "Timestamp":     position['Timestamp'],
                "Partial_Taken": int(position.get('Partial_Taken', 0)),
                "Tier1A_Taken":  int(position.get('Tier1A_Taken', 0)),
                "Original_Qty":  float(position.get('Original_Qty', quantity)),
                "Trail_HWM":     float(position.get('Trail_HWM', entry_price)),
                "Bars_Held":     bars_held,
                "Counter_Trend": int(position.get('Counter_Trend', 0)),
            })

    # -------------------------------------------------------------------
    # Entry candidate collection (Pass 1)
    # Gates 1, 1.5, 2 are checked here.
    # Gate 3 (position count) and capital guard are checked in Pass 2
    # after ranking, so the best signal always enters first.
    # -------------------------------------------------------------------
    if position is None and signal_dir is not None:

        # Gate 1 (implicit): position is None — coin is flat ✓

        # Gate 1.5: RSI reset filter
        rsi_ok, rsi_block_reason = rsi_reset_allows_entry(symbol, signal_dir, df)
        if not rsi_ok:
            print(f"  🚫 RSI reset: {rsi_block_reason}")
            print()
            continue

        # Gate 1.6: Re-entry price filter — don't re-enter below last exit price
        # When a coin completed a profitable cycle (trail stop or trail timeout),
        # the last exit price is the high-water mark of that move. If current
        # price is below that exit price, the coin is retracing — buying it now
        # is chasing a declining price, not a fresh move.
        #
        # Example: NEAR trailed out at ₹242.74. Re-entry at ₹242.13 is below
        # that exit — the coin is falling from its recent peak. Block it.
        # Block only applies after completed winning cycles, not after losses
        # (after a loss we want a recovery, so higher price is expected).
        _last_t = get_last_trade(symbol)
        if _last_t is not None:
            _last_pnl    = float(_last_t.get("PnL", 0))
            _last_reason = str(_last_t.get("Exit Reason", ""))
            _last_exit_px = float(_last_t.get("Exit Price", 0))
            if (_last_pnl > 0
                    and _last_reason in _COMPLETED_CYCLE_REASONS
                    and signal_dir == "LONG"
                    and latest_price < _last_exit_px):
                print(f"  🚫 Re-entry price check — current ₹{latest_price:.4f} is below "
                      f"last exit ₹{_last_exit_px:.4f} after {_last_reason}. "
                      f"Coin retracing from peak, not a fresh move.")
                print()
                continue

        # Gate 2: BTC regime filter
        if symbol != "BTC/INR":
            if LONG_ONLY:
                if btc_regime == "SHORT":
                    coin_score      = signal_data.get('soft_confirmations', 0)
                    is_ct           = signal_data.get('counter_trend', True) or _4h_ct
                    coin_corr       = COIN_BTC_CORR.get(symbol, 1.0)
                    corr_allows     = coin_corr <= REGIME_OVERRIDE_MAX_CORR
                    regime_override = (
                        coin_score >= REGIME_OVERRIDE_MIN_SCORE
                        and not is_ct
                        and corr_allows
                    )
                    if regime_override:
                        print(f"  ⚡ BTC regime SHORT overridden — "
                              f"coin score {coin_score}/5, corr={coin_corr:.2f}, not counter-trend.")
                    elif not corr_allows:
                        print(f"  🚫 BTC regime SHORT — blocked (corr={coin_corr:.2f} > {REGIME_OVERRIDE_MAX_CORR}). "
                              f"Too correlated to BTC to trade in bear market.")
                        print()
                        continue
                    else:
                        print(f"  🚫 BTC regime SHORT — LONG entry blocked in bear market.")
                        print()
                        continue
                if btc_regime is None and not REGIME_ALLOWS_LONG_IN_NEUTRAL:
                    print(f"  🚫 BTC regime NEUTRAL — entry blocked.")
                    print()
                    continue
            else:
                if btc_regime is None:
                    print(f"  🚫 BTC regime NEUTRAL — entry blocked.")
                    print()
                    continue
                if btc_regime != signal_dir:
                    print(f"  🚫 BTC regime mismatch — signal={signal_dir}, BTC={btc_regime}. Blocked.")
                    print()
                    continue

        # Passed gates 1, 1.5, 2 — add to ranked candidate pool
        score    = signal_data.get('soft_confirmations', 0)
        adx      = float(signal_data.get('adx', 0))
        is_ct    = signal_data.get('counter_trend', False) or _4h_ct
        coin_corr = COIN_BTC_CORR.get(symbol, 1.0)

        # Gate 2.4: 4H hard direction gate
        # If 4H Supertrend on the coin is BEAR, block entry entirely.
        # The 4H reflects the coin's actual multi-hour structure — bouncing
        # bullish on 1H while 4H is decisively bearish is fighting the trend.
        # This replaces the old CT-flag-only behaviour for 4H BEAR.
        # BTC/INR itself is exempt (we use BTC's own regime separately).
        if USE_4H_HARD_GATE and symbol != "BTC/INR" and _4h_dir == "BEAR":
            print(f"  🚫 4H direction is BEAR on {symbol} — entry blocked "
                  f"(1H bounce inside multi-hour downtrend).")
            print()
            continue

        # Gate 2.45: Daily trend alignment gate
        # Coin must be above its daily EMA50 to confirm the bigger picture
        # trend is bullish. A coin can show 4/4 on 1H while being in a
        # clear daily downtrend — this gate blocks those entries.
        # Fails open on data error so a candle fetch failure never blocks
        # a valid entry.
        if symbol != "BTC/INR":
            _daily_bull, _daily_reason = get_daily_trend(symbol)
            if not _daily_bull:
                print(f"  🚫 Daily trend not aligned — {_daily_reason}")
                print()
                continue
            if VERBOSE_DIAG:
                print(f"  [Daily] {_daily_reason}")

        # Gate 2.5: CT minimum score filter
        # Counter-trend entries need higher confirmation than trend entries.
        # Below EMA200 + weak score = too risky. Filter before queuing.
        if is_ct and score < CT_SOFT_REQUIRED:
            print(f"  🚫 CT score too low — score={score}/5 < {CT_SOFT_REQUIRED} required for counter-trend. Blocked.")
            print()
            continue

        # Gate 2.6: Block ALL longs for high-corr coins in BTC SHORT regime.
        # Previously only CT entries were blocked. But trend entries for BNB,
        # TRX, FIL, DOT were still going through in a falling market and losing.
        # In BTC SHORT, any coin with corr > 0.65 moves with BTC — entering a
        # LONG regardless of signal type is fighting the macro direction.
        # Low-corr coins (FTM -0.65, MANA -0.40, MATIC +0.13, ETH +0.22, ARB +0.38)
        # are below the threshold and still evaluated on their own merit.
        if btc_regime == "SHORT" and coin_corr > 0.65:
            print(f"  🚫 BTC SHORT regime — ALL longs blocked for {symbol} "
                  f"(corr={coin_corr:.2f} > 0.65). High-corr coin in bear market.")
            print()
            continue

        # Gate 2.7: CT-in-SHORT correlation block for low-corr coins.
        # Low-corr coins can trend-enter in BTC SHORT but still can't CT-enter —
        # even independent coins shouldn't bounce-trade against their own 4H BEAR.
        if is_ct and btc_regime == "SHORT" and coin_corr > CT_BLOCK_CORR_IN_SHORT:
            print(f"  🚫 CT entry blocked — BTC SHORT regime + corr={coin_corr:.2f} > {CT_BLOCK_CORR_IN_SHORT}. "
                  f"Too correlated to BTC to bounce-trade in bear market.")
            print()
            continue

        entry_candidates.append((symbol, signal_data, df))
        pass

    elif position is None and mr_signal_data is not None:
        entry_candidates.append((symbol, mr_signal_data, df))

    elif position is not None and signal_dir == position['Side']:
        pass   # status shown on header line

    if _worth_showing:
        print()

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Step 3B-PRE: Market Health Gate
# ---------------------------------------------------------------------------
# Before entering ANY new position, check that the broader crypto market
# is actually trending. Uses a 6-coin reference basket (BTC + 5 alts).
# If the market is unhealthy, entry_candidates is cleared — no new trades.
#
# MR (mean reversion) entries bypass this gate — they fire on panic drops
# and are counter-trend by design. Blocking them on market health defeats
# their purpose. Only momentum entries are gated.
#
# Open positions are NOT affected — they continue through normal exit tiers.

if entry_candidates:
    market_ok, market_reason, market_detail = check_market_health(coins_data)

    print()
    print("=" * 50)
    print("Market Health Check")
    print("=" * 50)
    print(f"  {market_reason}")

    if VERBOSE_DIAG and market_detail:
        for sym, d in market_detail.items():
            if d.get("trending") is not None:
                print(f"    {sym:<12} ADX={d.get('adx','?'):>5}  "
                      f"EMA50={'↑' if d.get('above_ema50') else '↓'}  "
                      f"{'✅' if d.get('trending') else '❌'}")
    print()

    if not market_ok:
        # Split candidates into three buckets:
        #   1. MR entries        — always allowed (counter-trend by design)
        #   2. 4/5 score entries — allowed (coin trending independently, max conviction)
        #   3. 3/5 and below    — blocked (market too weak for lower-conviction entries)
        mr_candidates      = [(s, d, f) for s, d, f in entry_candidates
                              if d.get("signal") == "MR_LONG"]
        highconv_candidates = [(s, d, f) for s, d, f in entry_candidates
                               if d.get("signal") != "MR_LONG"
                               and d.get("soft_confirmations", 0) >= 4]
        blocked_candidates  = [(s, d, f) for s, d, f in entry_candidates
                               if d.get("signal") != "MR_LONG"
                               and d.get("soft_confirmations", 0) < 4]

        if blocked_candidates:
            blocked = [s for s, _, _ in blocked_candidates]
            print(f"  🚫 Market health FAIL — blocking {len(blocked_candidates)} "
                  f"entry/entries (score < 4/5): {blocked}")

        if highconv_candidates:
            allowed = [s for s, _, _ in highconv_candidates]
            print(f"  ⚡ 4/5 score override — entering despite weak market: {allowed}")

        if mr_candidates:
            print(f"  ✅ MR entries bypass market health gate: "
                  f"{[s for s, _, _ in mr_candidates]}")

        # Only high-conviction + MR entries proceed
        entry_candidates = highconv_candidates + mr_candidates

# ---------------------------------------------------------------------------
# Step 3B: Ranked entry — enter best candidates first
# ---------------------------------------------------------------------------
# Sort all entry candidates by:
#   1. soft_confirmations DESC (5/5 before 4/5 before 3/5)
#   2. ADX DESC as tiebreaker (stronger trend gets priority)
#
# Then run Gate 3 (position count) and capital guard in rank order.
# This ensures the highest-conviction signal always enters, regardless
# of coin list position.

if entry_candidates:
    # Sort: highest score first, then highest ADX as tiebreaker
    entry_candidates.sort(
        key=lambda x: (
            x[1].get('soft_confirmations', 0),   # score DESC
            float(x[1].get('adx', 0))            # ADX DESC
        ),
        reverse=True
    )

    print()
    print(f"--- Entry Ranking ({len(entry_candidates)} candidate(s)) ---")
    for rank, (sym, sig, _) in enumerate(entry_candidates, 1):
        print(f"  #{rank} {sym:<14} score={sig.get('soft_confirmations',0)}/5  "
              f"ADX={sig.get('adx',0):.1f}  "
              f"CT={'yes' if sig.get('counter_trend') else 'no'}")
    print()

    for symbol, signal_data, df in entry_candidates:
        signal_dir   = signal_data["signal"]
        latest_price = float(df["Close"].iloc[-1])

        # MR_LONG maps to LONG for all position counting / gate purposes
        is_mr        = (signal_dir == "MR_LONG")
        gate_dir     = "LONG" if is_mr else signal_dir

        is_counter_trend = signal_data.get('counter_trend', False)
        effective_tp     = COUNTER_TREND_TP_PCT    if is_counter_trend else PARTIAL_TAKE_PROFIT_PCT
        effective_trail  = COUNTER_TREND_TRAIL_PCT if is_counter_trend else TRAILING_STOP_PCT

        print(f"--- {symbol} ({'MR' if is_mr else 'momentum'} ranked entry) ---")

        # Gate 3: position count — regime-aware max, checked here so ranking takes effect
        dir_counts  = dir_counts_cache
        _max_pos    = effective_max_positions(btc_regime)
        if dir_counts[gate_dir] >= _max_pos:
            print(f"  🚫 Correlation guard — already {dir_counts[gate_dir]} "
                  f"{gate_dir}(s) open (max={_max_pos} in {btc_regime or 'NEUTRAL'} regime). Blocked.")
            print()
            continue

        # Score-based allocation: higher conviction = more capital
        #   5/5 or 4/5 → 10%  (RISK_PER_TRADE_HIGH)
        #   3/5 → 8%   (RISK_PER_TRADE)
        #   2/5 → 5%   (RISK_PER_TRADE_LOW)
        _score = signal_data.get('soft_confirmations', 0)
        if _score >= 4:
            _risk_rate = RISK_PER_TRADE_HIGH
        elif _score <= 2:
            _risk_rate = RISK_PER_TRADE_LOW
        else:
            _risk_rate = RISK_PER_TRADE

        # Capital guard
        allocation = cash * _risk_rate
        total_capital = cash + sum(
            float(load_position(s)['Entry Price']) * float(load_position(s)['Quantity'])
            for s in COINS if load_position(s) is not None
        )
        deployed = total_capital - cash
        if total_capital > 0 and deployed / total_capital >= MAX_ALLOCATION:
            print(f"  🚫 Capital guard — {deployed/total_capital:.0%} deployed "
                  f"exceeds MAX_ALLOCATION={MAX_ALLOCATION:.0%}.")
            print()
            continue

        if allocation <= 0 or cash < allocation:
            print(f"  🚫 Insufficient cash (₹{cash:.2f}) for allocation ₹{allocation:.2f}.")
            print()
            continue

        quantity = allocation / latest_price

        # Gate 4: 15-min momentum confirmation
        # MR entries bypass this gate — the whole point of mean reversion is
        # that price just dropped and RSI is low. Requiring 15-min RSI > 50
        # would block 100% of valid MR entries. MR has its own confirmation
        # built into the signal (drop size + 4H RSI oversold).
        if CONFIRM_15MIN and not is_mr:
            _entry_score = signal_data.get('soft_confirmations', 0)
            mom_ok, mom_reason = confirm_15min_momentum(symbol, signal_dir)
            if not mom_ok and _entry_score >= 4:
                # Check if failure was only due to RSI flat (not falling), not too low
                _bypass = "flat" in mom_reason.lower() or "falling" in mom_reason.lower()
                if _bypass and "too low" not in mom_reason.lower():
                    mom_ok = True
                    mom_reason = mom_reason + " [bypassed — 4/5 signal]"
            if VERBOSE_DIAG or not mom_ok:
                print(f"  [15min] {mom_reason}")
            if not mom_ok:
                print(f"  🚫 Entry skipped — 15-min momentum check failed.")
                print()
                continue

        # Live mode: place real buy order BEFORE saving position.
        if TRADING_MODE == "live":
            from exchange import place_market_order, get_order_status
            order_id = place_market_order(symbol, "buy", quantity)
            if order_id is None:
                print(f"  ✗ Live order failed — skipping position save.")
                print()
                continue
            import time as _t
            _t.sleep(1.5)
            order_info  = get_order_status(order_id)
            fill_status = order_info.get("status", "unknown")
            fill_price  = float(order_info.get("avg_price", latest_price) or latest_price)
            fill_qty    = float(order_info.get("total_quantity", quantity) or quantity) -                           float(order_info.get("remaining_quantity", 0) or 0)
            print(f"  [live] Order {order_id}: status={fill_status} "
                  f"fill_price={fill_price:.4f} filled_qty={fill_qty:.6f}")
            if fill_status not in ("filled", "partially_filled"):
                print(f"  ✗ Order not filled (status={fill_status}) — skipping.")
                print()
                continue
            latest_price = fill_price
            quantity     = fill_qty

        is_ct = signal_data.get('counter_trend', False)
        save_position(symbol, {
            "Coin":          symbol,
            "Side":          "LONG",   # MR_LONG → LONG on disk
            "Entry Price":   latest_price,
            "Quantity":      quantity,
            "Timestamp":     datetime.now(timezone.utc),
            "Partial_Taken": 0,
            "Tier1A_Taken":  0,
            "Original_Qty":  quantity,
            "Trail_HWM":     latest_price,
            "Bars_Held":     0,
            "Counter_Trend": int(is_ct),
            # MR-specific fields — used by exit logic to route to simple TP/SL
            "MR_Entry":      1 if is_mr else 0,
            "MR_TP_Price":   round(latest_price * (1 + signal_data["mr_tp_pct"]), 6)
                             if is_mr else 0,
            "MR_SL_Price":   round(latest_price * (1 - signal_data["mr_sl_pct"]), 6)
                             if is_mr else 0,
            "MR_Deadline":   (datetime.now(timezone.utc) +
                              timedelta(hours=signal_data["mr_max_hours"])).isoformat()
                             if is_mr else "",
        })

        cash -= allocation
        dir_counts_cache = count_open_by_direction()  # refresh after open
        total_trades += 1
        trades_this_run += 1

        entry_label = "MR_LONG" if is_mr else signal_dir
        run_events.append(f"{symbol} ENTRY {entry_label}")
        print(f"  ✅ Entered {entry_label} @ {latest_price:.4f}")
        print(f"     Alloc=₹{allocation:.2f} | Qty={quantity:.6f} | Risk={_risk_rate:.0%} (score={_score}/5)")
        if is_mr:
            print(f"     Drop={signal_data['drop_pct']:.1f}%  RSI={signal_data['rsi']:.1f}  "
                  f"TP=+{signal_data['mr_tp_pct']*100:.0f}%  SL=-{signal_data['mr_sl_pct']*100:.0f}%  "
                  f"MaxHold={signal_data['mr_max_hours']}h  BTC_Regime={btc_regime or 'NEUTRAL'}")
        else:
            mode_label = "LONG-only" if LONG_ONLY else "LONG+SHORT"
            ct_label   = " | ⚠️  COUNTER-TREND (tighter exits)" if signal_data.get('counter_trend') else ""
            print(f"     ADX={signal_data['adx']:.1f} | RSI={signal_data['rsi']:.1f} | "
                  f"Confirmations={signal_data['soft_confirmations']}/5 | "
                  f"BTC_Regime={btc_regime or 'NEUTRAL'} | Mode={mode_label}{ct_label}")
            if signal_data.get('counter_trend'):
                print(f"     TP={effective_tp:.1%} | Trail={effective_trail:.1%} (counter-trend targets)")
            else:
                print(f"     TP={effective_tp:.1%} | Trail={effective_trail:.1%} (trend targets)")
        print()

# ---------------------------------------------------------------------------
# Portfolio snapshot
# ---------------------------------------------------------------------------

open_positions = 0
unrealized     = 0

for symbol in COINS:
    position = load_position(symbol)
    if position is not None:
        open_positions += 1

        if symbol in coins_data:
            latest_close = float(coins_data[symbol]['Close'].iloc[-1])
        else:
            fetched      = fetch_data(symbol)
            latest_close = float(fetched['Close'].iloc[-1]) if len(fetched) > 0 \
                           else float(position['Entry Price'])

        entry = float(position['Entry Price'])
        qty   = float(position['Quantity'])

        unrealized += (
            (latest_close - entry) * qty if position['Side'] == "LONG"
            else (entry - latest_close) * qty
        )

equity = cash + unrealized

log_portfolio(cash, equity, open_positions, realized_pnl, unrealized, total_trades, run_events)

print("=" * 50)
print("Portfolio Snapshot")
print("=" * 50)

allocated_capital = INITIAL_CAPITAL - cash

print(f"Allocated    : ₹{allocated_capital:.2f}")
print(f"Cash         : ₹{cash:.2f}")
print(f"Unrealized   : ₹{unrealized:.2f}")
print(f"Equity       : ₹{equity:.2f}")
print(f"Realized PnL : ₹{realized_pnl:.2f}")
print(f"BTC Regime   : {btc_regime or 'NEUTRAL'}")
print(
    f"Open         : {open_positions} | "
    f"Trades this run: {trades_this_run}"
)

print("=" * 50)

# ---------------------------------------------------------------------------
# Open Positions Summary
# ---------------------------------------------------------------------------

print()
print("=" * 50)
print("Open Positions")
print("=" * 50)

any_open = False
for symbol in COINS:
    position = load_position(symbol)
    if position is None:
        continue

    any_open        = True
    side            = position['Side']
    entry_price     = float(position['Entry Price'])
    quantity        = float(position['Quantity'])
    bars_held       = int(position.get('Bars_Held', 0))
    already_partial = bool(int(position.get('Partial_Taken', 0)))
    hwm             = float(position.get('Trail_HWM', entry_price))

    if symbol in coins_data:
        current_price = float(coins_data[symbol]['Close'].iloc[-1])
    else:
        fetched       = fetch_data(symbol)
        current_price = float(fetched['Close'].iloc[-1]) if len(fetched) > 0 \
                        else entry_price

    # Direction-aware profit %
    if side == "LONG":
        move_pct     = (current_price - entry_price) / entry_price
        peak_pct     = (hwm - entry_price) / entry_price
        reversal_pct = (hwm - current_price) / hwm if already_partial else None
    else:
        move_pct     = (entry_price - current_price) / entry_price
        peak_pct     = (entry_price - hwm) / entry_price
        reversal_pct = (current_price - hwm) / hwm if already_partial else None

    pnl = move_pct * entry_price * quantity

    # Status + reason
    if already_partial:
        gap_to_trail = abs(reversal_pct) if reversal_pct is not None else 0
        status       = "HOLDING"
        reason       = f"Reversal < {TRAILING_STOP_PCT:.1%}"
        trail_label  = f"{abs(reversal_pct):.1%} from peak" if reversal_pct is not None else "—"
    else:
        _pos_ct      = bool(int(position.get('Counter_Trend', 0)))
        _eff_tp      = COUNTER_TREND_TP_PCT if _pos_ct else PARTIAL_TAKE_PROFIT_PCT
        gap_to_tp    = _eff_tp - move_pct
        ct_tag       = " [CT]" if _pos_ct else ""
        status       = "HOLDING"
        reason       = f"TP not reached (+{gap_to_tp:.1%} to go{ct_tag})" if move_pct < _eff_tp \
                       else "TP pending"
        trail_label  = "NOT ACTIVE"

    profit_sign = "+" if move_pct >= 0 else ""
    peak_sign   = "+" if peak_pct >= 0 else ""
    pnl_sign    = "+" if pnl >= 0 else ""

    print(f"{symbol} | {side}")
    print("-" * 50)
    print(f"Entry Price        : {entry_price:.2f}")
    print(f"Current Price      : {current_price:.2f}")
    print(f"Current Profit     : {profit_sign}{move_pct:.1%}")

    # MR positions show their own TP/SL targets instead of tier info
    if int(position.get("MR_Entry", 0)) == 1:
        mr_tp  = float(position.get("MR_TP_Price", 0))
        mr_sl  = float(position.get("MR_SL_Price", 0))
        mr_ddl = str(position.get("MR_Deadline", ""))
        print(f"Type               : MEAN REVERSION")
        print(f"MR TP Target       : {mr_tp:.4f}  (+{(mr_tp/entry_price-1)*100:.1f}%)")
        print(f"MR SL Target       : {mr_sl:.4f}  (-{(1-mr_sl/entry_price)*100:.1f}%)")
        if mr_ddl:
            try:
                deadline  = datetime.fromisoformat(mr_ddl)
                now_utc   = datetime.now(timezone.utc)
                hrs_left  = (deadline - now_utc).total_seconds() / 3600
                print(f"Deadline           : {hrs_left:.1f}h remaining")
            except ValueError:
                pass
    else:
        print(f"Peak Profit        : {peak_sign}{peak_pct:.1%}")
        if already_partial and reversal_pct is not None:
            print(f"Reversal           : {abs(reversal_pct):.1%}")
        print(f"Trailing Exit      : {trail_label}")
        print(f"Partial TP         : {'YES' if already_partial else 'NO'}")
        print(f"Bars Held          : {bars_held}")
        print(f"Status             : {status}")
        print(f"Reason             : {reason}")

    print(f"PnL                : {pnl_sign}{pnl:.2f}")

if not any_open:
    print("No open positions.")

print("=" * 50)
print("Done.")