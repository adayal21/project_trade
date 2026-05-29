"""
grid_bot.py — Grid Trading Bot
================================
Completely self-contained. No shared state with HMA/Ichimoku bot.
Called once per 4H run from main.py via run_grid_bot().

Architecture:
  - Each coin has its own grid state file: data/grid/ZEC_USDT_grid.json
  - Each coin has its own trade log:        data/grid/ZEC_USDT_grid_trades.csv
  - Portfolio state:                        data/grid/grid_portfolio.csv

Grid logic:
  - Dynamic range: upper = max High over last GRID_RANGE_BARS bars
                   lower = min Low  over last GRID_RANGE_BARS bars
  - 20 equal grid levels within the range
  - BUY  when candle Low touches a grid level (and level not already held)
  - SELL when candle High reaches the next grid level above a held position
  - STOP when price falls 15% below any grid entry
  - PAUSE buying when price below range lower — resume when back inside
  - RESET grid (upside only) when price breaks above range upper
"""

from __future__ import annotations

import os
import json
import math
import pandas as pd
from datetime import datetime, timezone

from config import (
    TRADING_MODE, CANDLES_URL, TIMEFRAME,
    CANDLES_LIMIT, INTERVAL_MS, WARMUP_BARS,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, COMMISSION,
)
from grid_config import (
    GRID_COINS, GRID_LEVELS, GRID_RANGE_BARS,
    GRID_STOP_PCT, GRID_MAX_OPEN, GRID_ALLOCATION,
    GRID_INITIAL_CAPITAL, GRID_DATA_DIR,
)
from bot_utils import fetch_candles, _telegram


# =============================================================================
# File helpers
# =============================================================================

def _safe(symbol: str) -> str:
    return symbol.replace("/", "_")

def _grid_state_file(symbol: str) -> str:
    return f"{GRID_DATA_DIR}/{_safe(symbol)}_grid.json"

def _grid_trades_file(symbol: str) -> str:
    return f"{GRID_DATA_DIR}/{_safe(symbol)}_grid_trades.csv"

def _grid_portfolio_file() -> str:
    return f"{GRID_DATA_DIR}/grid_portfolio.csv"


def load_grid_state(symbol: str) -> dict:
    """Load persisted grid state for a coin."""
    f = _grid_state_file(symbol)
    if os.path.exists(f):
        with open(f) as fh:
            return json.load(fh)
    return {
        "grid_levels":        [],
        "grid_orders":        {},   # "level_idx" → {qty, buy_price, cost, stop, entry_time}
        "range_upper":        0.0,
        "range_lower":        0.0,
        "capital_per_level":  0.0,
        "buying_paused":      False,
        "coin_cash":          GRID_INITIAL_CAPITAL * GRID_ALLOCATION,
    }


def save_grid_state(symbol: str, state: dict) -> None:
    with open(_grid_state_file(symbol), "w") as fh:
        json.dump(state, fh, indent=2)


def log_grid_trade(symbol: str, trade: dict) -> None:
    f  = _grid_trades_file(symbol)
    df = pd.DataFrame([trade])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)


def load_grid_portfolio() -> dict:
    f = _grid_portfolio_file()
    if os.path.exists(f):
        df = pd.read_csv(f)
        if not df.empty:
            r = df.iloc[-1]
            return {
                "realized_pnl": float(r["Realized PnL"]),
                "total_trades": int(r["Total Trades"]),
            }
    return {"realized_pnl": 0.0, "total_trades": 0}


def log_grid_portfolio(equity: float, realized_pnl: float,
                       total_trades: int, open_positions: int) -> None:
    f  = _grid_portfolio_file()
    row = pd.DataFrame([{
        "Timestamp":    datetime.now(timezone.utc).isoformat(),
        "Equity":       round(equity, 4),
        "Realized PnL": round(realized_pnl, 4),
        "Total Trades": total_trades,
        "Open Positions": open_positions,
    }])
    if os.path.exists(f):
        row = pd.concat([pd.read_csv(f), row], ignore_index=True)
    row.to_csv(f, index=False)


# =============================================================================
# Grid level builder
# =============================================================================

def build_grid_levels(lower: float, upper: float, n: int) -> list[float]:
    step = (upper - lower) / n
    return [lower + i * step for i in range(n + 1)]


# =============================================================================
# Telegram notifications
# =============================================================================

def _notify_grid(msg: str) -> None:
    _telegram(f"<b>[GRID BOT]</b>\n{msg}")


def notify_grid_buy(symbol: str, price: float, qty: float,
                    cost: float, stop: float, lvl: int) -> None:
    _notify_grid(
        f"BUY  {symbol}\n"
        f"Price : ${price:.4f}\n"
        f"Qty   : {qty:.6f}\n"
        f"Cost  : ${cost:.2f}\n"
        f"Stop  : ${stop:.4f}  (-{GRID_STOP_PCT:.0%})\n"
        f"Level : {lvl}/{GRID_LEVELS}"
    )


