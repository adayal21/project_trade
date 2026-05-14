import os
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone

from config import COINS, DATA_DIR, INITIAL_CAPITAL, RISK_PER_TRADE
from strategy import apply_indicators, generate_signal
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STOP_LOSS_PCT   = 0.04   # 4 % adverse move closes the position
MAX_ALLOCATION  = 0.80   # never deploy more than 80 % of cash across all trades

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
# Data fetching
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

def fetch_data(symbol: str) -> pd.DataFrame:
    end = datetime.now(timezone.utc)

    # Coinbase has max candle limits
    # 300 hourly candles ≈ 12 days
    start = end - timedelta(days=12)

    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"

    params = {
        "granularity": 3600,
        "start": start.isoformat(),
        "end": end.isoformat()
    }

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=10
    )

    print(f"  URL: {response.url}")
    print(f"  HTTP Status: {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        return pd.DataFrame()

    if response.status_code != 200:
        print(f"  Coinbase error response: {data}")
        return pd.DataFrame()

    if not isinstance(data, list) or len(data) == 0:
        print(f"  Coinbase returned empty candles for {symbol}")
        return pd.DataFrame()

    columns = [
        "time",
        "low",
        "high",
        "open",
        "close",
        "volume"
    ]

    df = pd.DataFrame(data, columns=columns)

    df['time'] = pd.to_datetime(df['time'], unit='s')

    df = df.sort_values('time')

    df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume"
    }, inplace=True)

    df.set_index("time", inplace=True)

    return df

# ---------------------------------------------------------------------------
# Portfolio state — load persisted cash so restarts don't reset it
# ---------------------------------------------------------------------------
def load_portfolio_state():
    """
    Load cumulative portfolio state from the latest portfolio row.
    """

    
    portfolio_file = f"{DATA_DIR}/portfolio.csv"

    if os.path.exists(portfolio_file):

        df = pd.read_csv(portfolio_file)

        if len(df) > 0:

            latest = df.iloc[-1]

            return {
                "cash": float(latest['Cash']),
                "realized_pnl": float(latest['Realized PnL']),
                "total_trades": int(latest['Total Trades'])
            }

    return {
        "cash": INITIAL_CAPITAL,
        "realized_pnl": 0.0,
        "total_trades": 0
    }


def load_cash() -> float:
    """Read the most recent cash balance from the portfolio log."""
    portfolio_file = f"{DATA_DIR}/portfolio.csv"
    if os.path.exists(portfolio_file):
        df = pd.read_csv(portfolio_file)
        if len(df) > 0:
            return float(df.iloc[-1]['Cash'])
    return INITIAL_CAPITAL


def count_open_positions() -> int:
    return sum(1 for s in COINS if load_position(s) is not None)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

initialize_portfolio()

state = load_portfolio_state()

cash = state['cash']
realized_pnl = state['realized_pnl']
total_trades = state['total_trades']


for symbol in COINS:
    print(f"\nChecking {symbol}")

    df = fetch_data(symbol)

    if len(df) < 250:
        print(f"  Not enough data ({len(df)} bars), skipping.")
        continue

    df            = apply_indicators(df)
    signal_data   = generate_signal(df)                  # dict or None
    signal_dir    = signal_data["signal"] if signal_data else None
    latest_price  = float(df['Close'].iloc[-1])
    position      = load_position(symbol)

    # -----------------------------------------------------------------------
    # Check stop-loss on existing position first
    # -----------------------------------------------------------------------
    if position is not None:
        side        = position['Side']          # "LONG" or "SHORT"
        entry_price = float(position['Entry Price'])
        quantity    = float(position['Quantity'])

        if side == "LONG":
            move_pct = (latest_price - entry_price) / entry_price
        else:
            move_pct = (entry_price - latest_price) / entry_price

        stop_hit = move_pct <= -STOP_LOSS_PCT

        if stop_hit:
            pnl = move_pct * entry_price * quantity
            print(f"  STOP-LOSS hit on {symbol} ({move_pct:.2%}), closing.")

            realized_pnl += pnl
            total_trades += 1
            position_value = entry_price * quantity
            cash += position_value + pnl

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         pnl,
                "Exit Reason": "STOP_LOSS",
                "Exit Time":   datetime.now(timezone.utc)
            })

            clear_position(symbol)
            position = None   # fall through to entry check below

    # -----------------------------------------------------------------------
    # Entry — only if flat and a signal fired
    # -----------------------------------------------------------------------
    if position is None and signal_dir is not None:

        # Capital guard: don't over-allocate
        max_spend   = cash * MAX_ALLOCATION
        allocation  = cash * RISK_PER_TRADE

        if allocation > max_spend:
            print(f"  Capital guard: allocation ${allocation:.2f} exceeds safe limit, skipping.")
            continue

        if allocation <= 0:
            print(f"  No cash available, skipping.")
            continue

        quantity = allocation / latest_price

        save_position(symbol, {
            "Coin":        symbol,
            "Side":        signal_dir,           # "LONG" or "SHORT" string
            "Entry Price": latest_price,
            "Quantity":    quantity,
            "Timestamp":   datetime.now(timezone.utc)
        })

        cash -= allocation

        print(f"  Entered {signal_dir} on {symbol} @ {latest_price:.4f}")
        if signal_data:
            print(f"  ADX={signal_data['adx']:.1f}  RSI={signal_data['rsi']:.1f}"
                  f"  Confirmations={signal_data['soft_confirmations']}/2")

    # -----------------------------------------------------------------------
    # Exit on counter-signal (position still open, opposite signal fires)
    # -----------------------------------------------------------------------
    elif position is not None and signal_dir is not None:

        side        = position['Side']
        entry_price = float(position['Entry Price'])
        quantity    = float(position['Quantity'])

        counter_signal = (side == "LONG" and signal_dir == "SHORT") or \
                         (side == "SHORT" and signal_dir == "LONG")

        if counter_signal:
            if side == "LONG":
                pnl = (latest_price - entry_price) * quantity
            else:
                pnl = (entry_price - latest_price) * quantity

            realized_pnl += pnl
            total_trades += 1
            position_value = entry_price * quantity
            cash += position_value + pnl

            print(f"  Exited {symbol} on counter-signal | PnL: {pnl:.2f}")

            log_trade(symbol, {
                "Coin":        symbol,
                "Side":        side,
                "Entry Price": entry_price,
                "Exit Price":  latest_price,
                "Quantity":    quantity,
                "PnL":         pnl,
                "Exit Reason": "COUNTER_SIGNAL",
                "Exit Time":   datetime.now(timezone.utc)
            })

            clear_position(symbol)

# ---------------------------------------------------------------------------
# Portfolio snapshot
# ---------------------------------------------------------------------------

open_positions = 0
unrealized     = 0

for symbol in COINS:
    position = load_position(symbol)
    if position is not None:
        open_positions += 1
        latest = float(fetch_data(symbol)['Close'].iloc[-1])
        entry  = float(position['Entry Price'])
        qty    = float(position['Quantity'])
        if position['Side'] == "LONG":
            unrealized += (latest - entry) * qty
        else:
            unrealized += (entry - latest) * qty

equity = cash + unrealized

log_portfolio(cash, equity, open_positions, realized_pnl, unrealized, total_trades)

print(f"\n--- Portfolio Snapshot ---")
print(f"Cash:        ${cash:.2f}")
print(f"Unrealized:  ${unrealized:.2f}")
print(f"Equity:      ${equity:.2f}")
print(f"Realized:    ${realized_pnl:.2f}")
print(f"Open:        {open_positions}  |  Trades this run: {total_trades}")
print("Done.")
