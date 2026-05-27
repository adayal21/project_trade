"""
fetch_ohlcv.py
==============
Run this script LOCALLY to download 4H OHLCV candles for any coin and save
them as a CSV. Once you have the CSV, pass it to run_backtest.py.

Supports three exchanges — whichever works for you:
  1. Binance     (default, no API key needed)
  2. Bybit       (fallback)
  3. KuCoin      (fallback)

Usage
-----
    # BTC (default)
    python fetch_ohlcv.py

    # Any other coin — just pass --symbol
    python fetch_ohlcv.py --symbol ETH/USDT
    python fetch_ohlcv.py --symbol SOL/USDT
    python fetch_ohlcv.py --symbol XRP/USDT
    python fetch_ohlcv.py --symbol BNB/USDT

    # --out is auto-named from symbol if not specified
    # ETH/USDT → eth_usdt_4h.csv, SOL/USDT → sol_usdt_4h.csv etc.

    # Custom date range
    python fetch_ohlcv.py --symbol SOL/USDT --since 2020-01-01

    # Use a different exchange
    python fetch_ohlcv.py --symbol BNB/USDT --exchange bybit

    # Explicit output path
    python fetch_ohlcv.py --symbol XRP/USDT --out my_xrp.csv

Install requirements (if not already installed):
    pip install ccxt pandas

Output CSV columns:
    timestamp, Open, High, Low, Close, Volume
    (timestamp is UTC ISO-8601, e.g. 2021-01-01T00:00:00+00:00)
"""

import argparse
import time
import sys

import ccxt
import pandas as pd


# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_SYMBOL    = "BTC/USDT"
TIMEFRAME         = "4h"
DEFAULT_SINCE     = "2019-01-01"
LIMIT_PER_REQUEST = 1000


def _default_out(symbol: str) -> str:
    """ETH/USDT → eth_usdt_4h.csv"""
    return symbol.lower().replace("/", "_") + "_4h.csv"


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str,
                since_str: str, end_str: str | None) -> pd.DataFrame:
    """
    Paginate through the exchange OHLCV endpoint and return a clean DataFrame.
    """
    since_ms = exchange.parse8601(f"{since_str}T00:00:00Z")
    end_ms   = (exchange.parse8601(f"{end_str}T00:00:00Z")
                if end_str else exchange.milliseconds())

    rows = []
    print(f"  Fetching {symbol} {timeframe} from {since_str} …", flush=True)

    while since_ms < end_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe,
                                         since=since_ms, limit=LIMIT_PER_REQUEST)
        except ccxt.NetworkError as e:
            print(f"  Network error: {e}. Retrying in 5 s …")
            time.sleep(5)
            continue
        except ccxt.ExchangeError as e:
            print(f"  Exchange error: {e}. Aborting.")
            break

        if not batch:
            break

        rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= since_ms:
            break
        since_ms = last_ts + 1

        last_dt = pd.to_datetime(last_ts, unit="ms", utc=True).strftime("%Y-%m-%d")
        print(f"    … fetched up to {last_dt} ({len(rows):,} bars)", end="\r", flush=True)

        time.sleep(exchange.rateLimit / 1000)

    print()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if end_str:
        df = df.loc[: pd.Timestamp(end_str, tz="UTC")]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def build_exchange(name: str) -> ccxt.Exchange:
    configs = {
        "binance": ccxt.binance,
        "bybit":   ccxt.bybit,
        "kucoin":  ccxt.kucoin,
    }
    if name not in configs:
        print(f"Unknown exchange '{name}'. Choose from: {list(configs)}")
        sys.exit(1)
    return configs[name]({"enableRateLimit": True})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch 4H OHLCV candles for any coin to CSV"
    )
    parser.add_argument("--symbol",   default=DEFAULT_SYMBOL,
                        help=f"Trading pair (default: {DEFAULT_SYMBOL}). "
                             f"Examples: ETH/USDT  SOL/USDT  XRP/USDT  BNB/USDT")
    parser.add_argument("--exchange", default="binance",
                        choices=["binance", "bybit", "kucoin"],
                        help="Exchange to fetch from (default: binance)")
    parser.add_argument("--since",   default=DEFAULT_SINCE,
                        help=f"Start date YYYY-MM-DD (default: {DEFAULT_SINCE})")
    parser.add_argument("--end",     default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--out",     default=None,
                        help="Output CSV path (default: auto from symbol, "
                             "e.g. eth_usdt_4h.csv)")
    args = parser.parse_args()

    # Normalise symbol — accept both ETH/USDT and ETH-USDT
    symbol = args.symbol.upper().replace("-", "/")

    # Auto-name output file if not specified
    out = args.out or _default_out(symbol)

    print(f"Symbol   : {symbol}  {TIMEFRAME}")
    print(f"Exchange : {args.exchange}")
    print(f"Range    : {args.since}  →  {args.end or 'latest'}")
    print(f"Output   : {out}")
    print()

    exchange = build_exchange(args.exchange)

    # Validate symbol exists on exchange
    try:
        exchange.load_markets()
        if symbol not in exchange.markets:
            print(f"ERROR: '{symbol}' not found on {args.exchange}.")
            print(f"  Check the symbol name. Example: BTC/USDT  ETH/USDT  SOL/USDT")
            sys.exit(1)
    except Exception as e:
        print(f"WARNING: Could not validate symbol ({e}). Proceeding anyway …")

    df = fetch_ohlcv(exchange, symbol, TIMEFRAME, args.since, args.end)

    if df.empty:
        print("ERROR: No data returned. Check your internet connection "
              "or try --exchange bybit")
        sys.exit(1)

    df.index.name = "timestamp"
    df.to_csv(out)

    print(f"\nSaved {len(df):,} bars to '{out}'")
    print(f"  First bar : {df.index.min()}")
    print(f"  Last bar  : {df.index.max()}")
    print()
    print("Next step:")
    print(f"  python run_backtest.py --data {out}")


if __name__ == "__main__":
    main()