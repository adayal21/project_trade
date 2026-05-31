"""
grid_bot.py — Martingale Grid Bot (v2)
=======================================
Entry ladder based on % drop from original reference price.
All allocations as % of current available cash.

Levels:
  First run : buy 5% of cash (baseline)
  -2%  drop : buy 10% of cash
  -4%  drop : buy 20% of cash
  -8%  drop : buy 30% of cash
  -16% drop : buy 40% of cash
  <-16%     : buy remaining cash up to $1,000 cap

Exit: +1.5% flat from each position entry price.
Reference: fixed at first run price.
Resets: one idle run after all positions close, then fresh start.
"""

from __future__ import annotations
import os
import json
import pandas as pd
from datetime import datetime, timezone
from config import TRADING_MODE, COMMISSION
from grid_config import GRID_COINS, GRID_DATA_DIR, COIN_PRECISION, COIN_MIN_QTY, COIN_PRECISION, COIN_MIN_QTY
from bot_utils import _telegram

# =============================================================================
# Helpers
# =============================================================================
def _round_qty(symbol: str, qty: float) -> float:
    """Round quantity to coin precision and check minimum."""
    precision = COIN_PRECISION.get(symbol, 4)
    min_qty   = COIN_MIN_QTY.get(symbol, 0.0)
    rounded   = round(qty, precision)
    return rounded if rounded >= min_qty else 0.0

# =============================================================================
# Helpers
# =============================================================================

def _round_qty(symbol: str, qty: float) -> float:
    """Round quantity to CoinDCX required precision."""
    precision = COIN_PRECISION.get(symbol, 6)
    return round(qty, precision)

def _check_min_qty(symbol: str, qty: float) -> bool:
    """Check if quantity meets CoinDCX minimum."""
    min_qty = COIN_MIN_QTY.get(symbol, 0.0)
    return qty >= min_qty


# =============================================================================
# Constants
# =============================================================================
COIN_CAPITAL    = 1_000.0
SELL_TARGET     = 0.015       # 1.5% flat sell target
MAX_DROP_GUARD  = 0.10        # skip if candle dropped >10%

# Entry ladder: (drop_threshold, allocation_pct_of_cash)
ENTRY_LADDER = [
    (0.02,  0.10),   # -2%  → 10% of cash
    (0.04,  0.20),   # -4%  → 20% of cash
    (0.08,  0.30),   # -8%  → 30% of cash
    (0.16,  0.40),   # -16% → 40% of cash
]
FIRST_RUN_PCT   = 0.05        # 5% of cash on first run
MAX_POSITIONS   = 10          # safety cap

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
        "reference_price":  None,
        "waiting_reset":    False,  # True = one idle run before fresh start
        "positions":        [],
        "levels_bought":    [],     # which drop levels already bought
        "deployed":         0.0,
        "coin_cash":        COIN_CAPITAL,
        "realized_pnl":     0.0,
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
        "Timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "Cash":           round(cash, 2),
        "Equity":         round(equity, 2),
        "Unrealized PnL": round(unrealized, 2),
        "Realized PnL":   round(realized_pnl, 2),
        "Total Trades":   total_trades,
        "Open Positions": open_pos,
    }])
    if os.path.exists(f):
        row = pd.concat([pd.read_csv(f), row], ignore_index=True)
    row.to_csv(f, index=False)

# =============================================================================
# Notifications
# =============================================================================
def _notify(msg): _telegram(f"<b>[GRID]</b> {msg}")

def notify_buy(sym, price, cost, target, level):
    _notify(f"BUY {sym} @ ${price:.4f} | ${cost:.2f} | lvl={level} | tgt=${target:.4f}")

def notify_sell(sym, entry, exit_p, pnl):
    sign = "+" if pnl >= 0 else ""
    _notify(f"SELL {sym} @ ${exit_p:.4f} | entry=${entry:.4f} | pnl={sign}${pnl:.2f}")

