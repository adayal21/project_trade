"""
utils.py — BTCUSDT HMA/SMA + RSI(14) + LinReg strategy helpers
Targets: 4H execution, long-only, backtesting.py.

Data convention: OHLCV with columns Open, High, Low, Close, Volume and a
DatetimeIndex (UTC). Same indexing/resampling style as the Phase 1-3 utils.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, floor, sqrt
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta
from pandas_ta.maps import Imports

import plotly.graph_objects as go
from plotly.subplots import make_subplots

# If TA-Lib was installed after the kernel started, make sure pandas_ta
# notices it as soon as this module is reloaded.
try:
    import talib  # noqa: F401
    Imports["talib"] = True
except Exception:
    pass


# ============================================================================
# Data loading + resampling
# ============================================================================

@dataclass(frozen=True)
class DataLoadConfig:
    """Mirrors the convention from the Phase 1-3 utils."""
    tz: str = "UTC"
    localize_tz: bool = False              # crypto data is already UTC
    drop_weekends: bool = False            # crypto trades 24/7
    resample_kw: dict = field(default_factory=lambda: dict(label="right", closed="right"))


def fetch_binance_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "4h",
    since: str = "2018-01-01",
    end: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV from Binance via ccxt with pagination.

    Returns a DataFrame with UTC DatetimeIndex and columns
    [Open, High, Low, Close, Volume].
    """
    import time
    import ccxt

    exchange = ccxt.binance({"enableRateLimit": True})
    since_ms = exchange.parse8601(f"{since}T00:00:00Z")
    end_ms = exchange.parse8601(f"{end}T00:00:00Z") if end else exchange.milliseconds()

    rows: list = []
    while since_ms < end_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
        if not batch:
            break
        rows += batch
        last_ts = batch[-1][0]
        if last_ts <= since_ms:
            break
        since_ms = last_ts + 1
        time.sleep(exchange.rateLimit / 1000)

    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if end:
        df = df.loc[: pd.Timestamp(end, tz="UTC")]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def load_csv_ohlcv(
    path: str,
    cfg: DataLoadConfig = DataLoadConfig(),
    time_col: str = "timestamp",
    time_format: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load OHLCV from a CSV (e.g., MetaTrader or Binance Vision export).
    Expected columns after cleaning: Open, High, Low, Close, Volume.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().replace("\ufeff", "") for c in df.columns]

    if time_format:
        dt = pd.to_datetime(df[time_col], format=time_format, errors="coerce")
    else:
        dt = pd.to_datetime(df[time_col], errors="coerce", utc=True)
    df = df.loc[dt.notna()].copy()
    df.index = pd.DatetimeIndex(dt[dt.notna()]).sort_values()

    if cfg.localize_tz and df.index.tz is None:
        df.index = df.index.tz_localize(cfg.tz, ambiguous="infer", nonexistent="shift_forward")

    keep = ["Open", "High", "Low", "Close", "Volume"]
    df = df[keep].copy()
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[~df.index.duplicated(keep="last")]
    if cfg.drop_weekends:
        df = df[df.index.dayofweek < 5]
    return df


def resample_ohlc(
    df: pd.DataFrame,
    rule: str,
    resample_kw: Optional[dict] = None,
) -> pd.DataFrame:
    """Resample OHLCV to a higher timeframe (e.g., '4H', '1D')."""
    if resample_kw is None:
        resample_kw = dict(label="right", closed="right")
    agg = {"Open": "first", "High": "max", "Low": "min",
           "Close": "last", "Volume": "sum"}
    out = df.resample(rule, **resample_kw).agg(agg)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    return out


# ============================================================================
# Indicators (pandas_ta + MTF merge)
# ============================================================================

