import os
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from auth import make_signature
from config import API_KEY, BASE_URL, EXCHANGE

from config import (
    COINS, DATA_DIR, INITIAL_CAPITAL, RISK_PER_TRADE, STOP_LOSS_PCT,
    MAX_ALLOCATION, MAX_POSITIONS_PER_DIR,
    PARTIAL_TAKE_PROFIT_PCT, PARTIAL_EXIT_RATIO,
    TRAILING_STOP_PCT,
    MAX_HOLD_BARS, TIME_EXIT_MIN_MOVE_PCT,
    MAX_HOLD_BARS_EXTENDED, MAX_HOLD_BARS_TRAIL,
)
from strategy import apply_indicators, generate_signal
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def get_position_file(symbol: str) -> str:
    return f"{DATA_DIR}/{symbol}_position.csv"


def get_trade_file(symbol: str) -> str:
    return f"{DATA_DIR}/{symbol}_trades.csv"


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

def get_btc_regime(coins_data: dict) -> str | None:
    """
    Returns the macro market direction based on BTC's indicators.

    Migrated from Coinbase → CoinSwitch: INR-denominated BTC candles are ~2x
    noisier, which causes EMA200 and Supertrend to disagree constantly on a
    single bar, producing permanent NEUTRAL under the old binary AND gate.

    New approach — 3-indicator majority vote (need 2-of-3 to agree):
      1. EMA200     : Close vs EMA200
      2. Supertrend : bullish / bearish flag
      3. EMA50/20   : fast EMA above slow EMA (short-term momentum)

    Additionally, Supertrend is confirmed over a 2-bar window to debounce
    single-candle flips caused by CoinSwitch tick noise.

    'LONG'  → 2 or more indicators bullish
    'SHORT' → 2 or more indicators bearish
    None    → split opinion; regime unclear
    """
    btc_df = coins_data.get("BTC/INR")
    if btc_df is None or len(btc_df) < 3:
        return None

    latest = btc_df.iloc[-1]
    prev   = btc_df.iloc[-2]

    required = ['EMA200', 'Supertrend']
    if any(pd.isna(latest.get(col)) for col in required):
        return None

    # --- Indicator 1: EMA200 ---
    ema200_bull = latest['Close'] > latest['EMA200']
    ema200_bear = latest['Close'] < latest['EMA200']

    # --- Indicator 2: Supertrend (2-bar confirmation to debounce noise) ---
    st_bull = bool(latest['Supertrend']) and bool(prev['Supertrend'])
    st_bear = (not bool(latest['Supertrend'])) and (not bool(prev['Supertrend']))

    # --- Indicator 3: EMA50 vs EMA20 short-term momentum ---
    ema20  = btc_df['Close'].ewm(span=20,  adjust=False).mean().iloc[-1]
    ema50  = btc_df['Close'].ewm(span=50,  adjust=False).mean().iloc[-1]
    ema_fast_bull = ema20 > ema50
    ema_fast_bear = ema20 < ema50

    bull_score = int(ema200_bull) + int(st_bull) + int(ema_fast_bull)
    bear_score = int(ema200_bear) + int(st_bear) + int(ema_fast_bear)

    if bull_score >= 2:
        return "LONG"
    if bear_score >= 2:
        return "SHORT"

    return None  # indicators split — no clear regime


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_data(symbol: str) -> pd.DataFrame:

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    start_ms = end_ms - (20 * 24 * 60 * 60 * 1000)

    endpoint = "/trade/api/v2/candles"

    params = {
        "exchange": EXCHANGE,
        "symbol": symbol,
        "interval": "60",
        "start_time": str(start_ms),
        "end_time": str(end_ms),
    }

    endpoint, epoch_time, signature = make_signature(
        "GET",
        endpoint,
        params
    )

    url = BASE_URL + endpoint

    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH": epoch_time,
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=10
    )

    # print(f"  URL: {url}")
    # print(f"  HTTP Status: {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        return pd.DataFrame()

    if response.status_code != 200:
        print(f"  CoinSwitch error: {data}")
        return pd.DataFrame()

    candles = data["data"]

    if len(candles) == 0:
        print(f"  Empty candles for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(candles)

    df.rename(columns={
        "o": "Open",
        "h": "High",
        "l": "Low",
        "c": "Close",
        "volume": "Volume",
        "start_time": "time"
    }, inplace=True)

    df["time"] = pd.to_datetime(
        pd.to_numeric(df["time"]),
        unit="ms"
    )

    numeric_cols = ["Open", "High", "Low", "Close", "Volume"]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col])

    df = df.sort_values("time")

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
    if len(df) < 200:   # lowered from 250: CoinSwitch returns ~480 bars per 20-day window
        print(f"  Not enough data ({len(df)} bars), skipping.")
        continue
    coins_data[symbol] = apply_indicators(df)

# ------------------------------------------------------------------
# Step 2: Determine BTC regime — single gate for all altcoin entries.
# ------------------------------------------------------------------

btc_regime = get_btc_regime(coins_data)
print(f"\n{'=' * 50}")
print(f"BTC Regime : {btc_regime or 'NEUTRAL (mixed — altcoin entries blocked)'}")
print(f"{'=' * 50}")

