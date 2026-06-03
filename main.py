"""
main.py — HMA + Ichimoku Combined Crypto Bot (CoinSwitch PRO)
=============================================================
Entry  : 4H candle close only (0,4,8,12,16,20 UTC)
Exit   : 1H candles, checked every hourly cron run — ALL coins

Coin universe: fetched live from CoinSwitch PRO at startup.
               All active INR pairs are scanned. No hardcoded list.

Cron:
    5 * * * * cd ~/Projects/project_trade && echo "========================================" >> logs/live.log && TZ=Asia/Kolkata date >> logs/live.log && /home/g12amandayal12/Projects/venv/bin/python main.py >> logs/live.log 2>&1
"""

import os
import glob
import pandas as pd
from datetime import datetime, timezone

from config import (
    TRADING_MODE, INITIAL_CAPITAL, STRATEGIES, DATA_DIR,
    ALLOCATION_PCT, MAX_OPEN_POSITIONS, POSITION_SIZE,
    COMMISSION, STOP_LOSS_PCT, VERBOSE,
)
from bot_utils import (
    fetch_candles,
    fetch_candles_1h,
    fetch_all_inr_pairs,
    compute_hma_signals,
    compute_hma_exit_1h,
    compute_ichimoku_signals,
    notify_signal_alert,
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

# =============================================================================
# Telegram tracking — only alert SELL/HOLD for coins we sent a BUY for
# =============================================================================

_TG_TRACKED_FILE = f"{DATA_DIR}/tg_tracked.json"

def _load_tg_tracked() -> set[tuple[str, str]]:
    """Load set of (symbol, strategy) pairs we've sent a BUY alert for."""
    import json
    try:
        with open(_TG_TRACKED_FILE) as f:
            raw = json.load(f)          # stored as list of [symbol, strategy]
        return {(r[0], r[1]) for r in raw}
    except Exception:
        return set()

def _save_tg_tracked(tracked: set[tuple[str, str]]) -> None:
    import json
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_TG_TRACKED_FILE, "w") as f:
            json.dump(sorted(tracked), f)
    except Exception:
        pass

def _tg_track(tracked: set, symbol: str, strategy: str) -> None:
    tracked.add((symbol, strategy))
    _save_tg_tracked(tracked)

def _tg_untrack(tracked: set, symbol: str, strategy: str) -> None:
    tracked.discard((symbol, strategy))
    _save_tg_tracked(tracked)


def get_coins_with_open_positions() -> list[str]:
    """
    Scan the data directory for any existing position files and return the
    unique coin symbols. This ensures we always manage positions that were
    opened under a previous coin universe, even if that coin is no longer
    in the live scan list.
    """
    coins = set()
    for path in glob.glob(f"{DATA_DIR}/*_position.csv"):
        fname = os.path.basename(path)                 # e.g. BTC_INR_hma_position.csv
        # strip the trailing _hma_position.csv or _ichi_position.csv
        for suffix in ["_hma_position.csv", "_ichi_position.csv"]:
            if fname.endswith(suffix):
                safe_sym = fname[: -len(suffix)]       # e.g. BTC_INR
                coins.add(safe_sym.replace("_", "/", 1))  # BTC/INR
                break
    return list(coins)

