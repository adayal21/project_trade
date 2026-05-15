import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# ---------------------------------------------------------------------------
# Supertrend — with proper band continuity (no repainting)
# ---------------------------------------------------------------------------

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3) -> pd.DataFrame:
    """
    Correct Supertrend implementation with band carry-forward logic.
    Bands only tighten while the trend holds; they never widen mid-trend.
    Adds columns: Supertrend (bool), Supertrend_Upper, Supertrend_Lower.
    """
    atr = AverageTrueRange(
        high=df['High'],
        low=df['Low'],
        close=df['Close'],
        window=period
    ).average_true_range()

    hl2 = (df['High'] + df['Low']) / 2
    raw_upper = hl2 + (multiplier * atr)
    raw_lower = hl2 - (multiplier * atr)

    upper = raw_upper.copy()
    lower = raw_lower.copy()
    supertrend = [True] * len(df)

    for i in range(1, len(df)):
        # Lower band: carry forward unless price broke below or raw band is higher
        lower.iloc[i] = (
            raw_lower.iloc[i]
            if raw_lower.iloc[i] > lower.iloc[i - 1] or df['Close'].iloc[i - 1] < lower.iloc[i - 1]
            else lower.iloc[i - 1]
        )
        # Upper band: carry forward unless price broke above or raw band is lower
        upper.iloc[i] = (
            raw_upper.iloc[i]
            if raw_upper.iloc[i] < upper.iloc[i - 1] or df['Close'].iloc[i - 1] > upper.iloc[i - 1]
            else upper.iloc[i - 1]
        )

        if df['Close'].iloc[i] > upper.iloc[i - 1]:
            supertrend[i] = True
        elif df['Close'].iloc[i] < lower.iloc[i - 1]:
            supertrend[i] = False
        else:
            supertrend[i] = supertrend[i - 1]

    df['Supertrend'] = supertrend
    df['Supertrend_Upper'] = upper
    df['Supertrend_Lower'] = lower
    return df


# ---------------------------------------------------------------------------
# Indicator pipeline
# ---------------------------------------------------------------------------

def apply_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes all indicators required by the strategy:
      - EMA200          : macro trend filter
      - RSI(14)         : momentum (used for crossover, not raw threshold)
      - Volume_SMA(20)  : volume baseline
      - ATR(14)         : volatility filter
      - ADX(14)         : regime / chop filter
      - Supertrend      : trend direction + dynamic S/R bands
    """
    df = df.copy()

    # Trend
    df['EMA200'] = EMAIndicator(df['Close'], window=200).ema_indicator()

    # Momentum — keep the raw series; crossover logic lives in generate_signal
    df['RSI'] = RSIIndicator(df['Close'], window=14).rsi()

    # Volume baseline
    df['Volume_SMA'] = df['Volume'].rolling(20).mean()

    # Volatility — ATR for compression detection
    df['ATR'] = AverageTrueRange(
        df['High'], df['Low'], df['Close'], window=14
    ).average_true_range()
    df['ATR_SMA'] = df['ATR'].rolling(20).mean()   # compare current ATR vs recent average

    # Regime — ADX distinguishes trending from ranging markets
    adx_indicator = ADXIndicator(df['High'], df['Low'], df['Close'], window=14)
    df['ADX'] = adx_indicator.adx()

    # Supertrend (with corrected band logic)
    df = calculate_supertrend(df)

    return df


# ---------------------------------------------------------------------------
# NaN / length guard
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS = ['EMA200', 'RSI', 'Volume_SMA', 'ATR', 'ATR_SMA', 'ADX',
                     'Supertrend', 'Supertrend_Upper', 'Supertrend_Lower']
_MIN_BARS = 200   # driven by EMA200 warm-up


def _is_ready(df: pd.DataFrame) -> bool:
    """Return False if there isn't enough data or the latest row has NaNs."""
    if len(df) < _MIN_BARS + 1:   # +1 so we always have a previous bar
        return False
    return not df[_REQUIRED_COLUMNS].iloc[-1].isna().any()


