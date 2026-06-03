"""
bot_utils.py — Data fetching + signal computation for the Combined Bot.

Both HMA and Ichimoku run independently on ALL 12 coins.
Each strategy can open its own separate position on the same coin.

Functions:
    fetch_candles(symbol)              → OHLCV DataFrame from CoinDCX (4H)
    fetch_candles_1h(symbol)           → OHLCV DataFrame from CoinDCX (1H)
    compute_hma_signals(symbol, df)    → HMA signal dict
    compute_ichimoku_signals(symbol, df) → Ichimoku signal dict
"""

from __future__ import annotations

import time
import requests
import pandas_ta as ta
import pandas as pd
import numpy as np
from datetime import datetime, timezone

import utils   # YouTuber's HMA indicator library — do not modify

from config import (
    CANDLES_URL, TIMEFRAME, CANDLES_LIMIT, INTERVAL_MS, WARMUP_BARS,
    HMA_FAST, HMA_SLOW, RSI_PERIOD, RSI_THRESHOLD, RSI_THRESHOLD_OVERRIDE,
    HMA_GAP_FILTER,
    LINREG_LENGTH, HMA_HALF_MODE, HMA_SQRT_MODE,
    USE_SMA, USE_DAILY_LINREG,
    ICHI_TENKAN, ICHI_KIJUN, ICHI_SENKOU, ICHI_REQUIRE_CHIKOU,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, VERBOSE,
)

_MIN_BARS_HMA  = 200
_MIN_BARS_ICHI = ICHI_SENKOU + ICHI_KIJUN + 10


# =============================================================================
# CoinDCX pair helpers
# =============================================================================

def _candle_pair(symbol: str) -> str:
    base, quote = symbol.split("/")
    return f"I-{base}_{quote}"

def _order_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


# =============================================================================
# Generic OHLCV fetcher (shared logic)
# =============================================================================