def count_open(universe: list[str]) -> int:
    return sum(1 for s in universe for st in STRATEGIES
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

def current_equity(cash, coins_data, universe: list[str]) -> float:
    val = 0.0
    for s in universe:
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

# ---------------------------------------------------------------------------
# Load coin universe from daily watchlist (fast) — falls back to live API
# ---------------------------------------------------------------------------
_WATCHLIST_FILE = f"{DATA_DIR}/watchlist.json"
_watchlist_age_h = None

UNIVERSE = []

# Try loading the daily watchlist first
try:
    import json as _json
    with open(_WATCHLIST_FILE) as _f:
        _wl = _json.load(_f)
    _updated = datetime.fromisoformat(_wl["updated_at"])
    _watchlist_age_h = (datetime.now(timezone.utc) - _updated).total_seconds() / 3600
    if _watchlist_age_h <= 26:    # accept watchlist up to 26h old (covers daily refresh)
        UNIVERSE = _wl["coins"]
        print(f"  Watchlist loaded: {len(UNIVERSE)} coins (updated {_watchlist_age_h:.1f}h ago)")
    else:
        print(f"  Watchlist too old ({_watchlist_age_h:.1f}h) — falling back to live API fetch")
except Exception as _e:
    print(f"  No watchlist found ({_e}) — falling back to live API fetch")

# Fallback: fetch live from CoinSwitch if watchlist missing or too old
if not UNIVERSE:
    print("  Fetching live INR pair universe from CoinSwitch...")
    UNIVERSE = fetch_all_inr_pairs()
    print(f"  Live universe: {len(UNIVERSE)} INR pairs")

# Always include coins with open positions — even if dropped from watchlist
# (delisted coins, stale coins we're still holding — must be able to exit)
position_coins = get_coins_with_open_positions()
extra_pos = [c for c in position_coins if c not in UNIVERSE]
if extra_pos:
    print(f"  + {len(extra_pos)} coin(s) with open positions added: {extra_pos}")
    UNIVERSE = extra_pos + UNIVERSE

# Always include TG-tracked coins (we sent a BUY — must be able to send SELL)
_tg_tracked_preview = _load_tg_tracked()
extra_tg = [c for c, _ in _tg_tracked_preview if c not in UNIVERSE]
if extra_tg:
    print(f"  + {len(extra_tg)} TG-tracked coin(s) added: {extra_tg}")
    UNIVERSE = extra_tg + UNIVERSE

print(f"  Universe: {len(UNIVERSE)} coins for this run")

# Load Telegram tracking state (persists across runs)
tg_tracked = _load_tg_tracked()
print(f"  TG tracked: {len(tg_tracked)} (symbol, strategy) pair(s)")
print()

state        = load_portfolio_state() if TRADING_MODE != "live" else None
if TRADING_MODE == "live":
    from exchange import get_live_inr_balance
    cash         = get_live_inr_balance()
    state        = load_portfolio_state()
    realized_pnl = state["realized_pnl"]
    total_trades = state["total_trades"]
else:
    cash         = state["cash"]
    realized_pnl = state["realized_pnl"]
    total_trades = state["total_trades"]

run_events = []
trades_run = 0

open_now = count_open(UNIVERSE)
print(f"  Cash: ₹{cash:,.2f}  |  Open: {open_now}/{MAX_OPEN_POSITIONS}  |  Trades: {total_trades}  |  PnL: ₹{realized_pnl:+,.2f}")
print()


# =============================================================================
# Step 1 — Fetch candles (4H for signals/entries, 1H for exits on all coins)
# =============================================================================

coins_data    = {}   # 4H candles — entries + Ichimoku signals
coins_data_1h = {}   # 1H candles — HMA exit for ALL coins

fetch_errors = []
total = len(UNIVERSE)

for i, symbol in enumerate(UNIVERSE, 1):
    print(f"  [{i}/{total}] {symbol} ...", end=" ", flush=True)

    # Fetch 4H candles
    df4h = fetch_candles(symbol)
    if df4h.empty or len(df4h) < 100:
        print(f"fetch failed/insufficient — skip")
        fetch_errors.append(symbol)
        continue

    # Staleness guard — still check even on watchlist coins
    # (a coin can go stale between daily watchlist refresh and hourly run)
    age_4h = (datetime.now(timezone.utc) - df4h.index[-1]).total_seconds() / 3600
    if age_4h > 8:
        print(f"STALE ({age_4h:.1f}h) — skip")
        fetch_errors.append(symbol)
        continue

    coins_data[symbol] = df4h

    # Fetch 1H candles for exit detection
    df1h = fetch_candles_1h(symbol)
    ok_1h = ""
    if not df1h.empty and len(df1h) >= 80:
        age_1h = (datetime.now(timezone.utc) - df1h.index[-1]).total_seconds() / 3600
        if age_1h <= 2:
            coins_data_1h[symbol] = df1h
            ok_1h = "✓1H"

    print(f"✓ {ok_1h}")

print()
if fetch_errors:
    print(f"  Skipped {len(fetch_errors)} coin(s) this run (stale/fetch-fail): {', '.join(fetch_errors)}")
SCANNED = list(coins_data.keys())
print(f"  Fetched: {len(SCANNED)} / {len(UNIVERSE)} coins with valid candles")
print()


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
for symbol in UNIVERSE:   # use full UNIVERSE here — must manage all open positions
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
            if move_pct <= -STOP_LOSS_PCT:
                exit_reason = "STOP_LOSS"
            else:
                if symbol in coins_data_1h:
                    sig_1h = compute_hma_exit_1h(symbol, coins_data_1h[symbol])
                    if sig_1h and sig_1h["exit_signal"]:
                        exit_reason = "HMA_1H_CROSS"
                else:
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
              f"{entry_price:.4f}→{latest_price:.4f}  {move_pct:+.2%}  PnL=₹{net_pnl:+.2f}")

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

        sig_for_alert = all_signals.get((symbol, strategy), {})
        if (symbol, strategy) in tg_tracked:
            notify_signal_alert(
                symbol        = symbol,
                strategy      = strategy,
                action        = "SELL",
                price         = latest_price,
                rsi           = sig_for_alert.get("rsi"),
                gap_pct       = sig_for_alert.get("hma_gap_pct") if strategy == "hma" else None,
                cloud_gap_pct = sig_for_alert.get("cloud_gap_pct") if strategy == "ichimoku" else None,
            )
            _tg_untrack(tg_tracked, symbol, strategy)

        if TRADING_MODE == "live":
            from exchange import place_market_order
            place_market_order(symbol, "sell", quantity)

        clear_position(symbol, strategy)

