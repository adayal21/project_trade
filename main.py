"""
main.py — HMA 4H Trend Bot
==========================
Runs every 4 hours via cron. On each run:
    1. Fetch 4H candles for all 6 coins from CoinDCX
    2. Compute HMA + RSI + LinReg signals via utils.prepare_dataset()
    3. Exit pass  — check stop loss + HMA cross-down on every open position
    4. Entry pass — check HMA cross-up + RSI + LinReg on every flat coin
    5. Log portfolio snapshot

Files this bot reads/writes (all in data/ folder):
    data/{COIN}_USDT_position.csv  — open position state per coin
    data/{COIN}_USDT_trades.csv    — full trade history per coin
    data/portfolio.csv             — equity curve (written by portfolio.py)
    data/btc_regime_prev.txt       — not used (old bot artefact, ignored)

Files that are UNCHANGED and shared with any other bots:
    portfolio.py  — log_portfolio(), initialize_portfolio()
    exchange.py   — place_market_order(), get_order_status()
    utils.py      — HMA / RSI / LinReg indicator logic (YouTuber's file)
    bot_utils.py  — data fetch + signal wrapper around utils.py
    config.py     — all settings
"""

import os
import sys
import pandas as pd
from datetime import datetime, timezone

from config import (
    TRADING_MODE, INITIAL_CAPITAL, COINS, DATA_DIR,
    ALLOCATION_PCT, MAX_OPEN_POSITIONS, POSITION_SIZE,
    COMMISSION, STOP_LOSS_PCT, VERBOSE,
)
from bot_utils import (
    fetch_candles, compute_signals,
    notify_entry, notify_exit, notify_run_summary,
)
from portfolio import initialize_portfolio, log_portfolio

os.makedirs(DATA_DIR, exist_ok=True)


# =============================================================================
# Position file helpers  (stored in data/ alongside the old bot's files)
# =============================================================================

def _safe(symbol: str) -> str:
    """'BTC/USDT' → 'BTC_USDT'"""
    return symbol.replace("/", "_")


def get_position_file(symbol: str) -> str:
    return f"{DATA_DIR}/{_safe(symbol)}_position.csv"


def get_trade_file(symbol: str) -> str:
    return f"{DATA_DIR}/{_safe(symbol)}_trades.csv"


def load_position(symbol: str) -> dict | None:
    f = get_position_file(symbol)
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


def save_position(symbol: str, pos: dict) -> None:
    pd.DataFrame([pos]).to_csv(get_position_file(symbol), index=False)


def clear_position(symbol: str) -> None:
    f = get_position_file(symbol)
    if os.path.exists(f):
        os.remove(f)


def log_trade(symbol: str, trade: dict) -> None:
    f  = get_trade_file(symbol)
    df = pd.DataFrame([trade])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)


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
    """Cash + mark-to-market value of all open positions."""
    unreal = 0.0
    for s in COINS:
        pos = load_position(s)
        if pos is None:
            continue
        px = (float(coins_data[s]["Close"].iloc[-1])
              if s in coins_data and not coins_data[s].empty
              else float(pos["Entry Price"]))
        unreal += float(pos["Quantity"]) * px
    return cash + unreal


def count_open() -> int:
    return sum(1 for s in COINS if load_position(s) is not None)


# =============================================================================
# Startup
# =============================================================================

initialize_portfolio()

print("=" * 58)
print(f"  HMA 4H Trend Bot  |  Mode: {TRADING_MODE.upper()}")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print("=" * 58)

state        = load_portfolio_state()
realized_pnl = state["realized_pnl"]
total_trades = state["total_trades"]
run_events   = []
trades_run   = 0

if TRADING_MODE == "live":
    from exchange import get_live_usdt_balance, reconcile_positions
    print("Reconciling positions with CoinDCX account...")
    reconcile_positions(COINS, load_position, save_position, clear_position)
    cash = get_live_usdt_balance()
    print(f"Live USDT balance: ${cash:.2f}")
else:
    cash = state["cash"]

print(f"  Cash         : ${cash:,.4f}")
print(f"  Realized PnL : ${realized_pnl:+,.4f}")
print(f"  Total trades : {total_trades}")
print()

# =============================================================================
# Step 1 — Fetch candles for all coins
# =============================================================================
print("=" * 58)
print("  Fetching 4H candles from CoinDCX...")
print("=" * 58)

coins_data = {}
for symbol in COINS:
    df = fetch_candles(symbol)
    if df.empty or len(df) < 100:
        print(f"  {symbol:<12}  SKIP — only {len(df)} bars returned")
        continue
    coins_data[symbol] = df
    print(f"  {symbol:<12}  {len(df):>4} bars  "
          f"({df.index.min().strftime('%Y-%m-%d')} → "
          f"{df.index.max().strftime('%Y-%m-%d')})  "
          f"last close: {df['Close'].iloc[-1]:.4f}")
print()

