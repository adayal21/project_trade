
import os
import pandas as pd
from datetime import datetime

from config import INITIAL_CAPITAL, DATA_DIR

os.makedirs(DATA_DIR, exist_ok=True)

portfolio_file = f"{DATA_DIR}/portfolio.csv"


def initialize_portfolio():
    if not os.path.exists(portfolio_file):
        df = pd.DataFrame([{
            "Timestamp": datetime.now(),
            "Cash": INITIAL_CAPITAL,
            "Equity": INITIAL_CAPITAL,
            "Open Positions": 0,
            "Realized PnL": 0,
            "Unrealized PnL": 0,
            "Total Trades": 0
        }])

        df.to_csv(portfolio_file, index=False)


def log_portfolio(
    cash,
    equity,
    open_positions,
    realized_pnl,
    unrealized_pnl,
    total_trades
):
    df = pd.read_csv(portfolio_file)

    new_row = {
        "Timestamp": datetime.now(),
        "Cash": cash,
        "Equity": equity,
        "Open Positions": open_positions,
        "Realized PnL": realized_pnl,
        "Unrealized PnL": unrealized_pnl,
        "Total Trades": total_trades
    }

    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df.to_csv(portfolio_file, index=False)
