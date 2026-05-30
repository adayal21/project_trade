"""
grid_bot.py — Percentage-Based Martingale Grid Bot
====================================================
Replaces the fixed-level grid bot with a dynamic percentage-based system.

Logic:
  - Reference price set at first run per coin
  - Every run: check % drop from reference price
  - If dropped X% → buy X% of coin capital ($1,000)
  - Each position sells independently at entry × 1.02 (+2% flat target)
  - Guard: skip buying if coin dropped >10% in current 1H candle
  - Hard cap: cumulative deployed ≤ $1,000 per coin
  - Reference resets when all positions closed

Capital:
  - Total: $10,000
  - Per coin: $1,000 (7 coins × $1,000 = $7,000 deployed, $3,000 buffer)
"""

from __future__ import annotations

import os
import json
import pandas as pd
from datetime import datetime, timezone

from config import TRADING_MODE, COMMISSION
from grid_config import GRID_COINS, GRID_DATA_DIR
from bot_utils import _telegram

# =============================================================================
# Constants
# =============================================================================
COIN_CAPITAL    = 1_000.0   # hard capital per coin
# Dynamic sell targets based on time held
SELL_TARGET_FAST = 0.02     # < 4 hours → 2%
SELL_TARGET_MID  = 0.015    # 4-12 hours → 1.5%
SELL_TARGET_SLOW = 0.01     # > 12 hours → 1%
MAX_DROP_GUARD   = 0.10     # skip if candle dropped >10%
MIN_DROP_TO_BUY = 0.01      # minimum 1% drop to trigger buy
MAX_POSITIONS   = 20        # safety cap per coin

# =============================================================================
# File helpers
# =============================================================================

def _safe(s): return s.replace("/", "_")
def _state_file(sym): return f"{GRID_DATA_DIR}/{_safe(sym)}_grid.json"
def _trades_file(sym): return f"{GRID_DATA_DIR}/{_safe(sym)}_grid_trades.csv"
def _portfolio_file(): return f"{GRID_DATA_DIR}/grid_portfolio.csv"


def load_state(symbol):
    f = _state_file(symbol)
    if os.path.exists(f):
        with open(f) as fh:
            return json.load(fh)
    return {
        "reference_price": None,
        "positions":       [],
        "deployed":        0.0,
        "coin_cash":       COIN_CAPITAL,
        "realized_pnl":    0.0,
    }


def save_state(symbol, state):
    with open(_state_file(symbol), "w") as fh:
        json.dump(state, fh, indent=2)


def log_trade(symbol, trade):
    f  = _trades_file(symbol)
    df = pd.DataFrame([trade])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)


def load_portfolio():
    f = _portfolio_file()
    if os.path.exists(f):
        df = pd.read_csv(f)
        if not df.empty:
            r = df.iloc[-1]
            return {"realized_pnl": float(r["Realized PnL"]),
                    "total_trades": int(r["Total Trades"])}
    return {"realized_pnl": 0.0, "total_trades": 0}


def log_portfolio(cash, equity, unrealized, realized_pnl, total_trades, open_pos):
    f   = _portfolio_file()
    row = pd.DataFrame([{
        "Timestamp":      datetime.now(timezone.utc).isoformat(),
        "Cash":           round(cash, 4),
        "Equity":         round(equity, 4),
        "Unrealized PnL": round(unrealized, 4),
        "Realized PnL":   round(realized_pnl, 4),
        "Total Trades":   total_trades,
        "Open Positions": open_pos,
    }])
    if os.path.exists(f):
        row = pd.concat([pd.read_csv(f), row], ignore_index=True)
    row.to_csv(f, index=False)


# =============================================================================
# Notifications
# =============================================================================

def _notify(msg): _telegram(f"<b>[GRID BOT]</b>\n{msg}")

def notify_buy(sym, price, cost, target, drop_pct, deployed):
    _notify(f"BUY  {sym}\n"
            f"Drop    : -{drop_pct:.2f}% from ref\n"
            f"Price   : ${price:.4f}\n"
            f"Cost    : ${cost:.2f}\n"
            f"Target  : ${target:.4f}  (+2%)\n"
            f"Deployed: ${deployed:.2f} / ${COIN_CAPITAL:.0f}")

def notify_sell(sym, entry, exit_p, pnl, drop_pct):
    sign = "+" if pnl >= 0 else ""
    _notify(f"SELL {sym}\n"
            f"Entry : ${entry:.4f}  (-{drop_pct:.2f}% entry)\n"
            f"Exit  : ${exit_p:.4f}  (+2% target)\n"
            f"PnL   : {sign}${pnl:.2f}")


