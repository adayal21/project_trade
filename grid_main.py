"""
grid_main.py — Standalone Grid Bot Runner
==========================================
Runs every hour via separate cron job.
Fetches 1H candles from CoinDCX for grid coins only.
Completely independent of main.py (HMA + Ichimoku bot).

Cron:
    5 * * * * cd ~/Projects/crypto_bot && python grid_main.py >> data/grid/grid.log 2>&1
"""

import os
import requests
import time
import pandas as pd
from datetime import datetime, timezone

from grid_bot import run_grid_bot
from grid_config import GRID_COINS, GRID_DATA_DIR
from config import CANDLES_URL, CANDLES_LIMIT, WARMUP_BARS

os.makedirs(GRID_DATA_DIR, exist_ok=True)

INTERVAL      = "30m"
INTERVAL_MS   = 1_800_000
WARMUP_BARS   = 336           # 336 bars of 30m = ~7 days


def fetch_1h_candles(symbol: str) -> pd.DataFrame:
    """Fetch 30m candles from CoinDCX for a single symbol."""
    base, quote = symbol.split("/")
    pair        = f"KC-{base}_{quote}"
    now_ms      = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms    = now_ms - (WARMUP_BARS * INTERVAL_MS)
    all_rows    = []
    cur         = start_ms
    retries     = 0

    while cur < now_ms:
        end_ms = min(cur + CANDLES_LIMIT * INTERVAL_MS, now_ms)
        params = {
            "pair": pair, "interval": INTERVAL,
            "startTime": cur, "endTime": end_ms,
            "limit": CANDLES_LIMIT,
        }
        try:
            resp = requests.get(CANDLES_URL, params=params, timeout=20)
        except requests.exceptions.RequestException:
            retries += 1
            if retries > 3:
                break
            time.sleep(3)
            continue

        if resp.status_code == 429:
            time.sleep(15)
            continue
        if resp.status_code != 200:
            retries += 1
            if retries > 3:
                break
            time.sleep(3)
            continue

        retries = 0
        try:
            candles = resp.json()
        except Exception:
            break

        if isinstance(candles, list) and candles:
            all_rows.extend(candles)
        cur = end_ms + INTERVAL_MS
        time.sleep(0.3)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    ts_col = next(
        (c for c in ["time","timestamp","open_time","ts"] if c in df.columns), None
    )
    if ts_col is None:
        return pd.DataFrame()

    df = df.rename(columns={
        ts_col: "timestamp", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["timestamp"] = pd.to_datetime(
        pd.to_numeric(df["timestamp"], errors="coerce"),
        unit="ms", utc=True
    )
    df = df.dropna(subset=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for col in ["Open","High","Low","Close","Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open","High","Low","Close"])
    df = df[df["Close"] > 0]

    return df[["Open","High","Low","Close","Volume"]]


# =============================================================================
# Main
# =============================================================================

print("=" * 65)
print(f"  GRID BOT RUNNER  |  30m candles  |  CoinDCX")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print("=" * 65)

coins_data = {}
for symbol in GRID_COINS:
    df = fetch_1h_candles(symbol)
    if df.empty or len(df) < 50:
        print(f"  {symbol:<14}  SKIP — only {len(df)} bars")
        continue
    coins_data[symbol] = df
    print(f"  {symbol:<14}  {len(df):>4} bars  "
          f"({df.index.min().strftime('%Y-%m-%d %H:%M')} → "
          f"{df.index.max().strftime('%Y-%m-%d %H:%M')})  "
          f"close={df['Close'].iloc[-1]:.4f}")

print()
run_grid_bot(coins_data)