# =============================================================================
# Per-coin update
# =============================================================================
def update_coin(symbol, df):
    if len(df) < 2:
        return 0.0, 0

    state         = load_state(symbol)
    cash          = state["coin_cash"]
    positions     = state["positions"]
    deployed      = state["deployed"]
    ref_price     = state["reference_price"]
    levels_bought = state["levels_bought"]
    waiting_reset = state["waiting_reset"]
    realized      = state["realized_pnl"]

    latest      = df.iloc[-1]
    prev        = df.iloc[-2]
    price       = float(latest["Close"])
    candle_high = float(latest["High"])
    candle_low  = float(latest["Low"])
    ts          = str(latest.name)

    net_pnl      = 0.0
    trades_count = 0

    # ── Waiting reset: one idle run, then fresh start ─────────────
    if waiting_reset:
        state["reference_price"] = price
        state["waiting_reset"]   = False
        state["levels_bought"]   = []
        save_state(symbol, state)
        return 0.0, 0

    # ── First run: set reference and buy 5% baseline ──────────────
    if ref_price is None:
        ref_price    = price
        buy_amount   = min(FIRST_RUN_PCT * cash, COIN_CAPITAL * 0.99)
        if buy_amount >= 1.0:
            qty    = _round_qty(symbol, buy_amount * (1 - COMMISSION) / price)
            if qty == 0.0:
                buy_amount = 0.0
            else:
                target = price * (1 + SELL_TARGET)
                positions.append({
                    "qty":         qty,
                    "entry_price": price,
                    "cost":        buy_amount,
                    "target":      target,
                    "level":       "init",
                "entry_time":  ts,
            })
            deployed += buy_amount
            cash     -= buy_amount
            notify_buy(symbol, price, buy_amount, target, "init")
        state.update({"reference_price": ref_price, "positions": positions,
                      "deployed": deployed, "coin_cash": cash,
                      "realized_pnl": realized, "levels_bought": levels_bought,
                      "waiting_reset": False})
        save_state(symbol, state)
        return 0.0, 0

    # ── Exit pass ─────────────────────────────────────────────────
    remaining = []
    for pos in positions:
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
                "level":       pos["level"],
                "cost":        round(pos["cost"], 2),
                "pnl":         round(pnl, 4),
                "pnl_pct":     round(pnl / pos["cost"] * 100, 3),
                "reason":      "TARGET_HIT",
                "entry_time":  pos["entry_time"],
                "exit_time":   ts,
            })
            notify_sell(symbol, pos["entry_price"], sell_p, pnl)
        else:
            remaining.append(pos)

    positions = remaining

    # All positions closed → wait one run before resetting
    if not positions and deployed <= 0:
        state.update({"coin_cash": cash, "positions": [], "deployed": 0.0,
                      "realized_pnl": realized, "levels_bought": [],
                      "waiting_reset": True, "reference_price": ref_price})
        save_state(symbol, state)
        return net_pnl, trades_count

    # ── Entry guard: skip if candle dropped >10% ─────────────────
    prev_close  = float(prev["Close"])
    candle_drop = (prev_close - price) / prev_close if prev_close > 0 else 0
    if candle_drop > MAX_DROP_GUARD:
        state.update({"coin_cash": cash, "positions": positions,
                      "deployed": deployed, "realized_pnl": realized,
                      "levels_bought": levels_bought})
        save_state(symbol, state)
        return net_pnl, trades_count

    # ── Entry pass: check drop levels from original reference ─────
    drop_pct = (ref_price - price) / ref_price if ref_price > 0 else 0

    if deployed < COIN_CAPITAL and len(positions) < MAX_POSITIONS:
        for threshold, alloc_pct in ENTRY_LADDER:
            level_key = str(threshold)
            if drop_pct >= threshold and level_key not in levels_bought:
                buy_amount = alloc_pct * cash
                buy_amount = min(buy_amount, COIN_CAPITAL - deployed, cash * 0.99)
                if buy_amount >= 1.0:
                    qty    = _round_qty(symbol, buy_amount * (1 - COMMISSION) / price)
                    if qty == 0.0:
                        continue
                    target = price * (1 + SELL_TARGET)
                    positions.append({
                        "qty":         qty,
                        "entry_price": price,
                        "cost":        buy_amount,
                        "target":      target,
                        "level":       f"-{int(threshold*100)}%",
                        "entry_time":  ts,
                    })
                    deployed       += buy_amount
                    cash           -= buy_amount
                    levels_bought.append(level_key)
                    notify_buy(symbol, price, buy_amount, target, f"-{int(threshold*100)}%")

        # Beyond -16%: deploy remaining cash
        if drop_pct >= 0.16 and "beyond" not in levels_bought:
            remaining_cap = COIN_CAPITAL - deployed
            buy_amount    = min(remaining_cap, cash * 0.99)
            if buy_amount >= 1.0:
                qty    = _round_qty(symbol, buy_amount * (1 - COMMISSION) / price)
                target = price * (1 + SELL_TARGET)
                positions.append({
                    "qty":         qty,
                    "entry_price": price,
                    "cost":        buy_amount,
                    "target":      target,
                    "level":       "beyond",
                    "entry_time":  ts,
                })
                deployed       += buy_amount
                cash           -= buy_amount
                levels_bought.append("beyond")
                notify_buy(symbol, price, buy_amount, target, "beyond")

    # ── Save ──────────────────────────────────────────────────────
    state.update({
        "coin_cash":       cash,
        "positions":       positions,
        "deployed":        deployed,
        "reference_price": ref_price,
        "realized_pnl":    realized,
        "levels_bought":   levels_bought,
        "waiting_reset":   False,
    })
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

    run_pnl = run_trades = total_open = 0
    total_cash = total_unrealized = total_equity = 0.0

    coin_lines = []

    for symbol in GRID_COINS:
        if symbol not in coins_data or len(coins_data[symbol]) < 2:
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
        unreal    = sum(pos["qty"] * price - pos["cost"] for pos in positions)
        eq        = state["coin_cash"] + sum(pos["qty"] * price for pos in positions)

        total_cash      += state["coin_cash"]
        total_unrealized += unreal
        total_equity    += eq

        status = "WAIT" if state.get("waiting_reset") else f"drop={drop_now:+.1f}%"
        coin_lines.append(
            f"  {symbol:<14} ref=${ref:.4f}  now=${price:.4f}  "
            f"{status}  open={n_open}  dep=${state['deployed']:.0f}  "
            f"unreal={unreal:+.2f}  pnl={pnl:+.2f}"
        )

    log_portfolio(total_cash, total_equity, total_unrealized,
                  realized_pnl, total_trades, total_open)

    # ── Compact log output ────────────────────────────────────────
    print("=" * 65)
    print(f"  GRID BOT  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  |  {TRADING_MODE.upper()}")
    print(f"  equity=${total_equity:.2f}  cash=${total_cash:.2f}  "
          f"unreal={total_unrealized:+.2f}  realized={realized_pnl:+.2f}  "
          f"trades={total_trades}  open={total_open}")
    print("-" * 65)
    for line in coin_lines:
        print(line)
    print("=" * 65)