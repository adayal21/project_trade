import os
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from config import INITIAL_CAPITAL, DATA_DIR

os.makedirs(DATA_DIR, exist_ok=True)

portfolio_file = f"{DATA_DIR}/portfolio.csv"

IST = ZoneInfo("Asia/Kolkata")


def initialize_portfolio():
    if not os.path.exists(portfolio_file):
        df = pd.DataFrame([{
            "Timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "Cash": INITIAL_CAPITAL,
            "Equity": INITIAL_CAPITAL,
            "Open Positions": 0,
            "Realized PnL": 0,
            "Unrealized PnL": 0,
            "Total Trades": 0,
            "Events": ""
        }])
        df.to_csv(portfolio_file, index=False)


def log_portfolio(
    cash,
    equity,
    open_positions,
    realized_pnl,
    unrealized_pnl,
    total_trades,
    events=None        # list of strings describing what happened this run
):
    df = pd.read_csv(portfolio_file)

    # Build a readable summary of this run's events.
    # e.g. "NEAR +14.22 PARTIAL | FIL -9.54 T4D_LOSING | BTC LONG entry"
    events_str = " | ".join(events) if events else ""

    new_row = {
        "Timestamp":      datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "Cash":           round(cash, 2),
        "Equity":         round(equity, 2),
        "Open Positions": open_positions,
        "Realized PnL":   round(realized_pnl, 2),
        "Unrealized PnL": round(unrealized_pnl, 2),
        "Total Trades":   total_trades,
        "Events":         events_str
    }

    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df.to_csv(portfolio_file, index=False)