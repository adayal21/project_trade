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
    Returns the macro market direction based on BTC's EMA200 + Supertrend.

    'LONG'  → BTC above EMA200 and Supertrend bullish
    'SHORT' → BTC below EMA200 and Supertrend bearish
    None    → indicators are mixed; regime is unclear

    Uses already-fetched BTC data to avoid a redundant API call.
    """
    btc_df = coins_data.get("BTC-USD")
    if btc_df is None or len(btc_df) == 0:
        return None

    latest = btc_df.iloc[-1]

    if pd.isna(latest.get('EMA200')) or pd.isna(latest.get('Supertrend')):
        return None

    if latest['Close'] > latest['EMA200'] and latest['Supertrend']:
        return "LONG"
    if latest['Close'] < latest['EMA200'] and not latest['Supertrend']:
        return "SHORT"

    return None  # EMA and Supertrend disagree — no clear regime


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
    if len(df) < 250:
        print(f"  Not enough data ({len(df)} bars), skipping.")
        continue
    coins_data[symbol] = apply_indicators(df)

# ------------------------------------------------------------------
# Step 2: Determine BTC regime — single gate for all altcoin entries.
# ------------------------------------------------------------------

btc_regime = get_btc_regime(coins_data)
print(f"\n{'=' * 50}")
print(f"BTC Regime : {btc_regime or 'NEUTRAL (mixed — altcoin entries blocked)'}")
print(f"{'=' * 50}\n")

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
    latest_price = float(df['Close'].iloc[-1])
    position     = load_position(symbol)

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
        # BTC-USD bypasses this — it is the regime reference itself
        if symbol != "BTC-USD":
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
            print(f"  🚫 Capital guard — ${allocation:.2f} exceeds safe limit.")
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
        print(f"     Alloc=${allocation:.2f} | Qty={quantity:.6f}")
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

print(f"Allocated    : ${allocated_capital:.2f}")
print(f"Cash         : ${cash:.2f}")
print(f"Unrealized   : ${unrealized:.2f}")
print(f"Equity       : ${equity:.2f}")
print(f"Realized PnL : ${realized_pnl:.2f}")
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