def _fetch_ohlcv(symbol: str, interval: str, interval_ms: int,
                 warmup_bars: int) -> pd.DataFrame:
    pair     = _candle_pair(symbol)
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (warmup_bars * interval_ms)
    all_rows = []
    cur      = start_ms

    while cur < now_ms:
        end_ms = min(cur + CANDLES_LIMIT * interval_ms, now_ms)
        params = {
            "pair": pair, "interval": interval,
            "startTime": cur, "endTime": end_ms,
            "limit": CANDLES_LIMIT,
        }

        success = False
        for attempt in range(4):                          # up to 4 attempts per page
            backoff = [0, 5, 15, 30][attempt]            # 0s → 5s → 15s → 30s
            if backoff:
                time.sleep(backoff)

            try:
                resp = requests.get(CANDLES_URL, params=params, timeout=20)
            except requests.exceptions.RequestException as e:
                print(f"  [{symbol}] fetch exception (attempt {attempt+1}/4): {e}")
                continue

            if resp.status_code == 429:
                print(f"  [{symbol}] rate limited (429) — sleeping 30s")
                time.sleep(30)
                continue
            if resp.status_code != 200:
                print(f"  [{symbol}] HTTP {resp.status_code} (attempt {attempt+1}/4): {resp.text[:120]}")
                continue

            try:
                candles = resp.json()
            except Exception as e:
                print(f"  [{symbol}] JSON parse error: {e}")
                continue

            if isinstance(candles, list) and candles:
                all_rows.extend(candles)
            success = True
            break

        if not success:
            print(f"  [{symbol}] gave up after 4 attempts — skipping")
            return pd.DataFrame()

        cur = end_ms + interval_ms
        time.sleep(0.5)                                   # 0.3 → 0.5s between pages

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    ts_col = next(
        (c for c in ["time","timestamp","open_time","ts"] if c in df.columns),
        None
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
    bad = (df["Close"] < df["Open"] * 0.005) & (df["Open"] > 0)
    if bad.sum() > 0:
        df = df[~bad]

    return df[["Open","High","Low","Close","Volume"]]


# =============================================================================
# OHLCV fetching — public API
# =============================================================================

def fetch_candles(symbol: str) -> pd.DataFrame:
    """Fetch WARMUP_BARS of 4H candles from CoinDCX — used for entries."""
    return _fetch_ohlcv(symbol, TIMEFRAME, INTERVAL_MS, WARMUP_BARS)


def fetch_candles_1h(symbol: str) -> pd.DataFrame:
    """Fetch 500 bars of 1H candles from CoinDCX — used for 1H exit detection.
    500 bars = ~21 days — enough for HMA(64) warmup.
    """
    return _fetch_ohlcv(symbol, "1h", 3_600_000, 500)


def compute_hma_exit_1h(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Lightweight HMA exit signal for 1H candles.
    Does NOT use YouTuber's library — computes HMA directly.
    Only checks exit condition: HMA fast crosses below HMA slow.
    Used for 1H exit detection on BNB, ADA, AVAX, LINK, ZEC, JASMY, POL.
    """
    if len(df) < HMA_SLOW + 10:
        return None

    try:
        close = df["Close"].astype(float)

        def _hma(s, p):
            half  = max(int(p / 2), 1)
            sqrtp = max(int(np.sqrt(p)), 1)

            wma_half = ta.wma(s, length=half)
            wma_full = ta.wma(s, length=p)

            raw = 2 * wma_half - wma_full

            return ta.wma(raw, length=sqrtp)

        hma_fast = _hma(close, HMA_FAST)
        hma_slow = _hma(close, HMA_SLOW)

        latest_fast = float(hma_fast.iloc[-1])
        latest_slow = float(hma_slow.iloc[-1])
        prev_fast   = float(hma_fast.iloc[-2])
        prev_slow   = float(hma_slow.iloc[-2])

        # Exit: HMA fast crosses below HMA slow
        cross_down  = (latest_fast < latest_slow) and (prev_fast >= prev_slow)
        # Also exit if fast has been below slow for the last bar (sustained cross)
        below       = latest_fast < latest_slow

        hma_gap = (latest_fast - latest_slow) / latest_slow * 100 if latest_slow != 0 else 0.0

        return {
            "strategy":     "hma",
            "entry_signal": False,   # never enter from 1H function
            "exit_signal":  cross_down or below,
            "close":        float(df["Close"].iloc[-1]),
            "hma_fast":     latest_fast,
            "hma_slow":     latest_slow,
            "hma_gap_pct":  round(hma_gap, 2),
            "bar_time":     str(df.index[-1]),
        }

    except Exception as e:
        if VERBOSE:
            print(f"  [{symbol}][HMA-1H] error: {e}")
        return None


# =============================================================================
# HMA signal computation
# =============================================================================

def compute_hma_signals(symbol: str, df: pd.DataFrame,
                        min_bars: int = _MIN_BARS_HMA) -> dict | None:
    """
    HMA(16/64) + RSI(14) + LinReg(50) strategy.
    Uses utils.prepare_dataset() — the YouTuber's validated indicator code.
    RSI threshold is per-coin: BTC/ETH use 50, all others use 52.
    Gap filter: DOGE/ETH/XRP/BNB allow mid-trend entry when gap ≤ 2%.
    Works on both 4H candles (entry) and 1H candles (exit detection).
    min_bars: lower for 1H exit use (100 bars sufficient for exit signal).
    """
    if len(df) < min_bars:
        return None

    # Per-coin RSI threshold — BTC and ETH use 50 (backtest validated)
    rsi_thresh = RSI_THRESHOLD_OVERRIDE.get(symbol, RSI_THRESHOLD)

    try:
        prepared = utils.prepare_dataset(
            df,
            hma_fast=HMA_FAST, hma_slow=HMA_SLOW,
            rsi_period=RSI_PERIOD, rsi_threshold=rsi_thresh,
            linreg_length=LINREG_LENGTH,
            hma_half_mode=HMA_HALF_MODE, hma_sqrt_mode=HMA_SQRT_MODE,
            use_sma=USE_SMA, use_daily_linreg=USE_DAILY_LINREG,
        )
    except Exception as e:
        if VERBOSE:
            print(f"  [{symbol}][HMA] error: {e}")
        return None

    if prepared.empty or len(prepared) < 2:
        return None

    row     = prepared.iloc[-1]
    hma_gap = (
        (float(row["hma_fast"]) - float(row["hma_slow"])) /
        float(row["hma_slow"]) * 100
        if float(row["hma_slow"]) != 0 else 0.0
    )

    # Per-coin gap filter for mid-trend entry
    gap_filter = HMA_GAP_FILTER.get(symbol)
    if gap_filter is not None:
        hma_gap_frac    = hma_gap / 100
        mid_trend_entry = (
            float(row["hma_fast"]) > float(row["hma_slow"])
            and 0 < hma_gap_frac <= 0.03
            and 52 < float(row["rsi"]) < 60
        )
        entry_signal = bool(row["entry_long"] == 1) or mid_trend_entry
    else:
        entry_signal = bool(row["entry_long"] == 1)

    # print(f"  [{symbol}] bar_time={row.name} close={float(row['Close']):.6f} fast={float(row['hma_fast']):.6f} slow={float(row['hma_slow']):.6f}")
    return {
        "strategy":     "hma",
        "entry_signal": entry_signal,
        "exit_signal":  bool(row["exit_long"] == 1),
        "close":        float(row["Close"]),
        "hma_fast":     float(row["hma_fast"]),
        "hma_slow":     float(row["hma_slow"]),
        "hma_gap_pct":  round(hma_gap, 2),
        "rsi":          float(row["rsi"]),
        "linreg":       float(row["linreg_4h"]),
        "bars":         len(prepared),
        "bar_time":     str(row.name),
    }


# =============================================================================
# Ichimoku signal computation
# =============================================================================

def compute_ichimoku_signals(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Ichimoku entry conditions (per-coin chikou override):
      ETH/BNB : TK cross + chikou + above cloud (strict)
      Others  : TK cross + above cloud (no chikou — backtest validated)

    Exit:
      - Tenkan crosses below Kijun
      - Close < Kijun
    """
    if len(df) < _MIN_BARS_ICHI:
        return None

    try:
        d = df.copy()
        d["tenkan"] = (
            d["High"].rolling(ICHI_TENKAN).max() +
            d["Low"].rolling(ICHI_TENKAN).min()
        ) / 2
        d["kijun"] = (
            d["High"].rolling(ICHI_KIJUN).max() +
            d["Low"].rolling(ICHI_KIJUN).min()
        ) / 2
        span_a_raw = (d["tenkan"] + d["kijun"]) / 2
        span_b_raw = (
            d["High"].rolling(ICHI_SENKOU).max() +
            d["Low"].rolling(ICHI_SENKOU).min()
        ) / 2
        d["span_a"]       = span_a_raw.shift(ICHI_KIJUN)
        d["span_b"]       = span_b_raw.shift(ICHI_KIJUN)
        d["cloud_top"]    = d[["span_a","span_b"]].max(axis=1)
        d["cloud_bottom"] = d[["span_a","span_b"]].min(axis=1)
        d["chikou_ref"]   = d["Close"].shift(ICHI_KIJUN)
        d = d.dropna()

        if len(d) < 2:
            return None

        latest = d.iloc[-1]
        prev   = d.iloc[-2]

        tk_bull     = (float(latest["tenkan"]) > float(latest["kijun"]) and
                       float(prev["tenkan"])   <= float(prev["kijun"]))
        tk_bear     = (float(latest["tenkan"]) < float(latest["kijun"]) and
                       float(prev["tenkan"])   >= float(prev["kijun"]))
        chikou_bull = float(latest["Close"]) > float(latest["chikou_ref"])
        above_cloud = float(latest["Close"]) > float(latest["cloud_top"])
        below_kijun = float(latest["Close"]) < float(latest["kijun"])

        require_chikou = ICHI_REQUIRE_CHIKOU.get(symbol, False)
        if require_chikou:
            entry_signal = tk_bull and chikou_bull and above_cloud
        else:
            entry_signal = tk_bull and above_cloud

        cloud_gap = (
            (float(latest["Close"]) - float(latest["cloud_top"])) /
            float(latest["cloud_top"]) * 100
            if float(latest["cloud_top"]) != 0 else 0.0
        )

        return {
            "strategy":      "ichimoku",
            "entry_signal":  entry_signal,
            "exit_signal":   tk_bear or below_kijun,
            "close":         float(latest["Close"]),
            "tenkan":        float(latest["tenkan"]),
            "kijun":         float(latest["kijun"]),
            "cloud_top":     float(latest["cloud_top"]),
            "cloud_bottom":  float(latest["cloud_bottom"]),
            "cloud_gap_pct": round(cloud_gap, 2),
            "tk_bull":       tk_bull,
            "tk_bear":       tk_bear,
            "chikou_ok":     chikou_bull,
            "above_cloud":   above_cloud,
            "bars":          len(d),
            "bar_time":      str(latest.name),
        }

    except Exception as e:
        if VERBOSE:
            print(f"  [{symbol}][ICHI] error: {e}")
        return None


# =============================================================================
# Telegram notifications
# =============================================================================

def _telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def notify_entry(symbol: str, strategy: str, price: float,
                 qty: float, allocation: float, sig: dict) -> None:
    if strategy == "ichimoku":
        detail = (f"Cloud gap: {sig.get('cloud_gap_pct',0):+.2f}%\n"
                  f"Kijun   : {sig.get('kijun',0):.4f} (stop)")
    else:
        detail = (f"RSI     : {sig.get('rsi',0):.1f}\n"
                  f"HMA gap : {sig.get('hma_gap_pct',0):+.2f}%")

    _telegram(
        f"<b>Bot — ENTRY [{strategy.upper()}]</b>\n"
        f"Coin       : {symbol}\n"
        f"Price      : ${price:.4f}\n"
        f"Qty        : {qty:.6f}\n"
        f"Allocation : ${allocation:.2f}\n"
        f"{detail}"
    )


def notify_exit(symbol: str, strategy: str, entry_price: float,
                exit_price: float, qty: float,
                pnl: float, reason: str) -> None:
    pct  = (exit_price - entry_price) / entry_price * 100
    sign = "+" if pnl >= 0 else ""
    _telegram(
        f"<b>Bot — EXIT [{strategy.upper()}]</b>\n"
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
        f"<b>Bot — Run Summary</b>\n"
        f"Equity   : ${equity:.2f}\n"
        f"Cash     : ${cash:.2f}\n"
        f"Open     : {open_pos}\n"
        f"Realized : ${realized_pnl:+.2f}"
    )