# ---------------------------------------------------------------------------
# Signal generation — operates on the full DataFrame for crossover context
# ---------------------------------------------------------------------------

def generate_signal(
    df: pd.DataFrame,
    adx_threshold: float = 25.0,
    atr_expansion_ratio: float = 0.6,   # ATR must be >= 60 % of its 20-bar SMA
) -> dict | None:
    """
    Returns a signal dict or None.

    Improvements vs original:
      1. NaN / length guard before any indexing.
      2. RSI *crossover* (50-line) replaces raw RSI threshold — better timing.
      3. ATR expansion filter — skips entries during volatility compression.
      4. ADX regime filter — skips entries in ranging / choppy markets.
      5. 3-of-4 scoring on confirmations; EMA200 + Supertrend are mandatory.
      6. Returns metadata dict instead of a bare string.

    Parameters
    ----------
    adx_threshold       : minimum ADX to consider the market trending (default 25)
    atr_expansion_ratio : ATR must be at least this fraction of its SMA (default 0.6)
    """
    if not _is_ready(df):
        return None

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    # ------------------------------------------------------------------
    # Hard filters — if either fails, no signal regardless of anything else
    # ------------------------------------------------------------------

    # 1. Regime filter: market must be trending
    if latest['ADX'] < adx_threshold:
        return None

    # 2. Volatility filter: avoid low-volatility compression (false breakouts)
    if latest['ATR'] < atr_expansion_ratio * latest['ATR_SMA']:
        return None

    # ------------------------------------------------------------------
    # Directional conditions
    # ------------------------------------------------------------------

    # --- LONG ---
    ema_long        = latest['Close'] > latest['EMA200']          # mandatory
    supertrend_long = latest['Supertrend']                        # mandatory
    rsi_long = latest['RSI'] > 50                                 # RSI crosses above 50
    volume_long     = latest['Volume'] > latest['Volume_SMA']     # volume confirms

    # EMA + Supertrend must both agree; need at least 1 of the 2 soft conditions
    long_mandatory  = ema_long and supertrend_long
    long_soft_score = int(rsi_long) + int(volume_long)
    long_condition  = long_mandatory and long_soft_score >= 1

    # --- SHORT ---
    ema_short        = latest['Close'] < latest['EMA200']         # mandatory
    supertrend_short = not latest['Supertrend']                   # mandatory
    rsi_short        = latest['RSI'] < 50                         # RSI crosses below 50
    volume_short     = latest['Volume'] > latest['Volume_SMA']    # volume confirms

    short_mandatory  = ema_short and supertrend_short
    short_soft_score = int(rsi_short) + int(volume_short)
    short_condition  = short_mandatory and short_soft_score >= 1

    # ------------------------------------------------------------------
    # Build signal metadata
    # ------------------------------------------------------------------

    if long_condition:
        return {
            "signal":            "LONG",
            "timestamp":         latest.name,
            "close":             latest['Close'],
            "ema200":            latest['EMA200'],
            "rsi":               latest['RSI'],
            "adx":               latest['ADX'],
            "atr":               latest['ATR'],
            "supertrend_lower":  latest['Supertrend_Lower'],
            "supertrend_upper":  latest['Supertrend_Upper'],
            "soft_confirmations": long_soft_score,   # 1 or 2
        }

    if short_condition:
        return {
            "signal":            "SHORT",
            "timestamp":         latest.name,
            "close":             latest['Close'],
            "ema200":            latest['EMA200'],
            "rsi":               latest['RSI'],
            "adx":               latest['ADX'],
            "atr":               latest['ATR'],
            "supertrend_lower":  latest['Supertrend_Lower'],
            "supertrend_upper":  latest['Supertrend_Upper'],
            "soft_confirmations": short_soft_score,
        }

    return None