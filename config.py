# =============================================================================
# config.py — HMA 4H Trend Bot
# =============================================================================
# Strategy : HMA(16/64) crossover + RSI(14) > 52 + 4H LinReg(50)
# Timeframe : 4H candles — run every 4 hours via cron
# Coins     : BTC, ADA, DOGE, AVAX, XRP, BNB — USDT pairs on CoinDCX
# Capital   : 20% per coin, max 5 active positions simultaneously
# Exit      : HMA cross-down + 15% hard stop loss
#
# Cron (5 min after each 4H candle close, UTC):
#   5 0,4,8,12,16,20 * * * cd /your/project && python main.py >> data/bot.log 2>&1
# =============================================================================

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------
# "paper" → simulate trades in CSV files, no real orders placed
# "live"  → connect to CoinDCX, place real market orders
#
# Start with paper. Switch to live only after satisfactory paper results.
# Override via environment variable: export TRADING_MODE=live

TRADING_MODE = os.environ.get("TRADING_MODE", "paper")

# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------
# Paper mode : INITIAL_CAPITAL is your simulated starting balance in USDT.
#              Set this to your intended live amount so paper P&L is
#              directly comparable to what live trading would produce.
#
# Live mode  : reads from LIVE_INITIAL_CAPITAL env var.
#              Set in .env file: LIVE_INITIAL_CAPITAL=10000

if TRADING_MODE == "live":
    INITIAL_CAPITAL = float(os.environ.get("LIVE_INITIAL_CAPITAL", "10000"))
else:
    INITIAL_CAPITAL = 10_000.0   # USDT — change this to your intended amount

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
# All position files, trade logs, and portfolio CSV go here.
# portfolio.py imports DATA_DIR from this file — do not rename this constant.

DATA_DIR = "data"

# ---------------------------------------------------------------------------
# Coin universe — CoinDCX USDT pairs
# ---------------------------------------------------------------------------
# Backtested on CoinDCX USDT data 2022–2026.
# All six coins confirmed profitable across full bear + bull cycle.
#
# Pair format : "COIN/USDT" internally
#               maps to "KC-COIN_USDT" for CoinDCX candles API
#               maps to "COINUSDT"     for CoinDCX order API

COINS = [
    "BTC/USDT",   # Sharpe 1.13 | +152% | MaxDD -24% | CONFIRMED
    "ADA/USDT",   # Sharpe 1.05 | +304% | MaxDD -33% | CONFIRMED
    "DOGE/USDT",  # Sharpe 0.91 | +275% | MaxDD -38% | CONFIRMED
    "AVAX/USDT",  # Sharpe 0.72 | +159% | MaxDD -45% | CONFIRMED
    "XRP/USDT",   # Sharpe 0.68 | +136% | MaxDD -37% | CONFIRMED
    "BNB/USDT",   # Sharpe 0.60 |  +62% | MaxDD -31% | POSITIVE
]

# ---------------------------------------------------------------------------
# Strategy parameters — DO NOT CHANGE
# ---------------------------------------------------------------------------
# These match utils.prepare_dataset() exactly as validated in the backtest.
# Changing any value here will invalidate the backtest results.

HMA_FAST         = 16       # fast HMA period
HMA_SLOW         = 64       # slow HMA period
RSI_PERIOD       = 14       # RSI lookback period
RSI_THRESHOLD    = 52       # RSI must be above this to enter
LINREG_LENGTH    = 50       # linear regression lookback
HMA_HALF_MODE    = "round"  # rounding mode for HMA half-period calc
HMA_SQRT_MODE    = "floor"  # rounding mode for HMA sqrt-period calc
USE_SMA          = False    # always HMA, never SMA
USE_DAILY_LINREG = False    # use 4H LinReg gate, not daily

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
# Each coin gets 20% of total current equity on entry.
# Maximum 5 coins active at once — one slot kept as cash reserve.
# At most 5 × 20% = 100% deployed, but rarely all fire simultaneously.

ALLOCATION_PCT     = 0.20   # fraction of total equity per trade
MAX_OPEN_POSITIONS = 5      # max simultaneous open positions
POSITION_SIZE      = 0.999  # tiny buffer so rounding never exceeds allocation

# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------
# Used only in paper mode for realistic P&L simulation.
# CoinDCX base taker fee = 0.2% per side (maker = 0.2% at base tier).
# Fees reduce with higher 30-day trading volume.
# At base tier (most retail traders): 0.2% per side = 0.002
#
# Check your personal fee tier at: https://coindcx.com/fees
# Update this value if your volume qualifies for a lower tier.

COMMISSION = 0.002    # 0.2% per side — CoinDCX base taker fee

# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------
# The primary exit is always the HMA cross-down signal.
# This hard stop is a last-resort protection for black swan events only.
# Set wide enough that normal 4H volatility never triggers it.
# Backtested worst trade: BTC -9.7%, AVAX -19.9% — 15% is safely outside.

STOP_LOSS_PCT = 0.15   # exit if price falls 15% below entry

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
CANDLES_URL   = "https://public.coindcx.com/market_data/candles"
TIMEFRAME     = "4h"
CANDLES_LIMIT = 500          # candles per API request
INTERVAL_MS   = 14_400_000   # 4 hours in milliseconds
WARMUP_BARS   = 400          # bars to fetch — HMA(64) needs 200+ for stable signals

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
# Set in .env file:
#   TELEGRAM_TOKEN=your_bot_token
#   TELEGRAM_CHAT_ID=your_chat_id
# Leave blank to disable.

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
# True  → print full indicator values every run (useful when debugging)
# False → clean concise logs (use this in production)

VERBOSE = False