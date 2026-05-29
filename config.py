# =============================================================================
# config.py — HMA + Ichimoku Combined Crypto Bot
# =============================================================================
# Two strategies running on 15 coins simultaneously:
#   HMA (9 coins)      : HMA(16/64) + RSI(14)>52 + LinReg(50) on 4H
#   Ichimoku (6 coins) : TK cross + Chikou + Above cloud on 4H
#
# Cron: 5 0,4,8,12,16,20 * * *  (every 4 hours, no change)
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
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")

# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------
if TRADING_MODE == "live":
    # Reads INR balance from CoinDCX — converted to USDT at trade time
    # Set LIVE_INITIAL_CAPITAL in .env to match your deposit amount in INR
    INITIAL_CAPITAL = float(os.environ.get("LIVE_INITIAL_CAPITAL", "10000"))
else:
    INITIAL_CAPITAL = 10_000.0   # Paper trading virtual capital (INR equivalent)

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
DATA_DIR = "data"

# ---------------------------------------------------------------------------
# Coin universe
# ---------------------------------------------------------------------------
# Listed in priority order — when MAX_OPEN_POSITIONS is hit,
# lower-ranked coins are skipped for that run.
#
# HMA coins  : best results with HMA crossover strategy (Sharpe 0.54-1.42)
# ICHI coins : best results with Ichimoku 3-condition entry (Sharpe 0.49-0.96)
# Skipped    : XRP, ATOM, LTC, DOT — neither strategy profitable on these

COINS = [
    # 12 validated coins — both HMA and Ichimoku run independently on each
    # Removed: DOT (-1.5% combined), LTC (-74.7%), ATOM (-93.0%)
    # 24 total independent slots (12 coins x 2 strategies)
    #
    # Combined backtest 2022-2026 (4H, 0.2% commission):
    "DOGE/USDT",   # Combined +271% | HMA +133% | ICHI +138% | BEST
    "ADA/USDT",    # Combined +199% | HMA +152% | ICHI  +47%
    "MATIC/USDT",  # Combined +166% | HMA  +86% | ICHI  +80%
    "SOL/USDT",    # Combined +160% | HMA  +74% | ICHI  +86%
    "BNB/USDT",    # Combined  +89% | HMA  +21% | ICHI  +68%
    "XRP/USDT",    # Combined  +87% | HMA  +91% | ICHI   -4%
    "BTC/USDT",    # Combined  +82% | HMA  +65% | ICHI  +17%
    "AVAX/USDT",   # Combined  +82% | HMA  +79% | ICHI   +3%
    "ETH/USDT",    # Combined  +40% | HMA  -27% | ICHI  +67%
    "LINK/USDT",   # Combined  +36% | HMA  -29% | ICHI  +65%
    "ZEC/USDT",    # HMA Sharpe 1.42 | Best HMA Sharpe overall
    "JASMY/USDT",  # HMA Sharpe 0.90 | 4-year consistent
]

# Both HMA and Ichimoku run independently on ALL 15 coins.
# Each strategy can open its own position on the same coin simultaneously.
# Double-confirmed entries (both fire) get priority in the entry queue.
# Strategies available: "hma", "ichimoku"
STRATEGIES = ["hma", "ichimoku"]

# ---------------------------------------------------------------------------
# HMA strategy parameters — DO NOT CHANGE
# ---------------------------------------------------------------------------
HMA_FAST         = 16
HMA_SLOW         = 64
RSI_PERIOD       = 14
RSI_THRESHOLD    = 52
LINREG_LENGTH    = 50
HMA_HALF_MODE    = "round"
HMA_SQRT_MODE    = "floor"
USE_SMA          = False
USE_DAILY_LINREG = False

# ---------------------------------------------------------------------------
# Ichimoku strategy parameters — DO NOT CHANGE
# ---------------------------------------------------------------------------
ICHI_TENKAN  = 9    # Conversion line period
ICHI_KIJUN   = 26   # Base line period
ICHI_SENKOU  = 52   # Senkou Span B period

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
ALLOCATION_PCT     = 0.10   # 10% per trade — max 8 active = 80% deployed
MAX_OPEN_POSITIONS = 8      # max simultaneous positions across 30 slots (15 coins x 2 strategies)
POSITION_SIZE      = 0.999  # tiny rounding buffer

# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------
COMMISSION = 0.002   # 0.2% per side — CoinDCX actual taker fee

# ---------------------------------------------------------------------------
# Stop loss (HMA coins only — Ichimoku uses Kijun as dynamic stop)
# ---------------------------------------------------------------------------
STOP_LOSS_PCT = 0.15   # 15% hard stop for HMA coins

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
CANDLES_URL   = "https://public.coindcx.com/market_data/candles"
TIMEFRAME     = "4h"
CANDLES_LIMIT = 500
INTERVAL_MS   = 14_400_000
WARMUP_BARS   = 600

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
VERBOSE = False