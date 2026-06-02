"""
main.py — HMA + Ichimoku Combined Crypto Bot
=============================================
Entry  : 4H candle close only (0,4,8,12,16,20 UTC)
Exit   : 1H candles, checked every hourly cron run — ALL coins
         Uses compute_hma_exit_1h() (EWM-based HMA, no linreg dependency)
         which avoids the None return from utils.prepare_dataset on short 1H history.

Cron:
    5 * * * * cd ~/Projects/project_trade && echo "========================================" >> logs/live.log && TZ=Asia/Kolkata date >> logs/live.log && /home/g12amandayal12/Projects/venv/bin/python main.py >> logs/live.log 2>&1
"""

import os
import sys
import pandas as pd
from datetime import datetime, timezone

from config import (
    TRADING_MODE, INITIAL_CAPITAL, COINS, STRATEGIES, DATA_DIR,
    ALLOCATION_PCT, MAX_OPEN_POSITIONS, POSITION_SIZE,
    COMMISSION, STOP_LOSS_PCT, VERBOSE,
)
from bot_utils import (
    fetch_candles,
    fetch_candles_1h,
    compute_hma_signals,
    compute_hma_exit_1h,
    compute_ichimoku_signals,
    notify_entry, notify_exit, notify_run_summary,
)
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)

_now_utc     = datetime.now(timezone.utc)
_now_hour    = _now_utc.hour
_is_4h_close = (_now_hour % 4 == 0)


# =============================================================================
# Position helpers
# =============================================================================

def _safe(symbol):   return symbol.replace("/", "_")
def _short(strat):   return "ichi" if strat == "ichimoku" else "hma"

def get_position_file(symbol, strategy):
    return f"{DATA_DIR}/{_safe(symbol)}_{_short(strategy)}_position.csv"

def get_trade_file(symbol, strategy):
    return f"{DATA_DIR}/{_safe(symbol)}_{_short(strategy)}_trades.csv"

def load_position(symbol, strategy):
    f = get_position_file(symbol, strategy)
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    if df.empty:
        os.remove(f); return None
    pos = df.iloc[0].to_dict()
    if float(pos.get("Quantity", 0)) <= 0:
        os.remove(f); return None
    return pos

def save_position(symbol, strategy, pos):
    pd.DataFrame([pos]).to_csv(get_position_file(symbol, strategy), index=False)

def clear_position(symbol, strategy):
    f = get_position_file(symbol, strategy)
    if os.path.exists(f): os.remove(f)

def log_trade(symbol, strategy, trade):
    f  = get_trade_file(symbol, strategy)
    df = pd.DataFrame([trade])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)

def count_open():
    return sum(1 for s in COINS for st in STRATEGIES
               if load_position(s, st) is not None)

def load_portfolio_state():
    pf = f"{DATA_DIR}/portfolio.csv"
    if os.path.exists(pf):
        df = pd.read_csv(pf)
        if len(df) > 0:
            r = df.iloc[-1]
            return {
                "cash":         float(r["Cash"]),
                "realized_pnl": float(r["Realized PnL"]),
                "total_trades": int(r["Total Trades"]),
            }
    return {"cash": INITIAL_CAPITAL, "realized_pnl": 0.0, "total_trades": 0}

def current_equity(cash, coins_data):
    val = 0.0
    for s in COINS:
        for st in STRATEGIES:
            pos = load_position(s, st)
            if pos is None: continue
            ep  = float(pos["Entry Price"])
            qty = float(pos["Quantity"])
            cp  = float(coins_data[s]["Close"].iloc[-1]) if s in coins_data else ep
            val += qty * cp
    return cash + val


# =============================================================================
# Startup
# =============================================================================

initialize_portfolio()