if not any_exit and count_open(UNIVERSE) == 0:
    print("  No open positions.")
print()

# ── Sell signal alerts — only for coins we previously sent a BUY for ──
# (coins without open positions but still tracked — e.g. signal flipped same run)
for symbol, strategy in list(tg_tracked):
    if load_position(symbol, strategy) is not None:
        continue   # already handled in exit pass above
    sig = all_signals.get((symbol, strategy))
    if not sig or not sig.get("exit_signal"):
        continue
    notify_signal_alert(
        symbol        = symbol,
        strategy      = strategy,
        action        = "SELL",
        price         = sig["close"],
        rsi           = sig.get("rsi"),
        gap_pct       = sig.get("hma_gap_pct")   if strategy == "hma"      else None,
        cloud_gap_pct = sig.get("cloud_gap_pct") if strategy == "ichimoku" else None,
    )
    _tg_untrack(tg_tracked, symbol, strategy)


# =============================================================================
# Step 4 — Entry pass (4H candle closes only)
# =============================================================================

print("=" * 55)
print("  Entry pass")
print("=" * 55)

equity_now = current_equity(cash, coins_data, UNIVERSE)
open_count = count_open(UNIVERSE)

if not _is_4h_close:
    print("  Skipped — not a 4H close.")
    raw_candidates = []
else:
    raw_candidates = [
        (sym, strat, sig)
        for sym in SCANNED          # only coins we successfully fetched
        for strat in STRATEGIES
        if (sig := all_signals.get((sym, strat))) and
           sig["entry_signal"] and
           load_position(sym, strat) is None
    ]

# Double-confirmed priority — coins where BOTH strategies fire
entry_coins = {}
for sym, strat, sig in raw_candidates:
    entry_coins.setdefault(sym, []).append(strat)
double_confirmed = {sym for sym, strats in entry_coins.items() if len(strats) >= 2}

# Sort: double-confirmed first, then alphabetical within each tier
candidates = sorted(raw_candidates, key=lambda c: (
    0 if c[0] in double_confirmed else 1,
    c[0],                             # alphabetical — fair ordering across 100+ coins
    0 if c[1] == "hma" else 1,
))

if not candidates:
    print("  No entry signals." if _is_4h_close else "")