def notify_grid_sell(symbol: str, entry: float, exit_p: float,
                     pnl: float, reason: str) -> None:
    sign = "+" if pnl >= 0 else ""
    pct  = (exit_p - entry) / entry * 100
    _notify_grid(
        f"SELL {symbol}  [{reason}]\n"
        f"Entry : ${entry:.4f}\n"
        f"Exit  : ${exit_p:.4f}  ({pct:+.2f}%)\n"
        f"PnL   : {sign}${pnl:.2f}"
    )


# =============================================================================
# Per-coin grid update — called once per 4H run
# =============================================================================

def update_coin_grid(symbol: str, df: pd.DataFrame,
                     pf_state: dict) -> tuple[float, int]:
    """
    Process one 4H bar for one coin.
    Returns (net_pnl_this_run, trades_this_run).
    """
    if len(df) < GRID_RANGE_BARS + 1:
        return 0.0, 0

    state    = load_grid_state(symbol)
    cash     = state["coin_cash"]
    orders   = state["grid_orders"]           # str keys from JSON
    orders   = {int(k): v for k, v in orders.items()}  # restore int keys

    # Current (latest completed) candle
    row      = df.iloc[-1]
    price    = float(row["Close"])
    high     = float(row["High"])
    low      = float(row["Low"])
    ts       = str(df.index[-1])

    net_pnl      = 0.0
    trades_count = 0

    # ── Initial grid setup ─────────────────────────────────────────
    if not state["grid_levels"]:
        window               = df.iloc[-GRID_RANGE_BARS - 1:-1]
        state["range_upper"] = float(window["High"].max())
        state["range_lower"] = float(window["Low"].min())
        if state["range_upper"] <= state["range_lower"]:
            save_grid_state(symbol, {**state, "grid_orders": orders})
            return 0.0, 0
        state["grid_levels"]       = build_grid_levels(
            state["range_lower"], state["range_upper"], GRID_LEVELS
        )
        state["capital_per_level"] = cash / GRID_LEVELS
        state["buying_paused"]     = False
        save_grid_state(symbol, {**state, "grid_orders": orders})
        return 0.0, 0

    grid  = state["grid_levels"]
    ru    = state["range_upper"]
    rl    = state["range_lower"]
    cpl   = state["capital_per_level"]

    # ── Upside breakout → reset grid higher ───────────────────────
    if price > ru:
        for lvl_idx, pos in list(orders.items()):
            proceeds = pos["qty"] * price * (1 - COMMISSION)
            pnl      = proceeds - pos["cost"]
            net_pnl += pnl
            log_grid_trade(symbol, {
                "symbol": symbol, "entry_price": pos["buy_price"],
                "exit_price": price, "pnl": round(pnl, 4),
                "pnl_pct": round(pnl / pos["cost"] * 100, 3),
                "reason": "UPSIDE_BREAKOUT",
                "entry_time": pos["entry_time"], "exit_time": ts,
            })
            notify_grid_sell(symbol, pos["buy_price"], price, pnl, "UPSIDE_BREAKOUT")
            cash         += proceeds
            trades_count += 1
        orders = {}

        window               = df.iloc[-GRID_RANGE_BARS - 1:-1]
        state["range_upper"] = float(window["High"].max())
        state["range_lower"] = float(window["Low"].min())
        if state["range_upper"] > state["range_lower"]:
            state["grid_levels"]       = build_grid_levels(
                state["range_lower"], state["range_upper"], GRID_LEVELS
            )
            state["capital_per_level"] = cash / GRID_LEVELS if cash > 0 else 0
        state["buying_paused"] = False
        grid = state["grid_levels"]
        ru   = state["range_upper"]
        rl   = state["range_lower"]
        cpl  = state["capital_per_level"]

    # ── Pause / resume buying ─────────────────────────────────────
    state["buying_paused"] = price < rl

    # ── Process existing positions: stop loss + grid sell ─────────
    for lvl_idx in list(orders.keys()):
        pos      = orders[lvl_idx]
        lvl_high = grid[lvl_idx + 1] if lvl_idx + 1 < len(grid) else None

        # Stop loss
        if low <= pos["stop"]:
            stop_p   = pos["stop"]
            proceeds = pos["qty"] * stop_p * (1 - COMMISSION)
            pnl      = proceeds - pos["cost"]
            net_pnl += pnl
            log_grid_trade(symbol, {
                "symbol": symbol, "entry_price": pos["buy_price"],
                "exit_price": stop_p, "pnl": round(pnl, 4),
                "pnl_pct": round(pnl / pos["cost"] * 100, 3),
                "reason": "STOP_LOSS",
                "entry_time": pos["entry_time"], "exit_time": ts,
            })
            notify_grid_sell(symbol, pos["buy_price"], stop_p, pnl, "STOP_LOSS")
            cash        += proceeds
            trades_count += 1
            del orders[lvl_idx]
            free = max(GRID_LEVELS - len(orders), 1)
            cpl  = cash / free if cash > 0 else 0
            continue

        # Grid sell
        if lvl_high and high >= lvl_high:
            sell_p   = lvl_high
            proceeds = pos["qty"] * sell_p * (1 - COMMISSION)
            pnl      = proceeds - pos["cost"]
            net_pnl += pnl
            log_grid_trade(symbol, {
                "symbol": symbol, "entry_price": pos["buy_price"],
                "exit_price": sell_p, "pnl": round(pnl, 4),
                "pnl_pct": round(pnl / pos["cost"] * 100, 3),
                "reason": "GRID_SELL",
                "entry_time": pos["entry_time"], "exit_time": ts,
            })
            notify_grid_sell(symbol, pos["buy_price"], sell_p, pnl, "GRID_SELL")
            cash        += proceeds
            trades_count += 1
            del orders[lvl_idx]
            free = max(GRID_LEVELS - len(orders), 1)
            cpl  = cash / free if cash > 0 else 0

    # ── New buys ──────────────────────────────────────────────────
    if not state["buying_paused"] and len(orders) < GRID_MAX_OPEN:
        for lvl_idx in range(len(grid) - 1):
            if lvl_idx in orders:
                continue
            lvl_low = grid[lvl_idx]
            if low <= lvl_low <= high:
                alloc = min(cpl, cash * 0.95)
                if alloc < 1 or cash < alloc:
                    continue
                qty  = alloc * (1 - COMMISSION) / lvl_low
                stop = lvl_low * (1 - GRID_STOP_PCT)
                orders[lvl_idx] = {
                    "qty":        qty,
                    "buy_price":  lvl_low,
                    "cost":       alloc,
                    "stop":       stop,
                    "entry_time": ts,
                }
                cash -= alloc
                notify_grid_buy(symbol, lvl_low, qty, alloc, stop, lvl_idx)
                if len(orders) >= GRID_MAX_OPEN:
                    break

    # ── Persist updated state ─────────────────────────────────────
    state["coin_cash"]          = cash
    state["capital_per_level"]  = cpl
    state.update({
        "grid_orders": {str(k): v for k, v in orders.items()}
    })
    save_grid_state(symbol, state)

    return net_pnl, trades_count