# =============================================================================
# Step 2 — Compute signals
# =============================================================================
print("=" * 58)
print("  Signal state")
print("=" * 58)
print(f"  {'Coin':<12} {'Close':>10} {'RSI':>6} {'HMAfast':>10} "
      f"{'HMAslow':>10} {'Gap%':>7} {'Entry':>7} {'Exit':>6}")
print(f"  {'-'*72}")

all_signals = {}
for symbol, df in coins_data.items():
    sig = compute_signals(symbol, df)
    if sig is None:
        print(f"  {symbol:<12}  signal computation failed")
        continue
    all_signals[symbol] = sig
    e_flag = " ENTRY" if sig["entry_long"] else "      "
    x_flag = "  EXIT" if sig["exit_long"]  else "      "
    print(f"  {symbol:<12} "
          f"{sig['close']:>10.4f} "
          f"{sig['rsi']:>6.1f} "
          f"{sig['hma_fast']:>10.4f} "
          f"{sig['hma_slow']:>10.4f} "
          f"{sig['hma_gap_pct']:>+7.2f}%"
          f"{e_flag}{x_flag}")
print()

# =============================================================================
# Step 3 — Exit pass  (always before entries)
# =============================================================================
print("=" * 58)
print("  Exit pass")
print("=" * 58)

any_exit = False
for symbol in COINS:
    pos = load_position(symbol)
    if pos is None:
        continue

    entry_price = float(pos["Entry Price"])
    quantity    = float(pos["Quantity"])

    latest_price = (
        float(coins_data[symbol]["Close"].iloc[-1])
        if symbol in coins_data and not coins_data[symbol].empty
        else entry_price
    )

    move_pct    = (latest_price - entry_price) / entry_price
    exit_reason = None

    # --- Stop loss (hard exit, checked first) ---
    if move_pct <= -STOP_LOSS_PCT:
        exit_reason = "STOP_LOSS"

    # --- HMA cross-down signal ---
    elif symbol in all_signals and all_signals[symbol]["exit_long"]:
        exit_reason = "HMA_CROSS_DOWN"

    if exit_reason is None:
        # Still holding — print status line
        bars_held = int(pos.get("Bars_Held", 0))
        print(f"  {symbol:<12}  HOLDING  "
              f"entry={entry_price:.4f}  "
              f"now={latest_price:.4f}  "
              f"move={move_pct:+.2%}  "
              f"bars={bars_held}")
        save_position(symbol, {**pos, "Bars_Held": bars_held + 1})
        continue

    # --- Execute exit ---
    pnl          = move_pct * entry_price * quantity
    commission_cost = latest_price * quantity * COMMISSION
    net_pnl      = pnl - commission_cost if TRADING_MODE == "paper" else pnl
    sign         = "+" if net_pnl >= 0 else ""

    print(f"  {symbol:<12}  EXIT [{exit_reason}]  "
          f"entry={entry_price:.4f} → now={latest_price:.4f}  "
          f"move={move_pct:+.2%}  PnL=${net_pnl:+.4f}")

    realized_pnl += net_pnl
    total_trades += 1
    trades_run   += 1
    cash         += entry_price * quantity + net_pnl
    any_exit      = True

    run_events.append(f"{symbol} {net_pnl:+.2f} {exit_reason}")

    log_trade(symbol, {
        "Coin":        symbol,
        "Side":        "LONG",
        "Entry Price": entry_price,
        "Exit Price":  round(latest_price, 8),
        "Quantity":    quantity,
        "PnL":         round(net_pnl, 6),
        "Exit Reason": exit_reason,
        "Exit Time":   datetime.now(timezone.utc).isoformat(),
    })

    notify_exit(symbol, entry_price, latest_price, quantity, net_pnl, exit_reason)

    if TRADING_MODE == "live":
        from exchange import place_market_order
        place_market_order(symbol, "sell", quantity)

    clear_position(symbol)

if not any_exit and count_open() == 0:
    print("  No open positions.")
print()

# =============================================================================
# Step 4 — Entry pass
# =============================================================================
print("=" * 58)
print("  Entry pass")
print("=" * 58)

# Recalculate equity after exits
equity_now = current_equity(cash, coins_data)
open_count  = count_open()

entry_signals = [
    (s, all_signals[s])
    for s in COINS
    if s in all_signals
    and all_signals[s]["entry_long"]
    and load_position(s) is None
]

if not entry_signals:
    print("  No entry signals this run.")