else:
    print(f"  {len(candidates)} signal(s) from {len(SCANNED)} coins scanned:")
    for sym, strat, sig in candidates:
        dbl = " ★DOUBLE" if sym in double_confirmed else ""
        print(f"    {sym:<14} [{strat.upper():<8}]{dbl}  @ ₹{sig['close']:.4f}")
    print()

    # ── Signal alerts — fire for ALL qualifying coins, register in tracker ──
    for sym, strat, sig in candidates:
        notify_signal_alert(
            symbol        = sym,
            strategy      = strat,
            action        = "BUY",
            price         = sig["close"],
            rsi           = sig.get("rsi"),
            gap_pct       = sig.get("hma_gap_pct")    if strat == "hma"      else None,
            cloud_gap_pct = sig.get("cloud_gap_pct")  if strat == "ichimoku" else None,
        )
        _tg_track(tg_tracked, sym, strat)   # register — SELL alerts now enabled for this coin

    # ── Paper trading execution — limited to MAX_OPEN_POSITIONS ──
    for sym, strat, sig in candidates:
        if open_count >= MAX_OPEN_POSITIONS:
            print(f"  {sym} [{strat.upper()}]  BLOCKED — max positions")
            continue
        allocation = equity_now * ALLOCATION_PCT
        if cash < allocation or allocation <= 0:
            print(f"  {sym} [{strat.upper()}]  BLOCKED — insufficient cash")
            continue

        # 1H confirmation filter
        if strat == "hma":
            df1h = coins_data_1h.get(sym)
            if df1h is not None:
                sig_1h = compute_hma_exit_1h(sym, df1h)
                if sig_1h and sig_1h["hma_fast"] <= sig_1h["hma_slow"]:
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
              f"@ ₹{latest_price:.4f}  qty={quantity:.6f}  alloc=₹{allocation:.2f}")

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

        cash         -= allocation
        open_count   += 1
        total_trades += 1
        trades_run   += 1
        run_events.append(f"{sym}[{strat[:4].upper()}] ENTRY @ {latest_price:.4f}")

print()


# =============================================================================
# Step 5 — Portfolio snapshot
# =============================================================================

equity_final = current_equity(cash, coins_data, UNIVERSE)
open_count   = count_open(UNIVERSE)
unrealized   = 0.0
for s in UNIVERSE:
    for st in STRATEGIES:
        pos = load_position(s, st)
        if pos is None: continue
        ep  = float(pos["Entry Price"])
        qty = float(pos["Quantity"])
        cp  = float(coins_data[s]["Close"].iloc[-1]) if s in coins_data else ep
        unrealized += (cp - ep) * qty

log_portfolio(cash, equity_final, open_count, realized_pnl, unrealized, total_trades, run_events)

# ── Heartbeat — every 6 hours ──
_heartbeat_file = f"{DATA_DIR}/last_heartbeat.txt"
_now_ts  = _now_utc.timestamp()
_last_hb = 0.0
try:
    with open(_heartbeat_file) as _f:
        _last_hb = float(_f.read().strip())
except Exception:
    pass

if _now_ts - _last_hb >= 6 * 3600:
    import zoneinfo as _zi
    _ist = _now_utc.astimezone(_zi.ZoneInfo("Asia/Kolkata"))
    from bot_utils import _telegram
    _telegram(
        f"🤖 Bot running — {_ist.strftime('%d %b %Y, %I:%M %p')} IST\n"
        f"Universe: {len(SCANNED)} coins scanned  |  Open: {open_count}/{MAX_OPEN_POSITIONS}"
    )
    try:
        with open(_heartbeat_file, "w") as _f:
            _f.write(str(_now_ts))
    except Exception:
        pass

ret_pct = (equity_final / INITIAL_CAPITAL - 1) * 100
print("=" * 55)
print(f"  Cash: ₹{cash:,.2f}  Unreal: ₹{unrealized:+,.2f}  Equity: ₹{equity_final:,.2f}")
print(f"  Return: {ret_pct:+.2f}%  |  PnL: ₹{realized_pnl:+,.2f}  |  Open: {open_count}/{MAX_OPEN_POSITIONS}")

# Open positions summary
if open_count > 0:
    print()
    print("  Open positions:")
    for s in UNIVERSE:
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
                gap   = sig.get("hma_gap_pct", 0)
                label = "ext" if gap > 5 else "ok" if gap > 1 else "weak" if gap > 0 else "EXIT"
                gap_str = f"  gap={gap:+.2f}%({label})"
            elif sig and st == "ichimoku":
                gap_str = f"  cloud={sig.get('cloud_gap_pct',0):+.2f}%"
            print(f"    {s} [{st.upper():<8}]  entry=₹{ep:.4f}  now=₹{cp:.4f}  "
                  f"{move:+.2%}  PnL=₹{pnl:+.2f}  bars={bars}{gap_str}")

print()
print(f"  Done. [{'4H close' if _is_4h_close else 'hourly exit'}]  Next: :05 past next hour")
print(f"  Scanned {len(SCANNED)} / {len(UNIVERSE)} INR pairs")
print("=" * 55)