# =============================================================================
# Per-coin update
# =============================================================================

def update_coin(symbol, df):
    if len(df) < 2:
        return 0.0, 0

    state     = load_state(symbol)
    cash      = state["coin_cash"]
    positions = state["positions"]
    deployed  = state["deployed"]
    ref_price = state["reference_price"]
    realized  = state["realized_pnl"]

    latest      = df.iloc[-1]
    prev        = df.iloc[-2]
    price       = float(latest["Close"])
    candle_high = float(latest["High"])
    ts          = str(latest.name)

    net_pnl      = 0.0
    trades_count = 0

    # ── First run: buy 1% immediately as baseline position ───────
    if ref_price is None:
        ref_price  = price
        buy_amount = 0.01 * cash   # 1% of available cash
        buy_amount = min(buy_amount, COIN_CAPITAL - deployed, cash * 0.99)
        if buy_amount >= 1.0:
            qty    = buy_amount * (1 - COMMISSION) / price
            target = price * (1 + SELL_TARGET_FAST)
            positions.append({
                "qty":         qty,
                "entry_price": price,
                "cost":        buy_amount,
                "target":      target,
                "drop_pct":    0.0,   # baseline entry, no drop
                "entry_time":  ts,
            })
            deployed += buy_amount
            cash     -= buy_amount
            notify_buy(symbol, price, buy_amount, target, 0.0, deployed)

        state.update({"reference_price": ref_price, "positions": positions,
                      "deployed": deployed, "coin_cash": cash,
                      "realized_pnl": realized})
        save_state(symbol, state)
        return 0.0, 0

    # ── Exit pass ─────────────────────────────────────────────────
    now_ts = datetime.now(timezone.utc)

    remaining = []
    for pos in positions:
        # Calculate dynamic target based on how long position has been open
        try:
            entry_dt  = pd.to_datetime(pos["entry_time"], utc=True)
            hours_held = (now_ts - entry_dt).total_seconds() / 3600
        except Exception:
            hours_held = 0

        if hours_held < 4:
            dynamic_target = pos["entry_price"] * (1 + SELL_TARGET_FAST)
        elif hours_held < 12:
            dynamic_target = pos["entry_price"] * (1 + SELL_TARGET_MID)
        else:
            dynamic_target = pos["entry_price"] * (1 + SELL_TARGET_SLOW)

        # Update target in position if it degraded
        pos["target"] = dynamic_target

        if candle_high >= pos["target"]:
            sell_p   = pos["target"]
            proceeds = pos["qty"] * sell_p * (1 - COMMISSION)
            pnl      = proceeds - pos["cost"]
            net_pnl += pnl
            realized += pnl
            deployed -= pos["cost"]
            cash     += proceeds
            trades_count += 1
            log_trade(symbol, {
                "symbol":      symbol,
                "entry_price": pos["entry_price"],
                "exit_price":  sell_p,
                "drop_pct":    pos["drop_pct"],
                "cost":        pos["cost"],
                "pnl":         round(pnl, 4),
                "pnl_pct":     round(pnl / pos["cost"] * 100, 3),
                "reason":      "TARGET_HIT",
                "entry_time":  pos["entry_time"],
                "exit_time":   ts,
            })
            notify_sell(symbol, pos["entry_price"], sell_p, pnl, pos["drop_pct"])
        else:
            remaining.append(pos)

    positions = remaining

    # Reset reference only if we HAD positions and now they are all closed
    # Do NOT reset just because positions is empty at start of run
    if not positions and state.get("deployed", 0) > 0:
        ref_price = price
        deployed  = 0.0

    # ── Entry guard: skip if 1H candle dropped >10% ──────────────
    prev_close  = float(prev["Close"])
    candle_drop = (prev_close - price) / prev_close if prev_close > 0 else 0
    if candle_drop > MAX_DROP_GUARD:
        print(f"    {symbol}: SKIP — candle dropped {candle_drop:.1%} (>{MAX_DROP_GUARD:.0%} guard)")
        state.update({"coin_cash": cash, "positions": positions,
                      "deployed": deployed, "reference_price": ref_price,
                      "realized_pnl": realized})
        save_state(symbol, state)
        return net_pnl, trades_count

    # ── Entry pass ────────────────────────────────────────────────
    # NOTE: cash is already updated from any sells above — use current cash as base
    drop_pct = (ref_price - price) / ref_price if ref_price > 0 else 0

    if (drop_pct >= MIN_DROP_TO_BUY and
            deployed < COIN_CAPITAL and
            len(positions) < MAX_POSITIONS):

        # Don't double-buy at same drop level
        existing_drops = {round(p["drop_pct"], 1) for p in positions}
        drop_rounded   = round(drop_pct * 100, 1)

        # Buy amount = drop% × current available cash (not fixed $1,000)
        buy_amount = drop_pct * cash
        buy_amount = min(buy_amount, COIN_CAPITAL - deployed, cash * 0.99)

        if buy_amount >= 1.0 and drop_rounded not in existing_drops:
            qty    = buy_amount * (1 - COMMISSION) / price
            target = price * (1 + SELL_TARGET_FAST)  # starts at 2%, degrades over time

            positions.append({
                "qty":         qty,
                "entry_price": price,
                "cost":        buy_amount,
                "target":      target,
                "drop_pct":    round(drop_pct * 100, 2),
                "entry_time":  ts,
            })
            deployed += buy_amount
            cash     -= buy_amount
            notify_buy(symbol, price, buy_amount, target, drop_pct * 100, deployed)

    # ── Save ──────────────────────────────────────────────────────
    state.update({"coin_cash": cash, "positions": positions,
                  "deployed": deployed, "reference_price": ref_price,
                  "realized_pnl": realized})
    save_state(symbol, state)
    return net_pnl, trades_count


