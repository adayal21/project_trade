"""
bot_utils.py — Data fetching + signal computation for the Combined Bot.
Data source: CoinSwitch PRO API (coinswitchx, INR pairs).

Both HMA and Ichimoku run independently on all coins.
Each strategy can open its own separate position on the same coin.

Functions:
    fetch_candles(symbol)               → OHLCV DataFrame from CoinSwitch (4H)
    fetch_candles_1h(symbol)            → OHLCV DataFrame from CoinSwitch (1H)
    compute_hma_signals(symbol, df)     → HMA signal dict
    compute_hma_exit_1h(symbol, df)     → lightweight HMA exit on 1H
    compute_ichimoku_signals(symbol, df)→ Ichimoku signal dict
    notify_signal_alert(...)            → Telegram BUY/SELL notification
"""

from __future__ import annotations

import time
import requests
import pandas_ta as ta
import pandas as pd
import numpy as np
from datetime import datetime, timezone

import utils   # HMA indicator library — do not modify

from config import (
    CS_EXCHANGE,
    CANDLES_LIMIT,
    TIMEFRAME_4H_MIN, TIMEFRAME_1H_MIN,
    INTERVAL_MS_4H, INTERVAL_MS_1H,
    WARMUP_BARS_4H, WARMUP_BARS_1H,
    HMA_FAST, HMA_SLOW, RSI_PERIOD, RSI_THRESHOLD, RSI_THRESHOLD_OVERRIDE,
    HMA_GAP_FILTER,
    LINREG_LENGTH, HMA_HALF_MODE, HMA_SQRT_MODE,
    USE_SMA, USE_DAILY_LINREG,
    ICHI_TENKAN, ICHI_KIJUN, ICHI_SENKOU, ICHI_REQUIRE_CHIKOU,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, VERBOSE,
)
from coinswitch_auth import signed_get

_MIN_BARS_HMA  = 200
_MIN_BARS_ICHI = ICHI_SENKOU + ICHI_KIJUN + 10


# =============================================================================
# CoinSwitch OHLCV fetcher
# =============================================================================