def _rounded_length(value: float, mode: str = "floor") -> int:
    """Convert a positive window length using a chosen rounding convention."""
    mode = str(mode).lower()
    if mode == "floor":
        out = floor(value)
    elif mode == "ceil":
        out = ceil(value)
    elif mode == "round":
        # Round-half-up, not Python's banker's rounding.
        out = floor(value + 0.5)
    else:
        raise ValueError(f"Unsupported rounding mode '{mode}'. Use floor, round, or ceil.")
    return max(int(out), 1)


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    """Convert a timeframe like '4h' or '15m' into a pandas Timedelta."""
    tf = str(timeframe).strip().lower()
    if not tf:
        raise ValueError("timeframe cannot be empty")

    unit = tf[-1]
    value = int(tf[:-1])
    if value <= 0:
        raise ValueError(f"timeframe must be positive, got '{timeframe}'")

    unit_map = {
        "m": "min",
        "h": "h",
        "d": "d",
        "w": "w",
    }
    if unit not in unit_map:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Use m, h, d, or w suffixes.")
    return pd.to_timedelta(value, unit=unit_map[unit])


def infer_bar_timedelta(index: pd.DatetimeIndex) -> pd.Timedelta:
    """Infer the dominant bar spacing from a DatetimeIndex."""
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError("Expected a DatetimeIndex")
    diffs = index.to_series().diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        raise ValueError("Cannot infer bar spacing from fewer than two timestamps")
    return diffs.mode().iloc[0]


def buffered_fetch_start(
    start: str | pd.Timestamp,
    *,
    timeframe: str = "4h",
    hma_slow: int = 65,
    rsi_period: int = 14,
    linreg_length: int = 50,
    hma_sqrt_mode: str = "floor",
    use_sma: bool = False,
    extra_bars: int = 12,
    tz: str = "UTC",
) -> pd.Timestamp:
    """
    Compute a safe fetch start so indicators are already valid at `start`.

    This is especially important for the daily LinReg filter, which needs a
    longer history than the 4H HMA / RSI inputs.
    """
    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize(tz)
    else:
        start_ts = start_ts.tz_convert(tz)

    tf_delta = timeframe_to_timedelta(timeframe)
    bars_per_day = max(int(round(pd.Timedelta(days=1) / tf_delta)), 1)

    if use_sma:
        hma_warmup_bars = int(hma_slow) + 1
    else:
        hma_warmup_bars = int(hma_slow) + _rounded_length(sqrt(int(hma_slow)), hma_sqrt_mode) + 1
    rsi_warmup_bars = int(rsi_period) + 1
    linreg_warmup_bars = (int(linreg_length) + 1) * bars_per_day

    total_bars = max(hma_warmup_bars, rsi_warmup_bars, linreg_warmup_bars) + int(extra_bars)
    return start_ts - total_bars * tf_delta


def hull_moving_average(
    close: pd.Series,
    length: int,
    *,
    half_mode: str = "floor",
    sqrt_mode: str = "floor",
) -> pd.Series:
    """
    HMA built from WMA components with configurable odd-length rounding.

    This lets us match platforms that differ on how they convert n/2 and
    sqrt(n) into integer window lengths for odd or non-square periods.
    """
    length = int(length)
    half_length = _rounded_length(length / 2, half_mode)
    sqrt_length = _rounded_length(sqrt(length), sqrt_mode)

    wma_half = ta.wma(close, length=half_length, talib=True)
    wma_full = ta.wma(close, length=length, talib=True)
    raw = 2 * wma_half - wma_full
    hma = ta.wma(raw, length=sqrt_length, talib=True)
    hma.name = f"HMA_{length}"
    return hma


def simple_moving_average(
    close: pd.Series,
    length: int,
) -> pd.Series:
    """Standard SMA on close, named for consistency with the selected length."""
    sma = ta.sma(close, length=int(length), talib=True)
    sma.name = f"SMA_{int(length)}"
    return sma


def trend_moving_average(
    close: pd.Series,
    length: int,
    *,
    use_sma: bool = False,
    half_mode: str = "floor",
    sqrt_mode: str = "floor",
) -> pd.Series:
    """Return either SMA(length) or HMA(length) depending on the configured mode."""
    if use_sma:
        return simple_moving_average(close, length)
    return hull_moving_average(close, length, half_mode=half_mode, sqrt_mode=sqrt_mode)


