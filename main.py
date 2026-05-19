import os
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone

from config import (
    TRADING_MODE,
    COINS, DATA_DIR, INITIAL_CAPITAL, RISK_PER_TRADE, STOP_LOSS_PCT,
    MAX_ALLOCATION, MAX_POSITIONS_PER_DIR,
    PARTIAL_TAKE_PROFIT_PCT, PARTIAL_EXIT_RATIO,
    TRAILING_STOP_PCT,
    COUNTER_TREND_TP_PCT, COUNTER_TREND_TRAIL_PCT,
    MAX_HOLD_BARS, MAX_HOLD_BARS_LOSING, TIME_EXIT_MIN_MOVE_PCT,
    MAX_HOLD_BARS_EXTENDED, MAX_HOLD_BARS_TRAIL,
    RSI_RESET_SHORT, RSI_RESET_LONG,
    LONG_ONLY, REGIME_ALLOWS_LONG_IN_NEUTRAL, REGIME_OVERRIDE_MIN_SCORE,
)
from strategy import apply_indicators, generate_signal
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)

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
    Partial-profit rows are skipped — they are not failed trades."""
    file = get_trade_file(symbol)
    if not os.path.exists(file):
        return None
    df = pd.read_csv(file)
    full_closes = df[df["Exit Reason"] != "PARTIAL_PROFIT"]
    if full_closes.empty:
        return None
    return full_closes.iloc[-1].to_dict()


# ---------------------------------------------------------------------------
# RSI reset check — re-entry filter after losing trades
# ---------------------------------------------------------------------------
# After a losing trade, the market must prove momentum genuinely resumed
# before re-entry in the same direction. Prevents the same weak signal
# from re-firing immediately after it failed.
#
#   Losing SHORT exit → require RSI < RSI_RESET_SHORT (45) AND RSI falling
#   Losing LONG exit  → require RSI > RSI_RESET_LONG  (55) AND RSI rising
# ---------------------------------------------------------------------------

def rsi_reset_allows_entry(symbol: str, signal_dir: str, df: pd.DataFrame) -> tuple[bool, str]:
    last = get_last_trade(symbol)
    if last is None:
        return True, ""

    last_side = str(last.get("Side", ""))
    last_pnl  = float(last.get("PnL", 0))

    # Filter only applies when last full-close was same direction AND a loss
    if last_side != signal_dir or last_pnl >= 0:
        return True, ""

    latest_rsi = float(df.iloc[-1]["RSI"])
    prev_rsi   = float(df.iloc[-2]["RSI"])
    arrow      = "↓" if latest_rsi < prev_rsi else "↑"

    if signal_dir == "SHORT":
        if latest_rsi < RSI_RESET_SHORT and latest_rsi < prev_rsi:
            return True, ""
        return False, (
            f"RSI reset required after losing SHORT "
            f"(need RSI<{RSI_RESET_SHORT} & falling, got RSI={latest_rsi:.1f}{arrow})"
        )

    if signal_dir == "LONG":
        if latest_rsi > RSI_RESET_LONG and latest_rsi > prev_rsi:
            return True, ""
        return False, (
            f"RSI reset required after losing LONG "
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
    print(f"\nFetching {symbol}")
    df = fetch_data(symbol)
    if len(df) < 200:   # EMA200 needs 200 bars of warm-up
        print(f"  Not enough data ({len(df)} bars), skipping.")
        continue
    coins_data[symbol] = apply_indicators(df)
    # Raw volume distribution — sanity-check the data feed.
    v = df['Volume']
    print(f"  [VOL DIAG] {symbol}: min={v.min():.4f}  median={v.median():.4f}  "
          f"max={v.max():.4f}  last={v.iloc[-1]:.4f}")

# ------------------------------------------------------------------
# Step 2: Determine BTC regime — single gate for all altcoin entries.
# ------------------------------------------------------------------

btc_regime, btc_breakdown = get_btc_regime(coins_data)
print(f"\n{'=' * 50}")
print(f"BTC Regime : {btc_regime or 'NEUTRAL (mixed — altcoin entries blocked)'}")
print(f"{'=' * 50}")

if btc_breakdown:
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

# ------------------------------------------------------------------
# Step 3: Process each coin.
# Cache open-position direction counts once — avoids O(N²) disk reads
# (count_open_by_direction reads every coin's position file; calling it
# inside the per-coin loop would re-read N files for each of N coins).
# Refreshed after any trade that opens or closes a position.
# ------------------------------------------------------------------

dir_counts_cache = count_open_by_direction()

for symbol in COINS:
    print(f"--- {symbol} ---")

    if symbol not in coins_data:
        print(f"  Skipped (no data).\n")
        continue

    df           = coins_data[symbol]
    signal_data  = generate_signal(df)
    signal_dir   = signal_data["signal"] if signal_data else None
    latest_price = float(df["Close"].iloc[-1])
    position     = load_position(symbol)

    # Determine effective TP and trail targets.
    # Counter-trend LONGs (bounces below EMA200) use tighter targets
    # so we bank profit quickly before the macro trend reasserts.
    # The flag is stored on the position so exits can reference it.
    is_counter_trend = (
        bool(int(position.get('Counter_Trend', 0))) if position is not None
        else (signal_data.get('counter_trend', False) if signal_data else False)
    )
    effective_tp    = COUNTER_TREND_TP_PCT    if is_counter_trend else PARTIAL_TAKE_PROFIT_PCT
    effective_trail = COUNTER_TREND_TRAIL_PCT if is_counter_trend else TRAILING_STOP_PCT

    # ------------------------------------------------------------------
    # [DIAG] Per-coin indicator snapshot — temporary verbose diagnostics
    # ------------------------------------------------------------------
    _row = df.iloc[-1]
    _atr_ratio = (_row["ATR"] / _row["ATR_SMA"]) if _row["ATR_SMA"] > 0 else 0
    print(f"  [DIAG] Price={latest_price:.4f}  EMA200={_row['EMA200']:.4f}  "
          f"Close>EMA200={latest_price > _row['EMA200']}")
    print(f"  [DIAG] RSI={_row['RSI']:.2f}  ADX={_row['ADX']:.2f}(thr=18)  "
          f"Supertrend={'BULL' if _row['Supertrend'] else 'BEAR'}")
    print(f"  [DIAG] ATR={_row['ATR']:.4f}  ATR_SMA={_row['ATR_SMA']:.4f}  "
          f"Ratio={_atr_ratio:.2f}(thr=0.50)  "
          f"ATR_OK={_atr_ratio >= 0.50}")
    print(f"  [DIAG] Volume={_row['Volume']:.2f}  Vol_Baseline={_row['Volume_Baseline']:.2f}  "
          f"Vol_OK={_row['Volume'] > _row['Volume_Baseline']} (median-based)")
    print(f"  [DIAG] EMA50={_row['EMA50']:.4f}  Close>EMA50={latest_price > _row['EMA50']}")
    if signal_data:
        ct = signal_data.get('counter_trend', False)
        print(f"  [DIAG] Signal={signal_dir}  CounterTrend={ct}  "
              f"Score={signal_data['soft_confirmations']}/4  "
              f"(RSI={signal_data.get('s1_rsi',False)} "
              f"Vol={signal_data.get('s2_volume',False)} "
              f"EMA200={signal_data.get('s3_ema200',False)} "
              f"EMA50={signal_data.get('s4_ema50',False)})")
    else:
        print(f"  [DIAG] Signal=NONE")
    if signal_data is None and _row["ADX"] < 18.0:
        print(f"  [DIAG] Blocked by: ADX too low ({_row['ADX']:.2f} < 18.0)")
    if signal_data is None and _atr_ratio < 0.50:
        print(f"  [DIAG] Blocked by: ATR compression ({_atr_ratio:.2f} < 0.50)")

    # -------------------------------------------------------------------
    # Stop-loss — always checked first, no gate bypasses this
    # Applies to the full remaining quantity (whether or not Tier 1 fired)
    # -------------------------------------------------------------------
    if position is not None:
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
    # Tier 1 — Partial profit-taking at +PARTIAL_TAKE_PROFIT_PCT
    # Closes PARTIAL_EXIT_RATIO of the position, banks that cash,
    # and marks the position as partially exited so Tier 2 can activate.
    # -------------------------------------------------------------------
    if position is not None:
        side             = position['Side']
        entry_price      = float(position['Entry Price'])
        quantity         = float(position['Quantity'])
        already_partial  = bool(int(position.get('Partial_Taken', 0)))

        move_pct = (
            (latest_price - entry_price) / entry_price if side == "LONG"
            else (entry_price - latest_price) / entry_price
        )

        if not already_partial and move_pct >= effective_tp:
            exit_qty      = quantity * PARTIAL_EXIT_RATIO
            remain_qty    = quantity - exit_qty
            partial_pnl   = move_pct * entry_price * exit_qty

            realized_pnl    += partial_pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += entry_price * exit_qty + partial_pnl

            print(f"  💰 TIER 1 partial exit ({move_pct:.2%} gain) | "
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

            # Live mode: sell the partial exit quantity
            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", exit_qty)

            # Update the position: smaller quantity, Tier 1 done,
            # seed the trailing high-water mark at current price
            save_position(symbol, {
                "Coin":          symbol,
                "Side":          side,
                "Entry Price":   entry_price,
                "Quantity":      remain_qty,
                "Timestamp":     position['Timestamp'],
                "Partial_Taken": 1,
                "Trail_HWM":     latest_price,   # Tier 2 starts tracking from here
                "Bars_Held":     int(position.get('Bars_Held', 0)),
            })
            position = load_position(symbol)   # reload updated state

    # -------------------------------------------------------------------
    # Tier 2 — Trailing stop on the remaining position
    # Only active after Tier 1 has fired (Partial_Taken == 1).
    # Updates the high-water mark each bar and exits if price drops
    # TRAILING_STOP_PCT below the peak.
    # -------------------------------------------------------------------
    if position is not None:
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
                "Trail_HWM":     hwm,
                "Bars_Held":     int(position.get('Bars_Held', 0)),
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
    # Tier 4 — Time-based exit backstop (three sub-cases)
    #
    #   4A  Stagnant          : bars >= MAX_HOLD_BARS  AND |move| < 0.5%
    #                           → price has gone nowhere; cut and free capital
    #
    #   4B  Stuck-profitable  : bars >= MAX_HOLD_BARS_EXTENDED AND 0 < move < +2%
    #                           (Tier 1 never fired) → take the small gain rather
    #                           than waiting forever for a target that may never come
    #
    #   4C  Trailing timeout  : Tier 1 already fired AND bars >= MAX_HOLD_BARS_TRAIL
    #                           → remaining half has drifted long enough; exit it
    #
    # Bars_Held is incremented every run and persisted in the position file.
    # -------------------------------------------------------------------
    if position is not None:
        side            = position['Side']
        entry_price     = float(position['Entry Price'])
        quantity        = float(position['Quantity'])
        already_partial = bool(int(position.get('Partial_Taken', 0)))
        bars_held       = int(position.get('Bars_Held', 0)) + 1  # increment this run

        move_pct = (
            (latest_price - entry_price) / entry_price if side == "LONG"
            else (entry_price - latest_price) / entry_price
        )

        tier4_exit    = False
        tier4_reason  = ""

        # 4A — truly stagnant: held long enough with no meaningful move either way
        if not tier4_exit and bars_held >= MAX_HOLD_BARS and abs(move_pct) < TIME_EXIT_MIN_MOVE_PCT:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_STAGNANT ({bars_held} bars, move={move_pct:.2%})"

        # 4D — losing position timeout: signal thesis failed, cut before stop-loss
        # Fires after just MAX_HOLD_BARS_LOSING (3h) of continuous loss — no point
        # waiting 6 bars when the trade has been underwater since entry.
        # Only fires when Tier 1 hasn't triggered — once partial profit was taken
        # the trade was validated; let trailing stop manage the remainder instead.
        if not tier4_exit and not already_partial \
                and bars_held >= MAX_HOLD_BARS_LOSING \
                and move_pct < 0:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_LOSING ({bars_held} bars, move={move_pct:.2%})"

        # 4B — stuck below partial-profit target (Tier 1 never triggered)
        if not tier4_exit and not already_partial \
                and bars_held >= MAX_HOLD_BARS_EXTENDED \
                and 0 < move_pct < effective_tp:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_STUCK_PROFIT ({bars_held} bars, move={move_pct:.2%})"

        # 4C — trailing half timeout (Tier 1 already fired)
        if not tier4_exit and already_partial and bars_held >= MAX_HOLD_BARS_TRAIL:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_TRAIL_TIMEOUT ({bars_held} bars)"

        if tier4_exit:
            pnl = move_pct * entry_price * quantity

            realized_pnl    += pnl
            total_trades    += 1
            trades_this_run += 1
            cash            += entry_price * quantity + pnl

            print(f"  ⏱️  TIER 4 exit | {tier4_reason} | PnL: {pnl:.2f}")

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         round(pnl, 4),
                "Exit Reason": tier4_reason.split(" (")[0],   # clean label for CSV
                "Exit Time":   datetime.now(timezone.utc)
            })

            if TRADING_MODE == "live":
                from exchange import place_market_order
                place_market_order(symbol, "sell", quantity)
            clear_position(symbol)
            dir_counts_cache = count_open_by_direction()  # refresh after close
            position = None
        else:
            # Not exiting yet — persist updated bars_held counter
            save_position(symbol, {
                "Coin":          symbol,
                "Side":          side,
                "Entry Price":   entry_price,
                "Quantity":      quantity,
                "Timestamp":     position['Timestamp'],
                "Partial_Taken": int(position.get('Partial_Taken', 0)),
                "Trail_HWM":     float(position.get('Trail_HWM', entry_price)),
                "Bars_Held":     bars_held,
            })

    # -------------------------------------------------------------------
    # Entry — three sequential gates (Tier 3: MAX_POSITIONS_PER_DIR = 2)
    # -------------------------------------------------------------------
    if position is None and signal_dir is not None:

        # Gate 1 (implicit): position is None — coin is flat ✓

        # Gate 1.5: RSI reset filter — blocks re-entry after a losing trade
        # until RSI has materially reset in the signal direction.
        rsi_ok, rsi_block_reason = rsi_reset_allows_entry(symbol, signal_dir, df)
        if not rsi_ok:
            print(f"  🚫 RSI reset: {rsi_block_reason}")
            print()
            continue

        # Gate 2: BTC regime filter
        # BTC/INR bypasses this — it is the regime reference itself.
        #
        # In LONG_ONLY mode:
        #   - BTC LONG    → allow
        #   - BTC NEUTRAL → allow (REGIME_ALLOWS_LONG_IN_NEUTRAL=True)
        #   - BTC SHORT   → normally block, BUT allow if the coin's own score
        #                   >= REGIME_OVERRIDE_MIN_SCORE (3/4) AND not counter-trend.
        #                   A coin scoring 3+ with Supertrend bull and above its own
        #                   EMA200 is showing genuine independent momentum.
        #
        # In LONG+SHORT mode: original strict behaviour unchanged.
        if symbol != "BTC/INR":
            if LONG_ONLY:
                if btc_regime == "SHORT":
                    # Check for high-conviction independent override
                    coin_score    = signal_data.get('soft_confirmations', 0)
                    is_ct         = signal_data.get('counter_trend', True)
                    regime_override = (
                        coin_score >= REGIME_OVERRIDE_MIN_SCORE and not is_ct
                    )
                    if regime_override:
                        print(f"  ⚡ BTC regime SHORT overridden — "
                              f"coin score {coin_score}/4, not counter-trend.")
                    else:
                        print(f"  🚫 BTC regime SHORT — LONG entry blocked in bear market.")
                        print()
                        continue
                if btc_regime is None and not REGIME_ALLOWS_LONG_IN_NEUTRAL:
                    print(f"  🚫 BTC regime NEUTRAL — entry blocked (REGIME_ALLOWS_LONG_IN_NEUTRAL=False).")
                    print()
                    continue
            else:
                # Original strict behaviour: any mismatch or NEUTRAL blocks
                if btc_regime is None:
                    print(f"  🚫 BTC regime NEUTRAL — entry blocked.")
                    print()
                    continue
                if btc_regime != signal_dir:
                    print(f"  🚫 BTC regime mismatch — signal={signal_dir}, BTC={btc_regime}. Blocked.")
                    print()
                    continue

        # Gate 3: Correlation guard (Tier 3 — now allows up to MAX_POSITIONS_PER_DIR=2)
        dir_counts = dir_counts_cache
        if dir_counts[signal_dir] >= MAX_POSITIONS_PER_DIR:
            print(f"  🚫 Correlation guard — already {dir_counts[signal_dir]} "
                  f"{signal_dir}(s) open (max={MAX_POSITIONS_PER_DIR}). Blocked.")
            print()
            continue

        # Capital guard
        allocation = cash * RISK_PER_TRADE
        total_capital = cash + sum(
            float(load_position(s)['Entry Price']) * float(load_position(s)['Quantity'])
            for s in COINS
            if load_position(s) is not None
        )
        deployed = total_capital - cash
        if deployed / total_capital >= MAX_ALLOCATION:
            print(f"  🚫 Capital guard — {deployed/total_capital:.0%} deployed exceeds MAX_ALLOCATION={MAX_ALLOCATION:.0%}.")
            print()
            continue
        if allocation <= 0:
            print(f"  🚫 No cash available.")
            print()
            continue

        quantity = allocation / latest_price

        is_ct = signal_data.get('counter_trend', False)

        # Live mode: place real buy order BEFORE saving position.
        # If the order fails, we do NOT save the position — no phantom trades.
        if TRADING_MODE == "live":
            from exchange import place_market_order, get_order_status
            order_id = place_market_order(symbol, "buy", quantity)
            if order_id is None:
                print(f"  ✗ Live order failed — skipping position save.")
                print()
                continue
            # Wait briefly then confirm fill and get actual fill price
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
            # Use actual fill price and quantity, not the signal price
            latest_price = fill_price
            quantity     = fill_qty

        save_position(symbol, {
            "Coin":          symbol,
            "Side":          signal_dir,
            "Entry Price":   latest_price,
            "Quantity":      quantity,
            "Timestamp":     datetime.now(timezone.utc),
            "Partial_Taken": 0,           # Tier 1 not yet fired
            "Trail_HWM":     latest_price, # Tier 2 high-water mark seed
            "Bars_Held":     0,            # Tier 4 counter
            "Counter_Trend": int(is_ct),  # 1=bounce below EMA200, 0=trend long
        })

        cash -= allocation
        dir_counts_cache = count_open_by_direction()  # refresh after open

        print(f"  ✅ Entered {signal_dir} @ {latest_price:.4f}")
        print(f"     Alloc=₹{allocation:.2f} | Qty={quantity:.6f}")
        mode_label = "LONG-only" if LONG_ONLY else "LONG+SHORT"
        ct_label   = " | ⚠️  COUNTER-TREND (tighter exits)" if signal_data.get('counter_trend') else ""
        print(f"     ADX={signal_data['adx']:.1f} | RSI={signal_data['rsi']:.1f} | "
              f"Confirmations={signal_data['soft_confirmations']}/4 | "
              f"BTC_Regime={btc_regime or 'NEUTRAL'} | Mode={mode_label}{ct_label}")
        if signal_data.get('counter_trend'):
            print(f"     TP={effective_tp:.1%} | Trail={effective_trail:.1%} (counter-trend targets)")
        else:
            print(f"     TP={effective_tp:.1%} | Trail={effective_trail:.1%} (trend targets)")

    elif signal_dir is None and position is None:
        print(f"  No signal.")

    elif position is not None and signal_dir == position['Side']:
        print(f"  Holding {position['Side']} (signal agrees, no action).")

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

log_portfolio(cash, equity, open_positions, realized_pnl, unrealized, total_trades)

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