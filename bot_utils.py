"""
bot_utils.py — Data fetching + signal computation for the HMA 4H Trend Bot.

This file wraps the YouTuber's utils.py (indicator library).
Nothing here duplicates or replaces utils.py logic.

Responsibilities:
    1. Fetch 4H OHLCV candles from CoinDCX public API
    2. Remove corrupt candles (CoinDCX occasionally produces bad ticks)
    3. Compute indicators via utils.prepare_dataset() — unchanged
    4. Return entry_long / exit_long signal state for latest bar
    5. Send Telegram notifications
"""

from __future__ import annotations

import time
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import Optional

import utils   # YouTuber's indicator library — do not modify

from config import (
    CANDLES_URL, TIMEFRAME, CANDLES_LIMIT, INTERVAL_MS, WARMUP_BARS,
    HMA_FAST, HMA_SLOW, RSI_PERIOD, RSI_THRESHOLD,
    LINREG_LENGTH, HMA_HALF_MODE, HMA_SQRT_MODE,
    USE_SMA, USE_DAILY_LINREG,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    VERBOSE,
)

# Minimum bars required after warmup for stable signals
_MIN_BARS = 200


# =============================================================================
# CoinDCX pair format helpers
# =============================================================================

def _candle_pair(symbol: str) -> str:
    """'BTC/USDT' → 'KC-BTC_USDT'  (CoinDCX candles API format)"""
    base, quote = symbol.split("/")
    return f"KC-{base}_{quote}"


def _order_symbol(symbol: str) -> str:
    """'BTC/USDT' → 'BTCUSDT'  (CoinDCX orders API format)"""
    return symbol.replace("/", "")


# =============================================================================
# OHLCV fetching
# =============================================================================

def fetch_candles(symbol: str) -> pd.DataFrame:
    """
    Fetch the most recent WARMUP_BARS of 4H candles from CoinDCX.

    Uses a sliding startTime/endTime window to paginate from
    (now - WARMUP_BARS * 4h) up to now. Returns a clean DataFrame
    with UTC DatetimeIndex and columns [Open, High, Low, Close, Volume].

    Returns an empty DataFrame on failure — caller must handle that.
    """
    pair     = _candle_pair(symbol)
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (WARMUP_BARS * INTERVAL_MS)
    all_rows = []
    retries  = 0
    cur      = start_ms

    while cur < now_ms:
        end_ms = min(cur + CANDLES_LIMIT * INTERVAL_MS, now_ms)
        params = {
            "pair":      pair,
            "interval":  TIMEFRAME,
            "startTime": cur,
            "endTime":   end_ms,
            "limit":     CANDLES_LIMIT,
        }

        try:
            resp = requests.get(CANDLES_URL, params=params, timeout=20)
        except requests.exceptions.RequestException as e:
            retries += 1
            if retries > 3:
                print(f"  [{symbol}] fetch failed after 3 retries: {e}")
                return pd.DataFrame()
            time.sleep(3)
            continue

        if resp.status_code == 429:
            time.sleep(15)
            continue

        if resp.status_code != 200:
            retries += 1
            if retries > 3:
                print(f"  [{symbol}] HTTP {resp.status_code} — giving up")
                return pd.DataFrame()
            time.sleep(3)
            continue

        retries = 0

        try:
            candles = resp.json()
        except Exception as e:
            print(f"  [{symbol}] JSON parse error: {e}")
            return pd.DataFrame()

        if isinstance(candles, list) and candles:
            all_rows.extend(candles)

        cur = end_ms + INTERVAL_MS
        time.sleep(0.3)

    if not all_rows:
        print(f"  [{symbol}] no candles returned")
        return pd.DataFrame()

    return _parse_candles(symbol, all_rows)