print("=" * 55)
print(f"  CRYPTO BOT | {TRADING_MODE.upper()} | {_now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"  {'4H CLOSE — entries + exits' if _is_4h_close else 'Hourly — exits only'}")
print("=" * 55)

state        = load_portfolio_state() if TRADING_MODE != "live" else None
if TRADING_MODE == "live":
    from exchange import get_live_usdt_balance
    cash         = get_live_usdt_balance()
    state        = load_portfolio_state()
    realized_pnl = state["realized_pnl"]
    total_trades = state["total_trades"]
else:
    cash         = state["cash"]
    realized_pnl = state["realized_pnl"]
    total_trades = state["total_trades"]

run_events = []
trades_run = 0

open_now = count_open()
print(f"  Cash: ${cash:,.2f}  |  Open: {open_now}/{MAX_OPEN_POSITIONS}  |  Trades: {total_trades}  |  PnL: ${realized_pnl:+,.2f}")
print()


# =============================================================================
# Step 1 — Fetch candles (4H for signals/entries, 1H for exits on all coins)
# =============================================================================

coins_data    = {}   # 4H candles — entries + Ichimoku signals
coins_data_1h = {}   # 1H candles — HMA exit for ALL coins

fetch_errors = []
for symbol in COINS:
    # --- 4H candles ---
    df4h = fetch_candles(symbol)
    if df4h.empty or len(df4h) < 100:
        print(f"  [{symbol}] 4H fetch failed or insufficient bars ({len(df4h)}), skipping")
        fetch_errors.append(symbol)
        continue

    age_4h = (datetime.now(timezone.utc) - df4h.index[-1]).total_seconds() / 3600
    print(f"[{symbol}] fetched {len(df4h)} bars, last={df4h.index[-1]} ({age_4h:.1f}h ago)")
    if age_4h > 5:
        print(f"  [{symbol}] STALE 4H DATA — {age_4h:.1f}h old, skipping")
        fetch_errors.append(symbol)
        continue

    coins_data[symbol] = df4h

    # --- 1H candles ---
    df1h = fetch_candles_1h(symbol)
    if df1h.empty or len(df1h) < 80:
        print(f"  [{symbol}] 1H fetch failed or insufficient bars ({len(df1h)}), skipping 1H")
        continue

    age_1h = (datetime.now(timezone.utc) - df1h.index[-1]).total_seconds() / 3600
    if age_1h > 2:
        print(f"  [{symbol}] STALE 1H DATA — {age_1h:.1f}h old, skipping 1H")
        continue

    coins_data_1h[symbol] = df1h

if fetch_errors:
    print(f"  FETCH ERRORS: {', '.join(fetch_errors)}")


# =============================================================================
# Step 2 — Compute signals (4H HMA + Ichimoku for entries/display)
# =============================================================================

all_signals = {}
sig_rows    = []

for symbol, df in coins_data.items():
    hma_sig  = compute_hma_signals(symbol, df)
    ichi_sig = compute_ichimoku_signals(symbol, df)
    if hma_sig:  all_signals[(symbol, "hma")]      = hma_sig
    if ichi_sig: all_signals[(symbol, "ichimoku")] = ichi_sig

    hma_str  = ("ENTRY" if hma_sig and hma_sig["entry_signal"] else
                "EXIT " if hma_sig and hma_sig["exit_signal"]  else
                "hold " if hma_sig else "ERR  ")
    ichi_str = ("ENTRY" if ichi_sig and ichi_sig["entry_signal"] else
                "EXIT " if ichi_sig and ichi_sig["exit_signal"]  else
                "hold " if ichi_sig else "ERR  ")
    rsi_str  = f"{hma_sig['rsi']:>5.1f}" if hma_sig else "  N/A"
    gap_str  = f"{hma_sig['hma_gap_pct']:>+6.2f}%" if hma_sig else "   N/A"
    tk_str   = ("BULL" if ichi_sig and ichi_sig.get("tk_bull") else "bear") if ichi_sig else " N/A"
    cg_str   = f"{ichi_sig.get('cloud_gap_pct',0):>+6.2f}%" if ichi_sig else "   N/A"
    sig_rows.append(f"  {symbol:<14} {rsi_str} {gap_str} {hma_str}    {tk_str} {cg_str} {ichi_str}")

print("=" * 55)
print("  Signals  (4H HMA + Ichimoku)")
print(f"  {'Coin':<14} {'RSI':>5} {'Gap%':>7} {'HMA':>5}    {'TK':>4} {'Cld%':>7} {'ICHI':>5}")
print("  " + "-" * 51)
for r in sig_rows: print(r)
print()


# =============================================================================
# Step 3 — Exit pass (every hourly run — 1H HMA exit for ALL coins)
# =============================================================================

print("=" * 55)
print("  Exit pass  [1H HMA for all HMA | Ichimoku signals]")
print("=" * 55)

any_exit = False
for symbol in COINS:
    for strategy in STRATEGIES:
        pos = load_position(symbol, strategy)
        if pos is None:
            continue

        entry_price = float(pos["Entry Price"])
        quantity    = float(pos["Quantity"])
        bars_held   = int(pos.get("Bars_Held", 0))
        sig_4h      = all_signals.get((symbol, strategy))

        latest_price = (float(coins_data[symbol]["Close"].iloc[-1])
                        if symbol in coins_data else entry_price)
        move_pct    = (latest_price - entry_price) / entry_price
        exit_reason = None

        if strategy == "hma":
            # Stop loss
            if move_pct <= -STOP_LOSS_PCT:
                exit_reason = "STOP_LOSS"
            else:
                # 1H HMA exit — ALL coins use this path now
                if symbol in coins_data_1h:
                    sig_1h = compute_hma_exit_1h(symbol, coins_data_1h[symbol])
                    if sig_1h and sig_1h["exit_signal"]:
                        exit_reason = "HMA_1H_CROSS"
                else:
                    # Fallback to 4H signal only if 1H fetch failed
                    if sig_4h and sig_4h["exit_signal"]:
                        exit_reason = "HMA_4H_FALLBACK"

        elif strategy == "ichimoku":
            if sig_4h and sig_4h["exit_signal"]:
                exit_reason = ("TK_CROSS_DOWN" if sig_4h.get("tk_bear")
                               else "BELOW_KIJUN")

        if exit_reason is None:
            print(f"  {symbol:<14} [{strategy.upper():<8}]  HOLD  "
                  f"{move_pct:+.2%}  bars={bars_held}")
            save_position(symbol, strategy, {**pos, "Bars_Held": bars_held + 1})
            continue

        # Execute exit
        pnl     = move_pct * entry_price * quantity
        comm    = latest_price * quantity * COMMISSION
        net_pnl = pnl - comm if TRADING_MODE == "paper" else pnl

        print(f"  {symbol:<14} [{strategy.upper():<8}]  EXIT [{exit_reason}]  "
              f"{entry_price:.4f}→{latest_price:.4f}  {move_pct:+.2%}  PnL=${net_pnl:+.2f}")

        realized_pnl += net_pnl
        total_trades += 1
        trades_run   += 1
        cash         += entry_price * quantity + net_pnl
        any_exit      = True

        run_events.append(f"{symbol}[{strategy[:4].upper()}] {net_pnl:+.2f} {exit_reason}")

        log_trade(symbol, strategy, {
            "Coin":        symbol,
            "Strategy":    strategy,
            "Entry Price": entry_price,
            "Exit Price":  round(latest_price, 8),
            "Quantity":    quantity,
            "PnL":         round(net_pnl, 6),
            "Exit Reason": exit_reason,
            "Exit Time":   _now_utc.isoformat(),
        })

        notify_exit(symbol, strategy, entry_price, latest_price,
                    quantity, net_pnl, exit_reason)

        if TRADING_MODE == "live":
            from exchange import place_market_order
            place_market_order(symbol, "sell", quantity)

        clear_position(symbol, strategy)

if not any_exit and count_open() == 0:
    print("  No open positions.")
print()


# =============================================================================
# Step 4 — Entry pass (4H candle closes only)
# =============================================================================

print("=" * 55)
print("  Entry pass")
print("=" * 55)

equity_now = current_equity(cash, coins_data)
open_count = count_open()

if not _is_4h_close:
    print("  Skipped — not a 4H close.")
    raw_candidates = []
else:
    raw_candidates = [
        (sym, strat, sig)
        for sym in COINS
        for strat in STRATEGIES
        if (sig := all_signals.get((sym, strat))) and
           sig["entry_signal"] and
           load_position(sym, strat) is None
    ]

# Double-confirmed priority
entry_coins = {}
for sym, strat, sig in raw_candidates:
    entry_coins.setdefault(sym, []).append(strat)
double_confirmed = {sym for sym, strats in entry_coins.items() if len(strats) >= 2}

candidates = sorted(raw_candidates, key=lambda c: (
    0 if c[0] in double_confirmed else 1,
    COINS.index(c[0]) if c[0] in COINS else 99,
    0 if c[1] == "hma" else 1,
))

if not candidates:
    print("  No entry signals." if _is_4h_close else "")
else:
    print(f"  {len(candidates)} signal(s):")
    for sym, strat, sig in candidates:
        dbl = " ★DOUBLE" if sym in double_confirmed else ""
        print(f"    {sym:<14} [{strat.upper():<8}]{dbl}  @ {sig['close']:.4f}")
    print()

    for sym, strat, sig in candidates:
        if open_count >= MAX_OPEN_POSITIONS:
            print(f"  {sym} [{strat.upper()}]  BLOCKED — max positions")
            continue
        allocation = equity_now * ALLOCATION_PCT
        if cash < allocation or allocation <= 0:
            print(f"  {sym} [{strat.upper()}]  BLOCKED — insufficient cash")
            continue

        # ==================================================
        # NEW 1H CONFIRMATION FILTER
        # ==================================================
        if strat == "hma":

            df1h = coins_data_1h.get(sym)

            if df1h is not None:

                sig_1h = compute_hma_exit_1h(sym, df1h)

                if sig_1h:

                    if sig_1h["hma_fast"] <= sig_1h["hma_slow"]:

                        print(
                            f"  {sym} [HMA] BLOCKED "
                            f"- 1H bearish "
                            f"({sig_1h['hma_fast']:.4f} "
                            f"< "
                            f"{sig_1h['hma_slow']:.4f})"
                        )

                        continue

        latest_price = sig["close"]
        quantity     = (allocation * POSITION_SIZE) / latest_price
        dbl_str      = " ★DOUBLE" if sym in double_confirmed else ""

        print(f"  {sym:<14} [{strat.upper():<8}]  ENTRY{dbl_str}  "
              f"@ ${latest_price:.4f}  qty={quantity:.6f}  alloc=${allocation:.2f}")

        if TRADING_MODE == "live":
            from exchange import place_market_order, get_order_status
            import time as _t
            order_id = place_market_order(sym, "buy", quantity)
            if order_id is None:
                print(f"    ORDER FAILED — skipping")
                continue
            _t.sleep(1.5)
            info     = get_order_status(order_id)
            fill_px  = float(info.get("avg_price", latest_price) or latest_price)
            fill_qty = float(info.get("total_quantity", quantity) or quantity) \
                     - float(info.get("remaining_quantity", 0) or 0)
            if info.get("status") not in ("filled", "partially_filled"):
                print(f"    Not filled — skipping")
                continue
            latest_price = fill_px
            quantity     = fill_qty

        save_position(sym, strat, {
            "Coin":        sym,
            "Strategy":    strat,
            "Side":        "LONG",
            "Entry Price": latest_price,
            "Quantity":    quantity,
            "Timestamp":   _now_utc.isoformat(),
            "Bars_Held":   0,
        })

        cash        -= allocation
        open_count  += 1
        total_trades += 1
        trades_run   += 1
        run_events.append(f"{sym}[{strat[:4].upper()}] ENTRY @ {latest_price:.4f}")

        notify_entry(sym, strat, latest_price, quantity, allocation, sig)

print()


# =============================================================================
# Step 5 — Portfolio snapshot
# =============================================================================

equity_final = current_equity(cash, coins_data)
open_count   = count_open()
unrealized = 0.0
for s in COINS:
    for st in STRATEGIES:
        pos = load_position(s, st)
        if pos is None: continue
        ep  = float(pos["Entry Price"])
        qty = float(pos["Quantity"])
        cp  = float(coins_data[s]["Close"].iloc[-1]) if s in coins_data else ep
        unrealized += (cp - ep) * qty

log_portfolio(cash, equity_final, open_count, realized_pnl, unrealized, total_trades, run_events)
notify_run_summary(equity_final, cash, open_count, realized_pnl)

ret_pct = (equity_final / INITIAL_CAPITAL - 1) * 100
print("=" * 55)
print(f"  Cash: ${cash:,.2f}  Unreal: ${unrealized:+,.2f}  Equity: ${equity_final:,.2f}")
print(f"  Return: {ret_pct:+.2f}%  |  PnL: ${realized_pnl:+,.2f}  |  Open: {open_count}/{MAX_OPEN_POSITIONS}")

# Open positions summary
if open_count > 0:
    print()
    print("  Open positions:")
    for s in COINS:
        for st in STRATEGIES:
            pos = load_position(s, st)
            if pos is None: continue
            ep   = float(pos["Entry Price"])
            qty  = float(pos["Quantity"])
            bars = int(pos.get("Bars_Held", 0))
            cp   = float(coins_data[s]["Close"].iloc[-1]) if s in coins_data else ep
            move = (cp - ep) / ep
            pnl  = (cp - ep) * qty
            sig  = all_signals.get((s, st))
            gap_str = ""
            if sig and st == "hma":
                gap = sig.get("hma_gap_pct", 0)
                label = "ext" if gap > 5 else "ok" if gap > 1 else "weak" if gap > 0 else "EXIT"
                gap_str = f"  gap={gap:+.2f}%({label})"
            elif sig and st == "ichimoku":
                gap_str = f"  cloud={sig.get('cloud_gap_pct',0):+.2f}%"
            print(f"    {s} [{st.upper():<8}]  entry=${ep:.4f}  now=${cp:.4f}  "
                  f"{move:+.2%}  PnL=${pnl:+.2f}  bars={bars}{gap_str}")

print()
print(f"  Done. [{'4H close' if _is_4h_close else 'hourly exit'}]  Next: :05 past next hour")
print("=" * 55)