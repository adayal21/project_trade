import os
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone

from config import COINS, DATA_DIR, INITIAL_CAPITAL, RISK_PER_TRADE, STOP_LOSS_PCT, MAX_ALLOCATION, MAX_POSITIONS_PER_DIR
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
        return pd.read_csv(file).iloc[0].to_dict()
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
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=12)

    url     = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
    params  = {"granularity": 3600, "start": start.isoformat(), "end": end.isoformat()}
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, params=params, headers=headers, timeout=10)

    print(f"  URL: {response.url}")
    print(f"  HTTP Status: {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        return pd.DataFrame()

    if response.status_code != 200:
        print(f"  Coinbase error: {data}")
        return pd.DataFrame()

    if not isinstance(data, list) or len(data) == 0:
        print(f"  Empty candles for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=["time", "low", "high", "open", "close", "volume"])
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.sort_values('time')
    df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "volume": "Volume"}, inplace=True)
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
    # Stop-loss — always checked, no gate bypasses this
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
    # Counter-signal exit
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
    # Entry — three sequential gates
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

        # Gate 3: Correlation guard
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
            "Coin":        symbol,
            "Side":        signal_dir,
            "Entry Price": latest_price,
            "Quantity":    quantity,
            "Timestamp":   datetime.now(timezone.utc)
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
print(f"Cash         : ${cash:.2f}")
print(f"Unrealized   : ${unrealized:.2f}")
print(f"Equity       : ${equity:.2f}")
print(f"Realized PnL : ${realized_pnl:.2f}")
print(f"BTC Regime   : {btc_regime or 'NEUTRAL'}")
print(f"Open         : {open_positions} | Trades this run: {trades_this_run}")
print("=" * 50)
print("Done.")