def add_hma_rsi(
    df: pd.DataFrame,
    hma_fast: int = 16,
    hma_slow: int = 65,
    rsi_period: int = 14,
    hma_half_mode: str = "floor",
    hma_sqrt_mode: str = "floor",
    use_sma: bool = False,
) -> pd.DataFrame:
    """
    Add 4H indicators in-place style (returns a copy):
      hma_fast, hma_slow, rsi

    For downstream compatibility the moving-average columns keep the historical
    names `hma_fast` / `hma_slow` even when `use_sma=True`.
    """
    out = df.copy()
    out["hma_fast"] = trend_moving_average(
        out["Close"], hma_fast,
        use_sma=use_sma,
        half_mode=hma_half_mode,
        sqrt_mode=hma_sqrt_mode,
    )
    out["hma_slow"] = trend_moving_average(
        out["Close"], hma_slow,
        use_sma=use_sma,
        half_mode=hma_half_mode,
        sqrt_mode=hma_sqrt_mode,
    )
    out["rsi"] = ta.rsi(out["Close"], length=rsi_period)
    out.attrs["ma_kind"] = "SMA" if use_sma else "HMA"
    return out


def add_daily_linreg_filter(
    df_4h: pd.DataFrame,
    linreg_length: int = 50,
    resample_kw: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Resample 4H → 1D, compute LinearRegression(length) on the daily Close,
    then attach back to the 4H index using ffill().shift(1).

    The .shift(1) on the 4H-frequency column shifts by ONE 4H bar, which
    eliminates any look-ahead while keeping the lag minimal — this matches
    the established codebase convention.

    Adds two columns:
      prev_d1_close   — previous-bar daily close, aligned to 4H index
      prev_d1_linreg  — previous-bar daily linreg(length), aligned to 4H index
    """
    if resample_kw is None:
        resample_kw = dict(label="right", closed="right")

    # Our 4H index is bar-open time, but the strategy is evaluated on bar close.
    # Shift to close timestamps before the daily resample so day boundaries line up
    # with the completed 4H bars users see on the chart.
    bar_delta = infer_bar_timedelta(df_4h.index)
    close_index = df_4h.index + bar_delta
    df_close_time = df_4h.copy()
    df_close_time.index = close_index

    # Daily series
    df_1d = resample_ohlc(df_close_time, "1D", resample_kw=resample_kw)
    daily = pd.DataFrame(index=df_1d.index)
    daily["d1_close"] = df_1d["Close"]
    daily["d1_linreg"] = ta.linreg(df_1d["Close"], length=linreg_length)

    # Align the completed daily values back to the original 4H rows.
    out = df_4h.copy()
    aligned = daily.reindex(close_index, method="ffill")
    out["d1_close"] = aligned["d1_close"].to_numpy()
    out["d1_linreg"] = aligned["d1_linreg"].to_numpy()
    out["prev_d1_close"]  = out["d1_close"].shift(1)
    out["prev_d1_linreg"] = out["d1_linreg"].shift(1)
    return out


def add_signal_columns(
    df: pd.DataFrame,
    rsi_threshold: float = 52.0,
    use_daily_linreg: bool = False,
) -> pd.DataFrame:
    """
    Compute auxiliary columns useful for inspection / plotting:
      cross_up    — bool, hma_fast crossed above hma_slow on this bar
      cross_dn    — bool, hma_fast crossed below hma_slow on this bar
      rsi_ok      — bool, rsi > rsi_threshold
      htf_ok      — bool, Close > selected LinReg gate
      entry_long  — int, 1 when all entry conditions align (visualization only)
      exit_long   — int, 1 when exit condition fires (visualization only)

    These columns are NOT consumed by the Strategy class — the Strategy
    re-derives the HMA cross state directly from the indicator series so the
    execution logic stays aligned with these inspection columns. They exist
    purely so plot_strategy_slice() can mark candles.
    """
    out = df.copy()

    fast_prev = out["hma_fast"].shift(1)
    slow_prev = out["hma_slow"].shift(1)
    entry_linreg_col = "d1_linreg" if use_daily_linreg else "linreg_4h"

    out["cross_up"] = (fast_prev <= slow_prev) & (out["hma_fast"] > out["hma_slow"])
    out["cross_dn"] = (fast_prev >= slow_prev) & (out["hma_fast"] < out["hma_slow"])
    out["rsi_ok"]   = out["rsi"] > rsi_threshold
    out["entry_linreg"] = out[entry_linreg_col]
    out["htf_ok"]   = out["Close"] > out["entry_linreg"]

    out["entry_long"] = (out["cross_up"] & out["rsi_ok"] & out["htf_ok"]).astype(int)
    out["exit_long"]  = out["cross_dn"].astype(int)
    out.attrs["use_daily_linreg"] = bool(use_daily_linreg)
    out.attrs["entry_linreg_col"] = entry_linreg_col
    out.attrs["entry_linreg_display"] = "Daily LinReg" if use_daily_linreg else "4H LinReg"
    return out


def prepare_dataset(
    df_4h: pd.DataFrame,
    hma_fast: int = 16,
    hma_slow: int = 65,
    rsi_period: int = 14,
    rsi_threshold: float = 52.0,
    linreg_length: int = 50,
    hma_half_mode: str = "floor",
    hma_sqrt_mode: str = "floor",
    use_sma: bool = False,
    use_daily_linreg: bool = False,
    drop_warmup: bool = True,
) -> pd.DataFrame:
    """
    One-call pipeline: 4H moving averages + LinReg filter + signal columns.
    Drops warm-up rows that contain NaNs from any indicator.
    """
    out = add_hma_rsi(
        df_4h,
        hma_fast=hma_fast,
        hma_slow=hma_slow,
        rsi_period=rsi_period,
        hma_half_mode=hma_half_mode,
        hma_sqrt_mode=hma_sqrt_mode,
        use_sma=use_sma,
    )
    out["linreg_4h"] = ta.linreg(out["Close"], length=linreg_length)
    out = add_daily_linreg_filter(out, linreg_length=linreg_length)
    out = add_signal_columns(
        out,
        rsi_threshold=rsi_threshold,
        use_daily_linreg=use_daily_linreg,
    )
    if drop_warmup:
        need = ["hma_fast", "hma_slow", "rsi", "linreg_4h", "entry_linreg"]
        out = out.dropna(subset=need)
    out.attrs["ma_kind"] = "SMA" if use_sma else "HMA"
    out.attrs["use_daily_linreg"] = bool(use_daily_linreg)
    out.attrs["entry_linreg_col"] = "d1_linreg" if use_daily_linreg else "linreg_4h"
    out.attrs["entry_linreg_display"] = "Daily LinReg" if use_daily_linreg else "4H LinReg"
    return out


# ============================================================================
# Plotting (signal slice) — Plotly, same style as the Phase 1-3 utils
# ============================================================================

def plot_strategy_slice(
    df: pd.DataFrame,
    start_i: int = 0,
    end_i: Optional[int] = None,
    rsi_threshold: float = 52.0,
    title: str = "",
    marker_offset_frac: float = 0.15,
) -> go.Figure:
    """
    Three-row Plotly figure for visualizing the strategy on a slice:
      Row 1: 4H candles + fast/slow MA + LinReg overlays + entry/exit markers
      Row 2: RSI + horizontal threshold line
      Row 3: 4H Close vs the selected entry LinReg gate

    Expects columns produced by prepare_dataset().
    """
    if end_i is None:
        end_i = len(df)
    d = df.iloc[start_i:end_i].copy()
    if d.empty:
        raise ValueError("Empty slice. Check start_i/end_i.")
    ma_kind = str(df.attrs.get("ma_kind", "HMA")).upper()
    ma_fast_name = f"{ma_kind.lower()}_fast"
    ma_slow_name = f"{ma_kind.lower()}_slow"
    use_daily_linreg = bool(df.attrs.get("use_daily_linreg", False))
    entry_linreg_display = str(df.attrs.get("entry_linreg_display", "4H LinReg"))
    entry_linreg_col = str(df.attrs.get("entry_linreg_col", "linreg_4h"))

    needed = ["Open", "High", "Low", "Close",
              "hma_fast", "hma_slow", "rsi", "linreg_4h", "entry_linreg",
              "entry_long", "exit_long"]
    for c in needed:
        if c not in d.columns:
            raise ValueError(f"Missing column '{c}'. Did you run prepare_dataset()?")

    # Marker offsets relative to local candle range
    rng = (d["High"] - d["Low"]).replace(0, np.nan)
    offset = (rng.fillna(0) * float(marker_offset_frac))
    is_entry = d["entry_long"].astype(bool)
    is_exit  = d["exit_long"].astype(bool)
    entry_y = (d["Low"]  - offset).where(is_entry)
    exit_y  = (d["High"] + offset).where(is_exit)

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.6, 0.2, 0.2],
        vertical_spacing=0.04,
        subplot_titles=(f"Price + {ma_kind}(fast/slow) + LinReg overlays", "RSI", f"4H Close vs {entry_linreg_display} (entry gate)"),
    )

    # --- Row 1: candles + HMAs + 4H LinReg overlay ---
    fig.add_trace(
        go.Candlestick(
            x=d.index, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
            name="Price", showlegend=False,
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(x=d.index, y=d["hma_fast"], mode="lines", name=ma_fast_name,
                   line=dict(width=1.5)),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(x=d.index, y=d["hma_slow"], mode="lines", name=ma_slow_name,
                   line=dict(width=1.5)),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=d.index, y=d["linreg_4h"], mode="lines",
            name="linreg_4h",
            line=dict(width=1.2, color="#0ea5e9"),
        ),
        row=1, col=1,
    )
    if use_daily_linreg:
        fig.add_trace(
            go.Scatter(
                x=d.index, y=d["d1_linreg"], mode="lines",
                name="d1_linreg",
                line=dict(width=1.2, color="#f59e0b", dash="dot"),
            ),
            row=1, col=1,
        )
    if is_entry.any():
        fig.add_trace(
            go.Scatter(
                x=d.index[is_entry], y=entry_y[is_entry],
                mode="markers", name="Entry (long)",
                marker=dict(symbol="triangle-up", size=12, color="#16a34a",
                            line=dict(color="white", width=1)),
            ),
            row=1, col=1,
        )
    if is_exit.any():
        fig.add_trace(
            go.Scatter(
                x=d.index[is_exit], y=exit_y[is_exit],
                mode="markers", name=f"Exit ({ma_kind} cross dn)",
                marker=dict(symbol="triangle-down", size=12, color="#dc2626",
                            line=dict(color="white", width=1)),
            ),
            row=1, col=1,
        )

    # --- Row 2: RSI ---
    fig.add_trace(
        go.Scatter(x=d.index, y=d["rsi"], mode="lines", name="rsi",
                   line=dict(width=1.5)),
        row=2, col=1,
    )
    fig.add_hline(y=rsi_threshold, line=dict(dash="dash", width=1),
                  annotation_text=f"th={rsi_threshold}",
                  annotation_position="top left",
                  row=2, col=1)

    # --- Row 3: 4H close vs 4H linreg (the entry gate) ---
    fig.add_trace(
        go.Scatter(x=d.index, y=d["Close"], mode="lines",
                   name="Close", line=dict(width=1.2)),
        row=3, col=1,
    )
    fig.add_trace(
        go.Scatter(x=d.index, y=d["entry_linreg"], mode="lines",
                   name=entry_linreg_col, line=dict(width=1.2, dash="dot")),
        row=3, col=1,
    )

    fig.update_layout(
        title=title or f"{ma_kind} + RSI + 4H LinReg — slice [{start_i}:{end_i}]",
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=20, t=70, b=40),
        height=820,
        hovermode="x unified",
    )
    fig.update_xaxes(type="date", showgrid=True)
    fig.update_yaxes(showgrid=True)
    return fig


def plot_equity_curve(
    stats,
    df_ohlc: Optional[pd.DataFrame] = None,
    title: str = "Equity vs Buy & Hold",
) -> go.Figure:
    """
    Plot the strategy equity curve from a backtesting.py stats object,
    optionally overlaid with a normalized buy & hold curve.
    """
    eq = stats["_equity_curve"]["Equity"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq.index, y=eq.values, mode="lines",
        name="Strategy equity", line=dict(width=2, color="#16a34a"),
    ))
    if df_ohlc is not None:
        # backtesting.py strips tz from the index — match it before reindexing
        bh_src = df_ohlc["Close"].copy()
        if isinstance(bh_src.index, pd.DatetimeIndex) and bh_src.index.tz is not None \
                and (not isinstance(eq.index, pd.DatetimeIndex) or eq.index.tz is None):
            bh_src.index = bh_src.index.tz_localize(None)
        bh = bh_src.reindex(eq.index, method="ffill")
        bh = bh / bh.iloc[0] * float(eq.iloc[0])
        fig.add_trace(go.Scatter(
            x=bh.index, y=bh.values, mode="lines",
            name="Buy & Hold", line=dict(width=1.5, color="#eab308", dash="dot"),
        ))
    fig.update_layout(
        title=title,
        xaxis_title="Time", yaxis_title="Equity",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=20, t=60, b=40),
        height=420,
        hovermode="x unified",
    )
    return fig


# ============================================================================
# backtesting.py strategy
# ============================================================================

def make_strategy_class(
    rsi_threshold: float = 52.0,
    position_size: float = 0.999,
    use_sma: bool = False,
    use_daily_linreg: bool = False,
):
    """
    Factory for the MA(fast/slow) + RSI + 4H-LinReg long-only strategy.

    Entry  (long, market): hma_fast crosses above hma_slow
                           AND rsi > rsi_threshold
                           AND 4H Close > selected LinReg gate
    Exit   (long, market): hma_fast crosses below hma_slow

    We intentionally treat equality on the previous bar as part of the cross:
      entry if prev_fast <= prev_slow and cur_fast > cur_slow
      exit  if prev_fast >= prev_slow and cur_fast < cur_slow
    This keeps the executed strategy aligned with the inspection signals in
    add_signal_columns().

    The DataFrame must already contain columns:
      hma_fast, hma_slow, rsi, linreg_4h, d1_linreg, Close_raw
    (use prepare_dataset()).
    """
    from backtesting import Strategy

    def _crossed_up_inclusive(fast, slow) -> bool:
        try:
            return float(fast[-2]) <= float(slow[-2]) and float(fast[-1]) > float(slow[-1])
        except (IndexError, TypeError, ValueError):
            return False

    def _crossed_down_inclusive(fast, slow) -> bool:
        try:
            return float(fast[-2]) >= float(slow[-2]) and float(fast[-1]) < float(slow[-1])
        except (IndexError, TypeError, ValueError):
            return False

    ma_label = "sma" if use_sma else "hma"

    class HmaRsiLinregStrategy(Strategy):
        # exposed for backtesting.py optimize() if needed
        rsi_th: float = float(rsi_threshold)
        size: float   = float(position_size)

        _entry_bar: int | None = None

        def init(self):
            self._entry_bar = None
            # Register pre-computed columns so they appear on bt.plot().
            # We wrap each one in np.array(..., copy=True) so FractionalBacktest's
            # unscaling pass (`indicator /= fractional_unit`) can write in-place;
            # the raw self.data.<col> view is read-only.
            _f = lambda c: (lambda: np.array(getattr(self.data, c), dtype=float, copy=True))
            self.hma_fast = self.I(_f("hma_fast"),       name=f"{ma_label}_fast", overlay=True)
            self.hma_slow = self.I(_f("hma_slow"),       name=f"{ma_label}_slow", overlay=True)
            self.rsi      = self.I(_f("rsi"),            name="rsi",      overlay=False)
            self.close_raw = self.I(_f("Close_raw"),     name="close_raw", overlay=False)
            self.lr_4h     = self.I(_f("linreg_4h"),     name="linreg_4h", overlay=True)
            if use_daily_linreg:
                self.lr_entry = self.I(_f("d1_linreg"), name="d1_linreg", overlay=True)
            else:
                self.lr_entry = self.lr_4h

        def next(self):
            cur_bar = len(self.data) - 1

            # --- Exit: HMA cross down ---
            if self.position and _crossed_down_inclusive(self.hma_fast, self.hma_slow):
                self.position.close()
                self._entry_bar = None
                return

            # --- Entry: only if flat ---
            if self.position:
                return

            cross_up = _crossed_up_inclusive(self.hma_fast, self.hma_slow)
            rsi_ok   = float(self.rsi[-1]) > self.rsi_th
            htf_ok   = float(self.close_raw[-1]) > float(self.lr_entry[-1])

            if cross_up and rsi_ok and htf_ok:
                self.buy(size=self.size)
                self._entry_bar = cur_bar

    return HmaRsiLinregStrategy


def run_backtest_backtestingpy(
    df: pd.DataFrame,
    strategy_cls,
    cash: float = 10_000.0,
    commission: float = 0.0006,        # ~6 bps per side, Binance-ish
    trade_on_close: bool = True,
    exclusive_orders: bool = True,
    margin: float = 1.0,
    fractional: bool = True,
    fractional_unit: float = 1 / 1e6,  # 1 μBTC granularity
):
    """
    Run backtesting.py Backtest and return (bt, stats, trades).

    For crypto (BTCUSDT etc.), use fractional=True so we can buy fractions of
    a unit. This wraps backtesting.lib.FractionalBacktest, which internally
    rescales prices so that 1 'unit' == fractional_unit and unscales the
    results (Size / EntryPrice / ExitPrice / SL / TP) on the way out.

    backtesting.py also requires a tz-naive DatetimeIndex and the OHLCV
    columns only — we strip both to be safe.
    """
    bt_df = df.copy()
    if isinstance(bt_df.index, pd.DatetimeIndex) and bt_df.index.tz is not None:
        bt_df.index = bt_df.index.tz_localize(None)
    bt_df["Close_raw"] = bt_df["Close"]
    ohlcv = ["Open", "High", "Low", "Close", "Volume"]
    extras = [c for c in bt_df.columns if c not in ohlcv]
    bt_df = bt_df[ohlcv + extras]  # keep extras (used by Strategy.init)

    if fractional:
        from backtesting.lib import FractionalBacktest as _BT
        bt = _BT(
            bt_df, strategy_cls,
            cash=cash, commission=commission,
            trade_on_close=trade_on_close,
            exclusive_orders=exclusive_orders,
            margin=margin,
            fractional_unit=fractional_unit,
        )
    else:
        from backtesting import Backtest as _BT
        bt = _BT(
            bt_df, strategy_cls,
            cash=cash, commission=commission,
            trade_on_close=trade_on_close,
            exclusive_orders=exclusive_orders,
            margin=margin,
        )

    stats = bt.run()
    trades = stats["_trades"].copy()
    return bt, stats, trades


# ============================================================================
# Quick stats helper (mirrors compare_long_short_winrates style)
# ============================================================================

def headline_stats(stats) -> pd.Series:
    """
    Pull the most relevant fields out of a backtesting.py stats object
    into a tidy single-column Series for display.
    """
    keys = [
        "Start", "End", "Duration", "Exposure Time [%]",
        "Equity Final [$]", "Equity Peak [$]",
        "Return [%]", "Buy & Hold Return [%]",
        "Return (Ann.) [%]", "Volatility (Ann.) [%]",
        "Sharpe Ratio", "Sortino Ratio", "Calmar Ratio",
        "Max. Drawdown [%]", "Avg. Drawdown [%]",
        "Max. Drawdown Duration", "Avg. Drawdown Duration",
        "# Trades", "Win Rate [%]", "Best Trade [%]", "Worst Trade [%]",
        "Avg. Trade [%]", "Profit Factor", "Expectancy [%]",
    ]
    rows = {k: stats[k] for k in keys if k in stats.index}
    return pd.Series(rows, name="value")
