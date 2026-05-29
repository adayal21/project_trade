"""
main.py — HMA + Ichimoku Combined Crypto Bot
=============================================
Both HMA and Ichimoku run independently on ALL 15 coins.
Each strategy manages its own separate position per coin.
Total potential slots: 30 (15 coins × 2 strategies).
Max active positions: 8 at any time, 10% allocation each.

Position files use strategy suffix:
    data/BTC_USDT_hma_position.csv
    data/BTC_USDT_ichi_position.csv

Priority when more than 8 signals fire simultaneously:
    1. Double-confirmed (both HMA AND Ichimoku fire on same coin) → first
    2. Single-strategy signals → by coin order in COINS list

Cron (unchanged):
    5 0,4,8,12,16,20 * * * cd ~/Projects/crypto_bot && python main.py >> data/bot.log 2>&1
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
    compute_hma_signals,
    compute_ichimoku_signals,
    notify_entry, notify_exit, notify_run_summary,
)
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)


# =============================================================================
# Position helpers — strategy-aware filenames
# =============================================================================

def _safe(symbol: str) -> str:
    return symbol.replace("/", "_")

def _strat_short(strategy: str) -> str:
    return "ichi" if strategy == "ichimoku" else "hma"

def get_position_file(symbol: str, strategy: str) -> str:
    return f"{DATA_DIR}/{_safe(symbol)}_{_strat_short(strategy)}_position.csv"

def get_trade_file(symbol: str, strategy: str) -> str:
    return f"{DATA_DIR}/{_safe(symbol)}_{_strat_short(strategy)}_trades.csv"

def load_position(symbol: str, strategy: str) -> dict | None:
    f = get_position_file(symbol, strategy)
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    if df.empty:
        os.remove(f)
        return None
    pos = df.iloc[0].to_dict()
    if float(pos.get("Quantity", 0)) <= 0:
        os.remove(f)
        return None
    return pos

def save_position(symbol: str, strategy: str, pos: dict) -> None:
    pd.DataFrame([pos]).to_csv(
        get_position_file(symbol, strategy), index=False
    )

def clear_position(symbol: str, strategy: str) -> None:
    f = get_position_file(symbol, strategy)
    if os.path.exists(f):
        os.remove(f)

def log_trade(symbol: str, strategy: str, trade: dict) -> None:
    f  = get_trade_file(symbol, strategy)
    df = pd.DataFrame([trade])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)

def count_open() -> int:
    total = 0
    for s in COINS:
        for strat in STRATEGIES:
            if load_position(s, strat) is not None:
                total += 1
    return total


# =============================================================================
# Portfolio helpers
# =============================================================================

def load_portfolio_state() -> dict:
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

def current_equity(cash: float, coins_data: dict) -> float:
    unreal = 0.0
    for s in COINS:
        for strat in STRATEGIES:
            pos = load_position(s, strat)
            if pos is None:
                continue
            px = (float(coins_data[s]["Close"].iloc[-1])
                  if s in coins_data else float(pos["Entry Price"]))
            unreal += float(pos["Quantity"]) * px
    return cash + unreal


# =============================================================================
# Startup
# =============================================================================

initialize_portfolio()

print("=" * 65)
print(f"  CRYPTO BOT — Dual Strategy  |  Mode: {TRADING_MODE.upper()}")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print(f"  Coins: {len(COINS)}  |  Strategies: {len(STRATEGIES)}  "
      f"|  Slots: {len(COINS)*len(STRATEGIES)}")
print(f"  Max positions: {MAX_OPEN_POSITIONS}  |  "
      f"Allocation: {ALLOCATION_PCT:.0%} per trade")
print("=" * 65)

if TRADING_MODE == "live":
    from exchange import get_live_usdt_balance, reconcile_positions
    print("Reconciling positions...")
    cash = get_live_usdt_balance()
    print(f"Live USDT balance: ${cash:.2f}")
    state = load_portfolio_state()
    realized_pnl = state["realized_pnl"]
    total_trades = state["total_trades"]
else:
    state        = load_portfolio_state()
    cash         = state["cash"]
    realized_pnl = state["realized_pnl"]
    total_trades = state["total_trades"]

run_events = []
trades_run = 0

print(f"  Cash         : ${cash:,.4f}")
print(f"  Realized PnL : ${realized_pnl:+,.4f}")
print(f"  Total trades : {total_trades}")
print(f"  Open pos     : {count_open()} / {MAX_OPEN_POSITIONS}")
print()


# =============================================================================
# Step 1 — Fetch candles (once per coin)
# =============================================================================
print("=" * 65)
print("  Fetching 4H candles from CoinDCX...")
print("=" * 65)

coins_data = {}
for symbol in COINS:
    df = fetch_candles(symbol)
    if df.empty or len(df) < 100:
        print(f"  {symbol:<14}  SKIP — only {len(df)} bars")
        continue
    coins_data[symbol] = df
    print(f"  {symbol:<14}  {len(df):>4} bars  "
          f"({df.index.min().strftime('%Y-%m-%d')} → "
          f"{df.index.max().strftime('%Y-%m-%d')})  "
          f"close={df['Close'].iloc[-1]:.4f}")
print()


# =============================================================================
# Step 2 — Compute both signals for every coin
# =============================================================================
print("=" * 65)
print("  Signal state  (HMA + Ichimoku on all 15 coins)")
print("=" * 65)
print(f"  {'Coin':<14} {'HMA':^22} {'ICHIMOKU':^26}")
print(f"  {'':14} {'RSI':>6} {'Gap%':>7} {'Sig':>6}  "
      f"{'TK':>4} {'Cloud%':>7} {'Sig':>6}")
print(f"  {'-'*60}")

all_signals = {}   # {(symbol, strategy): sig_dict}

for symbol, df in coins_data.items():
    hma_sig  = compute_hma_signals(symbol, df)
    ichi_sig = compute_ichimoku_signals(symbol, df)

    if hma_sig:
        all_signals[(symbol, "hma")] = hma_sig
    if ichi_sig:
        all_signals[(symbol, "ichimoku")] = ichi_sig

    # Build display
    if hma_sig:
        hma_rsi = f"{hma_sig['rsi']:>5.1f}"
        hma_gap = f"{hma_sig['hma_gap_pct']:>+6.2f}%"
        hma_sig_str = "ENTRY " if hma_sig["entry_signal"] else (
                      "EXIT  " if hma_sig["exit_signal"]  else "hold  ")
    else:
        hma_rsi = hma_gap = "  N/A"
        hma_sig_str = "ERROR "

    if ichi_sig:
        tk  = "BULL" if ichi_sig.get("tk_bull") else "bear"
        cg  = f"{ichi_sig.get('cloud_gap_pct',0):>+6.2f}%"
        ichi_sig_str = "ENTRY " if ichi_sig["entry_signal"] else (
                       "EXIT  " if ichi_sig["exit_signal"]  else "hold  ")
    else:
        tk = " N/A"; cg = "  N/A"
        ichi_sig_str = "ERROR "

    print(f"  {symbol:<14} {hma_rsi} {hma_gap} {hma_sig_str}  "
          f"{tk:>4} {cg} {ichi_sig_str}")
print()


# =============================================================================
# Step 3 — Exit pass (all 30 slots)
# =============================================================================
print("=" * 65)
print("  Exit pass")
print("=" * 65)

any_exit = False
for symbol in COINS:
    for strategy in STRATEGIES:
        pos = load_position(symbol, strategy)
        if pos is None:
            continue

        entry_price  = float(pos["Entry Price"])
        quantity     = float(pos["Quantity"])
        bars_held    = int(pos.get("Bars_Held", 0))
        sig          = all_signals.get((symbol, strategy))

        latest_price = (
            float(coins_data[symbol]["Close"].iloc[-1])
            if symbol in coins_data else entry_price
        )
        move_pct    = (latest_price - entry_price) / entry_price
        exit_reason = None

        # Stop loss — HMA only (Ichimoku uses Kijun as stop, checked in signal)
        if strategy == "hma" and move_pct <= -STOP_LOSS_PCT:
            exit_reason = "STOP_LOSS"
        elif sig and sig["exit_signal"]:
            if strategy == "ichimoku":
                exit_reason = ("TK_CROSS_DOWN" if sig.get("tk_bear")
                               else "BELOW_KIJUN")
            else:
                exit_reason = "HMA_CROSS_DOWN"

        if exit_reason is None:
            print(f"  {symbol:<14} [{strategy.upper():<8}]  "
                  f"HOLDING  entry={entry_price:.4f}  "
                  f"now={latest_price:.4f}  "
                  f"move={move_pct:+.2%}  bars={bars_held}")
            save_position(symbol, strategy,
                          {**pos, "Bars_Held": bars_held + 1})
            continue

        # Execute exit
        pnl     = move_pct * entry_price * quantity
        comm    = latest_price * quantity * COMMISSION
        net_pnl = pnl - comm if TRADING_MODE == "paper" else pnl
        sign    = "+" if net_pnl >= 0 else ""

        print(f"  {symbol:<14} [{strategy.upper():<8}]  "
              f"EXIT [{exit_reason}]  "
              f"{entry_price:.4f} → {latest_price:.4f}  "
              f"move={move_pct:+.2%}  PnL=${net_pnl:+.4f}")

        realized_pnl += net_pnl
        total_trades += 1
        trades_run   += 1
        cash         += entry_price * quantity + net_pnl
        any_exit      = True

        run_events.append(
            f"{symbol}[{strategy[:4].upper()}] {net_pnl:+.2f} {exit_reason}"
        )

        log_trade(symbol, strategy, {
            "Coin":        symbol,
            "Strategy":    strategy,
            "Side":        "LONG",
            "Entry Price": entry_price,
            "Exit Price":  round(latest_price, 8),
            "Quantity":    quantity,
            "PnL":         round(net_pnl, 6),
            "Exit Reason": exit_reason,
            "Exit Time":   datetime.now(timezone.utc).isoformat(),
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
# Step 4 — Entry pass with priority ordering
# =============================================================================
print("=" * 65)
print("  Entry pass  (priority: double-confirmed > single signal)")
print("=" * 65)

equity_now = current_equity(cash, coins_data)
open_count  = count_open()

# Build all entry candidates
raw_candidates = []
for symbol in COINS:
    for strategy in STRATEGIES:
        sig = all_signals.get((symbol, strategy))
        if sig and sig["entry_signal"] and load_position(symbol, strategy) is None:
            raw_candidates.append((symbol, strategy, sig))

# Check if the OTHER strategy also fired on same coin — used for priority
double_confirmed_coins = set()
entry_coins = {}
for symbol, strategy, sig in raw_candidates:
    if symbol not in entry_coins:
        entry_coins[symbol] = []
    entry_coins[symbol].append(strategy)
for symbol, strats in entry_coins.items():
    if len(strats) >= 2:
        double_confirmed_coins.add(symbol)

# Sort: double-confirmed first, then by COINS list order
def priority_key(candidate):
    symbol, strategy, sig = candidate
    is_double = 0 if symbol in double_confirmed_coins else 1
    coin_rank  = COINS.index(symbol) if symbol in COINS else 99
    strat_rank = 0 if strategy == "hma" else 1
    return (is_double, coin_rank, strat_rank)

candidates = sorted(raw_candidates, key=priority_key)

if not candidates:
    print("  No entry signals this run.")
else:
    print(f"  {len(candidates)} signal(s) found:")
    for symbol, strategy, sig in candidates:
        dbl = " ★DOUBLE" if symbol in double_confirmed_coins else ""
        print(f"    {symbol:<14} [{strategy.upper():<8}]{dbl}  "
              f"close={sig['close']:.4f}")
    print()

    for symbol, strategy, sig in candidates:
        if open_count >= MAX_OPEN_POSITIONS:
            print(f"  {symbol:<14} [{strategy.upper():<8}]  "
                  f"BLOCKED — max positions ({open_count}/{MAX_OPEN_POSITIONS})")
            continue

        allocation = equity_now * ALLOCATION_PCT
        if cash < allocation or allocation <= 0:
            print(f"  {symbol:<14} [{strategy.upper():<8}]  "
                  f"BLOCKED — insufficient cash "
                  f"(need ${allocation:.2f}, have ${cash:.2f})")
            continue

        latest_price = sig["close"]
        quantity     = (allocation * POSITION_SIZE) / latest_price

        dbl_str = " ★DOUBLE CONFIRMED" if symbol in double_confirmed_coins else ""
        print(f"  {symbol:<14} [{strategy.upper():<8}]  "
              f"ENTERING LONG{dbl_str}")
        print(f"    Price      : ${latest_price:.4f}")
        print(f"    Allocation : ${allocation:.2f}  "
              f"({ALLOCATION_PCT:.0%} of ${equity_now:.2f})")
        print(f"    Quantity   : {quantity:.6f}")

        if strategy == "ichimoku":
            print(f"    TK cross   : BULLISH")
            print(f"    Chikou     : {'OK' if sig.get('chikou_ok') else 'WEAK'}")
            print(f"    Cloud gap  : {sig.get('cloud_gap_pct',0):+.2f}%")
            print(f"    Stop       : TK cross down / below Kijun "
                  f"(${sig.get('kijun',0):.4f})")
        else:
            print(f"    RSI        : {sig.get('rsi',0):.1f}")
            print(f"    HMA gap    : {sig.get('hma_gap_pct',0):+.2f}%")
            print(f"    Stop       : ${latest_price*(1-STOP_LOSS_PCT):.4f}  "
                  f"(-{STOP_LOSS_PCT:.0%})")

        if TRADING_MODE == "live":
            from exchange import place_market_order, get_order_status
            import time as _t
            order_id = place_market_order(symbol, "buy", quantity)
            if order_id is None:
                print(f"    ORDER FAILED — skipping")
                continue
            _t.sleep(1.5)
            info        = get_order_status(order_id)
            fill_px     = float(info.get("avg_price", latest_price) or latest_price)
            fill_qty    = float(info.get("total_quantity", quantity) or quantity) \
                        - float(info.get("remaining_quantity", 0) or 0)
            fill_status = info.get("status", "unknown")
            if fill_status not in ("filled", "partially_filled"):
                print(f"    Not filled ({fill_status}) — skipping")
                continue
            latest_price = fill_px
            quantity     = fill_qty

        save_position(symbol, strategy, {
            "Coin":        symbol,
            "Strategy":    strategy,
            "Side":        "LONG",
            "Entry Price": latest_price,
            "Quantity":    quantity,
            "Timestamp":   datetime.now(timezone.utc).isoformat(),
            "Bars_Held":   0,
        })

        cash        -= allocation
        open_count  += 1
        total_trades += 1
        trades_run   += 1
        run_events.append(
            f"{symbol}[{strategy[:4].upper()}] ENTRY @ {latest_price:.4f}"
        )

        notify_entry(symbol, strategy, latest_price, quantity, allocation, sig)
        print(f"    Entered @ ${latest_price:.4f}")
        print()
print()


# =============================================================================
# Step 5 — Portfolio snapshot
# =============================================================================
equity_final = current_equity(cash, coins_data)
open_count   = count_open()

unrealized = 0.0
for s in COINS:
    for strat in STRATEGIES:
        pos = load_position(s, strat)
        if pos is None:
            continue
        ep = float(pos["Entry Price"])
        qty= float(pos["Quantity"])
        cp = float(coins_data[s]["Close"].iloc[-1]) if s in coins_data else ep
        unrealized += (cp - ep) * qty

log_portfolio(
    cash, equity_final, open_count,
    realized_pnl, unrealized, total_trades, run_events
)
notify_run_summary(equity_final, cash, open_count, realized_pnl)

print("=" * 65)
print("  Portfolio Snapshot")
print("=" * 65)
print(f"  Mode          : {TRADING_MODE.upper()}")
print(f"  Cash          : ${cash:,.4f}")
print(f"  Unrealized    : ${unrealized:+,.4f}")
print(f"  Equity        : ${equity_final:,.4f}")
print(f"  Realized PnL  : ${realized_pnl:+,.4f}")
print(f"  Return        : {(equity_final/INITIAL_CAPITAL-1)*100:+.2f}%")
print(f"  Open positions: {open_count} / {MAX_OPEN_POSITIONS}")
print(f"  Trades today  : {trades_run}")
print(f"  Total trades  : {total_trades}")
print()

if open_count > 0:
    print("=" * 65)
    print("  Open Positions")
    print("=" * 65)
    for s in COINS:
        for strat in STRATEGIES:
            pos = load_position(s, strat)
            if pos is None:
                continue
            ep   = float(pos["Entry Price"])
            qty  = float(pos["Quantity"])
            bars = int(pos.get("Bars_Held", 0))
            cp   = float(coins_data[s]["Close"].iloc[-1]) if s in coins_data else ep
            move = (cp - ep) / ep
            pnl  = move * ep * qty
            sign = "+" if pnl >= 0 else ""
            sig  = all_signals.get((s, strat))

            print(f"  {s}  [{strat.upper()}]")
            print(f"    Entry    : ${ep:.4f}")
            print(f"    Current  : ${cp:.4f}")
            print(f"    Move     : {move:+.2%}")
            print(f"    PnL      : {sign}${pnl:.4f}")
            print(f"    Bars held: {bars}")
            if sig:
                if strat == "ichimoku":
                    tk = "BULL" if not sig.get("tk_bear") else "BEAR — EXIT SOON"
                    print(f"    TK status: {tk}")
                    print(f"    Cloud gap: {sig.get('cloud_gap_pct',0):+.2f}%")
                else:
                    gap = sig.get("hma_gap_pct", 0)
                    print(f"    HMA gap  : {gap:+.2f}%  "
                          f"({'exit near' if gap > -0.5 else 'trend intact'})")
            print()

print("=" * 65)
print(f"  Done.  Next run: next 4H candle close + 5 min")
print("=" * 65)