# ------------------------------------------------------------------
# [DIAG] BTC regime vote breakdown — temporary verbose diagnostics
# Remove this block once CoinSwitch behaviour is validated.
# ------------------------------------------------------------------
btc_df = coins_data.get("BTC/INR")
if btc_df is not None and len(btc_df) >= 3:
    _latest = btc_df.iloc[-1]
    _prev   = btc_df.iloc[-2]
    _ema20  = btc_df["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
    _ema50  = btc_df["Close"].ewm(span=50, adjust=False).mean().iloc[-1]

    _v1_bull = _latest["Close"] > _latest["EMA200"]
    _v2_bull = bool(_latest["Supertrend"]) and bool(_prev["Supertrend"])
    _v3_bull = _ema20 > _ema50

    _v1_bear = _latest["Close"] < _latest["EMA200"]
    _v2_bear = (not bool(_latest["Supertrend"])) and (not bool(_prev["Supertrend"]))
    _v3_bear = _ema20 < _ema50

    _bull_score = int(_v1_bull) + int(_v2_bull) + int(_v3_bull)
    _bear_score = int(_v1_bear) + int(_v2_bear) + int(_v3_bear)

    print(f"[DIAG] BTC/INR Regime Votes:")
    print(f"  Close       : {_latest['Close']:.2f}")
    print(f"  EMA200      : {_latest['EMA200']:.2f}  ->  Vote1 BULL={_v1_bull} BEAR={_v1_bear}")
    print(f"  Supertrend  : now={bool(_latest['Supertrend'])} prev={bool(_prev['Supertrend'])}  ->  Vote2 BULL={_v2_bull} BEAR={_v2_bear}")
    print(f"  EMA20/EMA50 : {_ema20:.2f} / {_ema50:.2f}  ->  Vote3 BULL={_v3_bull} BEAR={_v3_bear}")
    print(f"  Bull Score  : {_bull_score}/3  |  Bear Score : {_bear_score}/3  ->  Regime={btc_regime or 'NEUTRAL'}")
    print(f"  ADX         : {_latest['ADX']:.2f}  |  RSI={_latest['RSI']:.2f}  |  ATR={_latest['ATR']:.2f}  |  ATR_SMA={_latest['ATR_SMA']:.2f}")
    print(f"  ST_Upper    : {_latest['Supertrend_Upper']:.2f}  |  ST_Lower={_latest['Supertrend_Lower']:.2f}")
print()

# ------------------------------------------------------------------
# Step 3: Process each coin.
# ------------------------------------------------------------------

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
    print(f"  [DIAG] Volume={_row['Volume']:.2f}  Vol_SMA={_row['Volume_SMA']:.2f}  "
          f"Vol_OK={_row['Volume'] > _row['Volume_SMA']}")
    print(f"  [DIAG] Signal={signal_dir or 'NONE'}")
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

            clear_position(symbol)
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

        if not already_partial and move_pct >= PARTIAL_TAKE_PROFIT_PCT:
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
                trail_breach = latest_price < hwm * (1 - TRAILING_STOP_PCT)
            else:
                hwm = min(hwm, latest_price)
                trail_breach = latest_price > hwm * (1 + TRAILING_STOP_PCT)

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

                clear_position(symbol)
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

            clear_position(symbol)
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

        # 4A — truly stagnant
        if not tier4_exit and bars_held >= MAX_HOLD_BARS and abs(move_pct) < TIME_EXIT_MIN_MOVE_PCT:
            tier4_exit   = True
            tier4_reason = f"TIME_EXIT_STAGNANT ({bars_held} bars, move={move_pct:.2%})"

        # 4B — stuck below partial-profit target (Tier 1 never triggered)
        if not tier4_exit and not already_partial \
                and bars_held >= MAX_HOLD_BARS_EXTENDED \
                and 0 < move_pct < PARTIAL_TAKE_PROFIT_PCT:
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

            clear_position(symbol)
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

        # Gate 2: BTC regime filter
        # BTC/INR bypasses this — it is the regime reference itself
        if symbol != "BTC/INR":
            if btc_regime is None:
                print(f"  🚫 BTC regime NEUTRAL — entry blocked.")
                print()
                continue
            if btc_regime != signal_dir:
                print(f"  🚫 BTC regime mismatch — signal={signal_dir}, BTC={btc_regime}. Blocked.")
                print()
                continue

        # Gate 3: Correlation guard (Tier 3 — now allows up to MAX_POSITIONS_PER_DIR=2)
        dir_counts = count_open_by_direction()
        if dir_counts[signal_dir] >= MAX_POSITIONS_PER_DIR:
            print(f"  🚫 Correlation guard — already {dir_counts[signal_dir]} "
                  f"{signal_dir}(s) open (max={MAX_POSITIONS_PER_DIR}). Blocked.")
            print()
            continue

        # Capital guard
        allocation = cash * RISK_PER_TRADE
        if allocation > cash * MAX_ALLOCATION:
            print(f"  🚫 Capital guard — ₹{allocation:.2f} exceeds safe limit.")
            print()
            continue
        if allocation <= 0:
            print(f"  🚫 No cash available.")
            print()
            continue

        quantity = allocation / latest_price

        save_position(symbol, {
            "Coin":          symbol,
            "Side":          signal_dir,
            "Entry Price":   latest_price,
            "Quantity":      quantity,
            "Timestamp":     datetime.now(timezone.utc),
            "Partial_Taken": 0,           # Tier 1 not yet fired
            "Trail_HWM":     latest_price, # Tier 2 high-water mark seed
            "Bars_Held":     0,            # Tier 4 counter
        })

        cash -= allocation

        print(f"  ✅ Entered {signal_dir} @ {latest_price:.4f}")
        print(f"     Alloc=₹{allocation:.2f} | Qty={quantity:.6f}")
        print(f"     ADX={signal_data['adx']:.1f} | RSI={signal_data['rsi']:.1f} | "
              f"Confirmations={signal_data['soft_confirmations']}/2 | "
              f"BTC_Regime={btc_regime}")

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
        gap_to_tp    = PARTIAL_TAKE_PROFIT_PCT - move_pct
        status       = "HOLDING"
        reason       = f"TP not reached (+{gap_to_tp:.1%} to go)" if move_pct < PARTIAL_TAKE_PROFIT_PCT \
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