# =============================================================================
# Main entry point — called from main.py
# =============================================================================

def run_grid_bot(coins_data: dict) -> None:
    """
    Run one cycle of the grid bot for all GRID_COINS.
    coins_data: dict of {symbol: DataFrame} already fetched by main.py.
    """
    os.makedirs(GRID_DATA_DIR, exist_ok=True)

    pf_state     = load_grid_portfolio()
    realized_pnl = pf_state["realized_pnl"]
    total_trades = pf_state["total_trades"]

    print("=" * 65)
    print("  GRID BOT — Dynamic Grid Strategy")
    print(f"  Coins: {len(GRID_COINS)}  |  Levels: {GRID_LEVELS}  "
          f"|  Stop: {GRID_STOP_PCT:.0%}  |  Range: {GRID_RANGE_BARS} bars")
    print("=" * 65)

    run_pnl    = 0.0
    run_trades = 0
    total_open = 0

    for symbol in GRID_COINS:
        if symbol not in coins_data:
            print(f"  {symbol:<14}  SKIP — no candle data")
            continue

        df = coins_data[symbol]
        if len(df) < GRID_RANGE_BARS + 2:
            print(f"  {symbol:<14}  SKIP — insufficient bars ({len(df)})")
            continue

        pnl, trades = update_coin_grid(symbol, df, pf_state)
        run_pnl    += pnl
        run_trades += trades
        total_trades += trades
        realized_pnl += pnl

        # Load state for display
        state  = load_grid_state(symbol)
        orders = state["grid_orders"]
        n_open = len(orders)
        total_open += n_open
        price  = float(df["Close"].iloc[-1])
        coin_equity = state["coin_cash"] + sum(
            pos["qty"] * price for pos in orders.values()
        )

        pause_str = " [PAUSED]" if state["buying_paused"] else ""
        print(f"  {symbol:<14}  "
              f"open={n_open:>2}/{GRID_MAX_OPEN}  "
              f"cash=${state['coin_cash']:>8.2f}  "
              f"equity=${coin_equity:>8.2f}  "
              f"pnl_run={pnl:>+7.2f}  "
              f"trades={trades}{pause_str}")

    # Total grid equity
    total_equity = 0.0
    for symbol in GRID_COINS:
        state = load_grid_state(symbol)
        if symbol in coins_data:
            price = float(coins_data[symbol]["Close"].iloc[-1])
        else:
            price = 0.0
        total_equity += state["coin_cash"] + sum(
            pos["qty"] * price for pos in state["grid_orders"].values()
        )

    log_grid_portfolio(total_equity, realized_pnl, total_trades, total_open)

    print()
    print("=" * 65)
    print("  Grid Portfolio Snapshot")
    print("=" * 65)
    print(f"  Mode          : {TRADING_MODE.upper()}")
    print(f"  Total equity  : ${total_equity:,.4f}")
    print(f"  Realized PnL  : ${realized_pnl:+,.4f}")
    print(f"  Return        : "
          f"{(total_equity / (GRID_INITIAL_CAPITAL * len(GRID_COINS) * GRID_ALLOCATION) - 1) * 100:+.2f}%")
    print(f"  Open positions: {total_open}")
    print(f"  Trades (run)  : {run_trades}")
    print(f"  Total trades  : {total_trades}")
    print()