# =============================================================================
# Main entry point
# =============================================================================

def run_grid_bot(coins_data):
    os.makedirs(GRID_DATA_DIR, exist_ok=True)

    pf           = load_portfolio()
    realized_pnl = pf["realized_pnl"]
    total_trades = pf["total_trades"]

    print("=" * 65)
    print("  GRID BOT — % Martingale Strategy")
    print(f"  Coins: {len(GRID_COINS)}  |  Buy: drop%×$1k  |  "
          f"Sell: 2%→1.5%→1% (dynamic)  |  Cap: ${COIN_CAPITAL:.0f}/coin")
    print("=" * 65)

    run_pnl = run_trades = total_open = 0

    for symbol in GRID_COINS:
        if symbol not in coins_data or len(coins_data[symbol]) < 2:
            print(f"  {symbol:<14}  SKIP")
            continue

        pnl, trades   = update_coin(symbol, coins_data[symbol])
        run_pnl      += pnl
        run_trades   += trades
        total_trades += trades
        realized_pnl += pnl

        state     = load_state(symbol)
        positions = state["positions"]
        n_open    = len(positions)
        total_open += n_open
        price     = float(coins_data[symbol]["Close"].iloc[-1])
        ref       = state["reference_price"] or price
        drop_now  = (ref - price) / ref * 100 if ref > 0 else 0
        unrealized= sum(pos["qty"] * price - pos["cost"] for pos in positions)

        print(f"  {symbol:<14}  "
              f"ref=${ref:.4f}  "
              f"now=${price:.4f}  "
              f"drop={drop_now:>+.2f}%  "
              f"open={n_open:>2}  "
              f"deployed=${state['deployed']:>7.2f}  "
              f"unreal={unrealized:>+7.2f}  "
              f"pnl={pnl:>+7.2f}")

    # Portfolio totals
    total_cash = total_unrealized = total_equity = 0.0
    for symbol in GRID_COINS:
        state = load_state(symbol)
        price = float(coins_data[symbol]["Close"].iloc[-1]) if symbol in coins_data else 0.0
        positions       = state["positions"]
        coin_pos_value  = sum(p["qty"] * price for p in positions)
        coin_cost       = sum(p["cost"] for p in positions)
        total_cash     += state["coin_cash"]
        total_unrealized+= coin_pos_value - coin_cost
        total_equity   += state["coin_cash"] + coin_pos_value

    log_portfolio(total_cash, total_equity, total_unrealized,
                  realized_pnl, total_trades, total_open)

    print()
    print("=" * 65)
    print("  Grid Portfolio Snapshot")
    print("=" * 65)
    print(f"  Mode          : {TRADING_MODE.upper()}")
    print(f"  Cash          : ${total_cash:,.4f}")
    print(f"  Unrealized    : ${total_unrealized:+,.4f}")
    print(f"  Equity        : ${total_equity:,.4f}")
    print(f"  Realized PnL  : ${realized_pnl:+,.4f}")
    print(f"  Return        : "
          f"{(total_equity / (COIN_CAPITAL * len(GRID_COINS)) - 1) * 100:+.2f}%")
    print(f"  Open positions: {total_open}")
    print(f"  Trades (run)  : {run_trades}")
    print(f"  Total trades  : {total_trades}")
    print()