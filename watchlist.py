"""
watchlist.py — Daily full universe scan for the trading bot.
=============================================================
Run once per day via cron to refresh which coins are worth scanning hourly.

What it does:
  1. Fetches all active INR pairs from CoinSwitch (~400 coins)
  2. Fetches 4H candles for each coin
  3. Applies pre-filters: staleness → bar count → volume sanity
  4. Saves survivors to data/watchlist.json

The hourly main.py then reads this file instead of scanning all 400 coins,
reducing each hourly run from ~10-12 minutes to ~2-3 minutes.

Cron (runs at 00:05 UTC daily — add alongside the existing hourly cron):
    5 0 * * * cd ~/Projects/project_trade && TZ=Asia/Kolkata date >> logs/watchlist.log && /home/g12amandayal12/Projects/venv/bin/python watchlist.py >> logs/watchlist.log 2>&1

Output:
    data/watchlist.json  — list of coins that passed pre-filters
"""

from __future__ import annotations

import os
import json
import time
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from bot_utils import fetch_all_inr_pairs
from coinswitch_auth import signed_get
from config import (
    CS_EXCHANGE, CANDLES_LIMIT,
    TIMEFRAME_4H_MIN, INTERVAL_MS_4H, WARMUP_BARS_4H,
    DATA_DIR,
)

os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_FILE    = f"{DATA_DIR}/watchlist.json"
PREFILTER_STALE_H = 8      # max age of last 4H candle in hours
PREFILTER_MIN_BARS = 100   # minimum bars needed for indicators
PREFILTER_ZERO_VOL = 0.30  # reject if >30% of last 20 candles have zero volume


# =============================================================================
# Candle fetcher — lightweight version (fewer bars, just for pre-filtering)
# =============================================================================

def _fetch_4h_light(symbol: str) -> "pd.DataFrame":
    """
    Fetch last 150 bars of 4H candles — enough to pre-filter, not full backtest.
    Keeps the watchlist scan fast.
    """
    import pandas as pd

    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (150 * INTERVAL_MS_4H)
    all_rows = []
    cur      = start_ms

    while cur < now_ms:
        end_ms = min(cur + CANDLES_LIMIT * INTERVAL_MS_4H, now_ms)
        params = {
            "exchange":   CS_EXCHANGE,
            "symbol":     symbol,
            "interval":   str(TIMEFRAME_4H_MIN),
            "start_time": str(cur),
            "end_time":   str(end_ms),
        }

        for attempt in range(3):
            backoff = [0, 3, 10][attempt]
            if backoff:
                time.sleep(backoff)
            try:
                resp = signed_get("/trade/api/v2/candles", params=params, timeout=15)
            except Exception:
                continue
            if resp.status_code == 429:
                time.sleep(30)
                continue
            if resp.status_code != 200:
                continue
            try:
                candles = resp.json().get("data", [])
                if candles:
                    all_rows.extend(candles)
                break
            except Exception:
                continue

        cur = end_ms + INTERVAL_MS_4H
        time.sleep(0.25)   # lighter sleep than bot — watchlist runs offline

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = df.rename(columns={"o":"Open","h":"High","l":"Low","c":"Close",
                             "volume":"Volume","start_time":"timestamp"})
    if "timestamp" not in df.columns:
        return pd.DataFrame()

    df["timestamp"] = pd.to_datetime(
        pd.to_numeric(df["timestamp"], errors="coerce"), unit="ms", utc=True
    )
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for col in ["Open","High","Low","Close","Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open","High","Low","Close"])
    df = df[df["Close"] > 0]
    return df[["Open","High","Low","Close","Volume"]]


# =============================================================================
# Pre-filter
# =============================================================================

def _passes(symbol: str, df) -> tuple[bool, str]:
    if df.empty:
        return False, "empty"
    age_h = (datetime.now(timezone.utc) - df.index[-1]).total_seconds() / 3600
    if age_h > PREFILTER_STALE_H:
        return False, f"stale ({age_h:.1f}h)"
    if len(df) < PREFILTER_MIN_BARS:
        return False, f"thin history ({len(df)} bars)"
    last20    = df["Volume"].iloc[-20:]
    zero_frac = (last20 == 0).sum() / len(last20)
    if zero_frac > PREFILTER_ZERO_VOL:
        return False, f"low volume ({zero_frac:.0%} zero)"
    return True, ""


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    start_ts = datetime.now(timezone.utc)
    print("=" * 55)
    print(f"  WATCHLIST SCAN | {start_ts.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 55)

    # Step 1 — get universe
    print("  Fetching INR pair universe...")
    universe = fetch_all_inr_pairs()
    print(f"  {len(universe)} pairs found\n")

    # Step 2 — pre-filter scan
    watchlist    = []
    skipped      = {"stale": [], "bars": [], "volume": [], "fetch": []}
    total        = len(universe)

    for i, symbol in enumerate(universe, 1):
        print(f"  [{i:>3}/{total}] {symbol:<16}", end=" ", flush=True)
        df = _fetch_4h_light(symbol)
        ok, reason = _passes(symbol, df)
        if ok:
            watchlist.append(symbol)
            print(f"✓  ({len(df)} bars)")
        else:
            print(f"✗  {reason}")
            if "stale" in reason:   skipped["stale"].append(symbol)
            elif "history" in reason: skipped["bars"].append(symbol)
            elif "volume" in reason:  skipped["volume"].append(symbol)
            else:                     skipped["fetch"].append(symbol)

    # Step 3 — save
    output = {
        "updated_at": start_ts.isoformat(),
        "coins":      watchlist,
    }
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(output, f, indent=2)

    elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds() / 60
    print(f"\n{'=' * 55}")
    print(f"  Watchlist: {len(watchlist)} coins saved → {WATCHLIST_FILE}")
    print(f"  Skipped  : {len(skipped['stale'])} stale | "
          f"{len(skipped['bars'])} thin | "
          f"{len(skipped['volume'])} low-vol | "
          f"{len(skipped['fetch'])} fetch-fail")
    print(f"  Elapsed  : {elapsed:.1f} min")
    print("=" * 55)