def _parse_candles(symbol: str, rows: list) -> pd.DataFrame:
    """Convert raw CoinDCX candle dicts to a clean OHLCV DataFrame."""
    df = pd.DataFrame(rows)

    ts_col = next(
        (c for c in ["time", "timestamp", "open_time", "ts"] if c in df.columns),
        None
    )
    if ts_col is None:
        print(f"  [{symbol}] no timestamp column — got: {df.columns.tolist()}")
        return pd.DataFrame()

    df = df.rename(columns={
        ts_col:   "timestamp",
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "close":  "Close",
        "volume": "Volume",
    })

    df["timestamp"] = pd.to_datetime(
        pd.to_numeric(df["timestamp"], errors="coerce"),
        unit="ms", utc=True
    )
    df = df.dropna(subset=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[df["Close"] > 0]

    # Remove corrupt candles — bars where Close is < 0.5% of Open
    # These are CoinDCX data glitches, not real prices
    bad = (df["Close"] < df["Open"] * 0.005) & (df["Open"] > 0)
    if bad.sum() > 0:
        if VERBOSE:
            print(f"  [{symbol}] removed {bad.sum()} corrupt candle(s)")
        df = df[~bad]

    return df[["Open", "High", "Low", "Close", "Volume"]]


# =============================================================================
# Signal computation
# =============================================================================

def compute_signals(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Run utils.prepare_dataset() and return the latest bar's signal state.

    Returns a dict:
        entry_long  : bool   — True if all 3 entry conditions met on latest bar
        exit_long   : bool   — True if HMA cross-down on latest bar
        close       : float  — latest close price
        hma_fast    : float  — latest HMA(16) value
        hma_slow    : float  — latest HMA(64) value
        hma_gap_pct : float  — (fast-slow)/slow * 100, negative = cross imminent
        rsi         : float  — latest RSI(14)
        linreg      : float  — latest LinReg(50) value
        bars        : int    — number of prepared bars
        bar_time    : str    — UTC timestamp of latest bar

    Returns None if insufficient data or computation fails.
    """
    if len(df) < _MIN_BARS:
        if VERBOSE:
            print(f"  [{symbol}] only {len(df)} bars, need {_MIN_BARS} — skip")
        return None

    try:
        prepared = utils.prepare_dataset(
            df,
            hma_fast         = HMA_FAST,
            hma_slow         = HMA_SLOW,
            rsi_period       = RSI_PERIOD,
            rsi_threshold    = RSI_THRESHOLD,
            linreg_length    = LINREG_LENGTH,
            hma_half_mode    = HMA_HALF_MODE,
            hma_sqrt_mode    = HMA_SQRT_MODE,
            use_sma          = USE_SMA,
            use_daily_linreg = USE_DAILY_LINREG,
        )
    except Exception as e:
        print(f"  [{symbol}] indicator error: {e}")
        return None

    if prepared.empty:
        return None

    row = prepared.iloc[-1]
    hma_gap = (
        (float(row["hma_fast"]) - float(row["hma_slow"])) /
        float(row["hma_slow"]) * 100
        if float(row["hma_slow"]) != 0 else 0.0
    )

    return {
        "entry_long":  bool(row["entry_long"] == 1),
        "exit_long":   bool(row["exit_long"]  == 1),
        "close":       float(row["Close"]),
        "hma_fast":    float(row["hma_fast"]),
        "hma_slow":    float(row["hma_slow"]),
        "hma_gap_pct": hma_gap,
        "rsi":         float(row["rsi"]),
        "linreg":      float(row["linreg_4h"]),
        "bars":        len(prepared),
        "bar_time":    str(row.name),
        # Individual conditions — useful for logging
        "c_cross":     bool(row["cross_up"]),
        "c_rsi":       float(row["rsi"]) > RSI_THRESHOLD,
        "c_linreg":    float(row["Close"]) > float(row["linreg_4h"]),
    }


# =============================================================================
# Telegram notifications
# =============================================================================

def _telegram(message: str) -> None:
    """Send Telegram message. Silently skips if not configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def notify_entry(symbol: str, price: float, qty: float,
                 allocation: float, sig: dict) -> None:
    _telegram(
        f"<b>HMA Bot — ENTRY</b>\n"
        f"Coin       : {symbol}\n"
        f"Price      : ${price:.4f}\n"
        f"Qty        : {qty:.6f}\n"
        f"Allocation : ${allocation:.2f}\n"
        f"RSI        : {sig['rsi']:.1f}\n"
        f"HMA fast   : {sig['hma_fast']:.4f}\n"
        f"HMA slow   : {sig['hma_slow']:.4f}\n"
        f"LinReg     : {sig['linreg']:.4f}"
    )


def notify_exit(symbol: str, entry_price: float, exit_price: float,
                qty: float, pnl: float, reason: str) -> None:
    pct  = (exit_price - entry_price) / entry_price * 100
    sign = "+" if pnl >= 0 else ""
    _telegram(
        f"<b>HMA Bot — EXIT</b>\n"
        f"Coin   : {symbol}\n"
        f"Reason : {reason}\n"
        f"Entry  : ${entry_price:.4f}\n"
        f"Exit   : ${exit_price:.4f}\n"
        f"Move   : {pct:+.2f}%\n"
        f"PnL    : {sign}${pnl:.2f}"
    )


def notify_run_summary(equity: float, cash: float,
                        open_pos: int, realized_pnl: float) -> None:
    _telegram(
        f"<b>HMA Bot — Run</b>\n"
        f"Equity   : ${equity:.2f}\n"
        f"Cash     : ${cash:.2f}\n"
        f"Open     : {open_pos}\n"
        f"Realized : ${realized_pnl:+.2f}"
    )