def _fetch_ohlcv(symbol: str, interval_min: int, interval_ms: int,
                 warmup_bars: int) -> pd.DataFrame:
    """
    Fetch OHLCV candles from CoinSwitch PRO.

    Parameters
    ----------
    symbol       : e.g. "BTC/INR"
    interval_min : candle width in minutes (60 = 1H, 240 = 4H)
    interval_ms  : candle width in milliseconds (used to compute time windows)
    warmup_bars  : how many bars of history to fetch total

    CoinSwitch candle endpoint:
        GET /trade/api/v2/candles
        params: exchange, symbol, interval (minutes), start_time (ms), end_time (ms)

    Response: {"data": [{"o","h","l","c","volume","symbol","interval",
                          "start_time","close_time"}, ...]}
    """
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (warmup_bars * interval_ms)
    all_rows = []
    cur      = start_ms

    while cur < now_ms:
        end_ms = min(cur + CANDLES_LIMIT * interval_ms, now_ms)

        params = {
            "exchange":   CS_EXCHANGE,
            "symbol":     symbol,
            "interval":   str(interval_min),
            "start_time": str(cur),
            "end_time":   str(end_ms),
        }

        success = False
        for attempt in range(4):
            backoff = [0, 5, 15, 30][attempt]
            if backoff:
                time.sleep(backoff)

            try:
                resp = signed_get("/trade/api/v2/candles", params=params, timeout=20)
            except Exception as e:
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
                payload = resp.json()
            except Exception as e:
                print(f"  [{symbol}] JSON parse error: {e}")
                continue

            candles = payload.get("data", [])
            if isinstance(candles, list) and candles:
                all_rows.extend(candles)
            success = True
            break

        if not success:
            print(f"  [{symbol}] gave up after 4 attempts — skipping")
            return pd.DataFrame()

        cur = end_ms + interval_ms
        time.sleep(0.5)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)

    # CoinSwitch response fields: o, h, l, c, volume, start_time, close_time
    rename_map = {
        "o":          "Open",
        "h":          "High",
        "l":          "Low",
        "c":          "Close",
        "volume":     "Volume",
        "start_time": "timestamp",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "timestamp" not in df.columns:
        print(f"  [{symbol}] unexpected response shape — missing start_time")
        return pd.DataFrame()

    df["timestamp"] = pd.to_datetime(
        pd.to_numeric(df["timestamp"], errors="coerce"),
        unit="ms", utc=True,
    )
    df = df.dropna(subset=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[df["Close"] > 0]

    # Sanity check — drop candles where close is less than 0.5% of open
    bad = (df["Close"] < df["Open"] * 0.005) & (df["Open"] > 0)
    if bad.sum() > 0:
        df = df[~bad]

    return df[["Open", "High", "Low", "Close", "Volume"]]


# =============================================================================
# Public OHLCV fetchers
# =============================================================================

def fetch_candles(symbol: str) -> pd.DataFrame:
    """Fetch 4H candles from CoinSwitch — used for entry signals."""
    return _fetch_ohlcv(symbol, TIMEFRAME_4H_MIN, INTERVAL_MS_4H, WARMUP_BARS_4H)


def fetch_candles_1h(symbol: str) -> pd.DataFrame:
    """Fetch 1H candles from CoinSwitch — used for 1H exit detection."""
    return _fetch_ohlcv(symbol, TIMEFRAME_1H_MIN, INTERVAL_MS_1H, WARMUP_BARS_1H)


# =============================================================================
# Dynamic coin universe helpers
# =============================================================================

def fetch_all_inr_pairs() -> list[str]:
    """
    Fetch all active INR trading pairs from CoinSwitch PRO.
    Returns a list like ["BTC/INR", "ETH/INR", ...].
    Falls back to COINS from config on error.
    """
    from config import COINS
    try:
        resp = signed_get("/trade/api/v2/coins", params={"exchange": CS_EXCHANGE}, timeout=15)
        if resp.status_code != 200:
            print(f"  [fetch_all_inr_pairs] HTTP {resp.status_code} — using config COINS")
            return COINS
        data = resp.json().get("data", {})
        symbols = data.get(CS_EXCHANGE, [])
        inr_pairs = sorted([s for s in symbols if s.endswith("/INR")])
        if not inr_pairs:
            return COINS
        return inr_pairs
    except Exception as e:
        print(f"  [fetch_all_inr_pairs] error: {e} — using config COINS")
        return COINS


# =============================================================================
# Lightweight 1H HMA exit (no utils dependency)
# =============================================================================

def compute_hma_exit_1h(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Lightweight HMA exit signal for 1H candles.
    Does NOT use utils library — computes HMA directly with pandas_ta.
    Only checks exit condition: HMA fast crosses below HMA slow.
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

        cross_down = (latest_fast < latest_slow) and (prev_fast >= prev_slow)
        below      = latest_fast < latest_slow

        hma_gap = (latest_fast - latest_slow) / latest_slow * 100 if latest_slow != 0 else 0.0

        return {
            "strategy":     "hma",
            "entry_signal": False,
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
# HMA signal computation (4H entry + exit)
# =============================================================================

def compute_hma_signals(symbol: str, df: pd.DataFrame,
                        min_bars: int = _MIN_BARS_HMA) -> dict | None:
    """
    HMA(16/64) + RSI(14) + LinReg(50) strategy.
    Uses utils.prepare_dataset() — the validated indicator library.

    Slope filter: HMA_slow must be rising over the last 3 bars at entry.
    Prevents entering on downtrend bounces where fast briefly crosses above
    a still-falling slow line. Fires at the same crossover bar — no delay —
    just skips crossovers where the slow line is still pointing down.
    """
    if len(df) < min_bars:
        return None

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

    if prepared.empty or len(prepared) < 5:
        return None

    row      = prepared.iloc[-1]
    row_3ago = prepared.iloc[-4]   # 3 bars back for slope check

    hma_gap = (
        (float(row["hma_fast"]) - float(row["hma_slow"])) /
        float(row["hma_slow"]) * 100
        if float(row["hma_slow"]) != 0 else 0.0
    )

    # Slope filter — HMA slow must be rising vs 3 bars ago
    hma_slow_rising = float(row["hma_slow"]) > float(row_3ago["hma_slow"])

    gap_filter = HMA_GAP_FILTER.get(symbol)
    if gap_filter is not None:
        hma_gap_frac    = hma_gap / 100
        mid_trend_entry = (
            float(row["hma_fast"]) > float(row["hma_slow"])
            and 0 < hma_gap_frac <= 0.03
            and 52 < float(row["rsi"]) < 60
            and hma_slow_rising
        )
        entry_signal = (bool(row["entry_long"] == 1) and hma_slow_rising) or mid_trend_entry
    else:
        entry_signal = bool(row["entry_long"] == 1) and hma_slow_rising

    return {
        "strategy":        "hma",
        "entry_signal":    entry_signal,
        "exit_signal":     bool(row["exit_long"] == 1),
        "close":           float(row["Close"]),
        "hma_fast":        float(row["hma_fast"]),
        "hma_slow":        float(row["hma_slow"]),
        "hma_gap_pct":     round(hma_gap, 2),
        "hma_slow_rising": hma_slow_rising,
        "rsi":             float(row["rsi"]),
        "linreg":          float(row["linreg_4h"]),
        "bars":            len(prepared),
        "bar_time":        str(row.name),
    }


# =============================================================================
# Ichimoku signal computation (4H)
# =============================================================================

def compute_ichimoku_signals(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Ichimoku entry conditions (per-coin chikou override):
      Default : TK cross + above cloud (no chikou)
      Override: TK cross + chikou + above cloud

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
        d["cloud_top"]    = d[["span_a", "span_b"]].max(axis=1)
        d["cloud_bottom"] = d[["span_a", "span_b"]].min(axis=1)
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


def notify_signal_alert(symbol: str, strategy: str, action: str,
                        price: float, rsi: float = None,
                        gap_pct: float = None, cloud_gap_pct: float = None) -> None:
    emoji = "🟢" if action == "BUY" else "🔴"
    lines = [
        f"{emoji} <b>{action} — {symbol}</b>  [{strategy.upper()}]",
        f"Price : ₹{price:.4f}",
    ]
    if rsi is not None:
        lines.append(f"RSI   : {rsi:.1f}")
    if gap_pct is not None:
        lines.append(f"Gap%  : {gap_pct:+.2f}%")
    if cloud_gap_pct is not None:
        lines.append(f"Cloud : {cloud_gap_pct:+.2f}%")
    _telegram("\n".join(lines))


def notify_exit(symbol: str, strategy: str, entry_price: float,
                exit_price: float, qty: float,
                pnl: float, reason: str) -> None:
    pct  = (exit_price - entry_price) / entry_price * 100
    sign = "+" if pnl >= 0 else ""
    _telegram(
        f"<b>Bot — EXIT [{strategy.upper()}]</b>\n"
        f"Coin   : {symbol}\n"
        f"Reason : {reason}\n"
        f"Entry  : ₹{entry_price:.4f}\n"
        f"Exit   : ₹{exit_price:.4f}\n"
        f"Move   : {pct:+.2f}%\n"
        f"PnL    : {sign}₹{pnl:.2f}"
    )


def notify_run_summary(equity: float, cash: float,
                       open_pos: int, realized_pnl: float) -> None:
    _telegram(
        f"<b>Bot — Run Summary</b>\n"
        f"Equity   : ₹{equity:.2f}\n"
        f"Cash     : ₹{cash:.2f}\n"
        f"Open     : {open_pos}\n"
        f"Realized : ₹{realized_pnl:+.2f}"
    )