else:
    print(f"  {len(entry_signals)} signal(s) found:")
    for symbol, sig in entry_signals:
        print(f"    {symbol}  RSI={sig['rsi']:.1f}  "
              f"HMAcross={'Y' if sig['c_cross'] else 'N'}  "
              f"RSIok={'Y' if sig['c_rsi'] else 'N'}  "
              f"LinRegok={'Y' if sig['c_linreg'] else 'N'}")
    print()

    for symbol, sig in entry_signals:

        # Gate 1 — position count
        if open_count >= MAX_OPEN_POSITIONS:
            print(f"  {symbol:<12}  BLOCKED — max positions "
                  f"({open_count}/{MAX_OPEN_POSITIONS})")
            continue

        # Gate 2 — capital
        allocation = equity_now * ALLOCATION_PCT
        if cash < allocation or allocation <= 0:
            print(f"  {symbol:<12}  BLOCKED — insufficient cash "
                  f"(need ${allocation:.2f}, have ${cash:.2f})")
            continue

        latest_price = sig["close"]
        quantity     = (allocation * POSITION_SIZE) / latest_price
        commission_cost = latest_price * quantity * COMMISSION

        print(f"  {symbol:<12}  ENTERING LONG")
        print(f"    Price      : ${latest_price:.4f}")
        print(f"    Allocation : ${allocation:.2f}  "
              f"({ALLOCATION_PCT:.0%} of ${equity_now:.2f} equity)")
        print(f"    Quantity   : {quantity:.6f}")
        print(f"    Commission : ${commission_cost:.4f}")
        print(f"    Stop loss  : ${latest_price * (1 - STOP_LOSS_PCT):.4f}  "
              f"(-{STOP_LOSS_PCT:.0%})")
        print(f"    RSI        : {sig['rsi']:.1f}")
        print(f"    HMA fast   : {sig['hma_fast']:.4f}")
        print(f"    HMA slow   : {sig['hma_slow']:.4f}")
        print(f"    LinReg     : {sig['linreg']:.4f}")

        # Live mode — place real order
        if TRADING_MODE == "live":
            from exchange import place_market_order, get_order_status
            import time as _t
            order_id = place_market_order(symbol, "buy", quantity)
            if order_id is None:
                print(f"    ORDER FAILED — skipping")
                continue
            _t.sleep(1.5)
            info        = get_order_status(order_id)
            fill_px     = float(info.get("avg_price",          latest_price) or latest_price)
            fill_qty    = float(info.get("total_quantity",      quantity)     or quantity) \
                        - float(info.get("remaining_quantity",  0)            or 0)
            fill_status = info.get("status", "unknown")
            print(f"    Order {order_id}: {fill_status}  "
                  f"fill={fill_px:.4f}  qty={fill_qty:.6f}")
            if fill_status not in ("filled", "partially_filled"):
                print(f"    Not filled — skipping")
                continue
            latest_price = fill_px
            quantity     = fill_qty

        # Save position
        save_position(symbol, {
            "Coin":        symbol,
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
        run_events.append(f"{symbol} ENTRY LONG @ {latest_price:.4f}")

        notify_entry(symbol, latest_price, quantity, allocation, sig)
        print(f"    Entered @ ${latest_price:.4f}")
        print()

print()

# =============================================================================
# Step 5 — Portfolio snapshot
# =============================================================================
equity_final = current_equity(cash, coins_data)
open_count   = count_open()
unrealized   = equity_final - cash

log_portfolio(
    cash, equity_final, open_count,
    realized_pnl, unrealized, total_trades, run_events
)

notify_run_summary(equity_final, cash, open_count, realized_pnl)

print("=" * 58)
print("  Portfolio Snapshot")
print("=" * 58)
print(f"  Mode          : {TRADING_MODE.upper()}")
print(f"  Cash          : ${cash:,.4f}")
print(f"  Unrealized    : ${unrealized:+,.4f}")
print(f"  Equity        : ${equity_final:,.4f}")
print(f"  Realized PnL  : ${realized_pnl:+,.4f}")
print(f"  Return        : {(equity_final / INITIAL_CAPITAL - 1) * 100:+.2f}%")
print(f"  Open positions: {open_count} / {MAX_OPEN_POSITIONS}")
print(f"  Trades today  : {trades_run}")
print(f"  Total trades  : {total_trades}")
print()

# ── Open positions detail ─────────────────────────────────────────────────────
if open_count > 0:
    print("=" * 58)
    print("  Open Positions")
    print("=" * 58)
    for symbol in COINS:
        pos = load_position(symbol)
        if pos is None:
            continue
        entry_px  = float(pos["Entry Price"])
        qty       = float(pos["Quantity"])
        bars_held = int(pos.get("Bars_Held", 0))
        cur_px    = (float(coins_data[symbol]["Close"].iloc[-1])
                     if symbol in coins_data and not coins_data[symbol].empty
                     else entry_px)
        move_pct  = (cur_px - entry_px) / entry_px
        pnl       = move_pct * entry_px * qty
        sign      = "+" if pnl >= 0 else ""

        print(f"  {symbol}")
        print(f"    Entry      : ${entry_px:.4f}")
        print(f"    Current    : ${cur_px:.4f}")
        print(f"    Move       : {move_pct:+.2%}")
        print(f"    PnL        : {sign}${pnl:.4f}")
        print(f"    Stop loss  : ${entry_px * (1 - STOP_LOSS_PCT):.4f}")
        print(f"    Bars held  : {bars_held}")
        if symbol in all_signals:
            gap = all_signals[symbol]["hma_gap_pct"]
            print(f"    HMA gap    : {gap:+.2f}%  "
                  f"({'exit near' if gap > -0.5 else 'trend intact'})")
        print()

print("=" * 58)
print("  Done.")
print("=" * 58)