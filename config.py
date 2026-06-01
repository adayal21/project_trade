# =============================================================================
# config.py — HMA + Ichimoku Combined Crypto Bot
# =============================================================================
# Two strategies running on 12 coins simultaneously:
#   HMA       : HMA(16/64) + RSI(14) + LinReg(50) on 4H
#   Ichimoku  : TK cross + Chikou + Above cloud on 4H
#
# Cron: 5 * * * *  (every hour — exits checked hourly, entries at 4H closes)
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
    INITIAL_CAPITAL = float(os.environ.get("LIVE_INITIAL_CAPITAL", "10000"))
else:
    INITIAL_CAPITAL = 10_000.0

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
DATA_DIR = "data"

# ---------------------------------------------------------------------------
# Coin universe
# ---------------------------------------------------------------------------
COINS = [
    "DOGE/USDT",   # Combined +271% | HMA +133% | ICHI +138% | BEST
    "ADA/USDT",    # Combined +199% | HMA +152% | ICHI  +47%
    "POL/USDT",    # Combined +166% | HMA  +86% | ICHI  +80%
    "SOL/USDT",    # Combined +160% | HMA  +74% | ICHI  +86%
    "BNB/USDT",    # Combined  +89% | HMA  +21% | ICHI  +68%
    "XRP/USDT",    # Combined  +87% | HMA  +91% | ICHI   -4%
    "BTC/USDT",    # Combined  +82% | HMA  +65% | ICHI  +17% | RSI 50
    "AVAX/USDT",   # Combined  +82% | HMA  +79% | ICHI   +3%
    "ETH/USDT",    # Combined  +40% | HMA  -27% | ICHI  +67% | RSI 50
    "LINK/USDT",   # Combined  +36% | HMA  -29% | ICHI  +65%
    "ZEC/USDT",    # HMA Sharpe 1.42 | Best HMA Sharpe overall
    "JASMY/USDT",  # HMA Sharpe 0.90 | 4-year consistent
]

STRATEGIES = ["hma", "ichimoku"]

# ---------------------------------------------------------------------------
# HMA strategy parameters
# ---------------------------------------------------------------------------
HMA_FAST         = 16
HMA_SLOW         = 64
RSI_PERIOD       = 14
RSI_THRESHOLD    = 52       # default RSI threshold for all coins
LINREG_LENGTH    = 50
HMA_HALF_MODE    = "round"
HMA_SQRT_MODE    = "floor"
USE_SMA          = False
USE_DAILY_LINREG = False

# Per-coin RSI overrides — based on backtest analysis
# BTC and ETH perform better at RSI 50 (+27.9% and +35.6% improvement)
RSI_THRESHOLD_OVERRIDE = {
    "BTC/USDT": 50,
    "ETH/USDT": 50,
}

# Per-coin exit frequency — based on backtest analysis
# "1h" = check exit every hour (catches fast reversals)
# "4h" = check exit only on 4H candle close (better for slow steady trends)
# BTC/ETH/DOGE/XRP/SOL perform better with 4H exit (slow trends)
# ADA/BNB/AVAX/LINK/ZEC/JASMY/POL perform better with 1H exit
HMA_EXIT_FREQUENCY = {
    "BTC/USDT":   "4h",   # 4H better: +138.6% vs +92.7%
    "ETH/USDT":   "4h",   # 4H better: +49.3% vs -10.2%
    "DOGE/USDT":  "4h",   # 4H better: +361.7% vs -16.6%
    "XRP/USDT":   "4h",   # 4H better: +145.3% vs +23.5%
    "SOL/USDT":   "4h",   # 4H better: +66.9% vs +35.1%
    "ADA/USDT":   "1h",   # 1H better: +157.0% vs +29.2%
    "BNB/USDT":   "1h",   # 1H better: +30.6% vs +21.9%
    "AVAX/USDT":  "1h",   # 1H better: +292.0% vs -1.2%
    "LINK/USDT":  "1h",   # 1H better: +155.0% vs -43.0%
    "ZEC/USDT":   "1h",   # 1H better: +689.4% vs +607.5%
    "JASMY/USDT": "1h",   # 1H better: +415.3% vs -35.9%
    "POL/USDT":   "1h",   # 1H better: -4.4% vs -12.7%
}

# Per-coin HMA gap filter for mid-trend entry — based on backtest analysis
# None = crossover only (strict)
# 0.02 = allow mid-trend entry when gap ≤ 2% (not too extended)
# Coins that benefit from mid-trend entry: DOGE +140%, ETH +14%, XRP +27%, BNB +34%
# Coins that perform better with strict crossover: BTC, ZEC, ADA, SOL, AVAX, LINK, JASMY, POL
HMA_GAP_FILTER = {
    "DOGE/USDT": 0.02,   # +366% → +506% with gap ≤2%
    "ETH/USDT":  0.02,   # +49%  → +63%  with gap ≤2%
    "XRP/USDT":  0.02,   # +147% → +174% with gap ≤2%
    "BNB/USDT":  0.02,   # +23%  → +31%  with gap ≤2%
}

# Per-coin Ichimoku Chikou condition override — based on backtest analysis
# True  = require chikou (current strict behavior)
# False = skip chikou check (enter on TK cross + cloud only)
# Removing chikou improves avg return from +22% to +48.4% on most coins
# ETH and BNB perform better WITH chikou — keep strict for those
ICHI_REQUIRE_CHIKOU = {
    "BTC/USDT":   False,  # NoCloud best but NoChikou also +8.8% better
    "ETH/USDT":   True,   # Full is best — keep chikou
    "DOGE/USDT":  False,  # NoCloud +106.6% better
    "ADA/USDT":   False,  # NoChikou +18.4% better
    "SOL/USDT":   False,  # NoChikou +133% better
    "BNB/USDT":   True,   # Full is best — keep chikou
    "XRP/USDT":   False,  # NoChikou +44.8% better
    "AVAX/USDT":  False,  # NoChikou +31.2% better
    "LINK/USDT":  False,  # NoCloud +49.6% better
    "ZEC/USDT":   False,  # NoChikou +167.9% better
    "JASMY/USDT": False,  # NoCloud +89.1% better
    "POL/USDT":   False,  # NoChikou +28.1% better
}

# ---------------------------------------------------------------------------
# Ichimoku strategy parameters — DO NOT CHANGE
# ---------------------------------------------------------------------------
ICHI_TENKAN  = 9
ICHI_KIJUN   = 26
ICHI_SENKOU  = 52

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
ALLOCATION_PCT     = 0.20   # 20% per trade — max 4 active = 80% deployed
MAX_OPEN_POSITIONS = 4      # max simultaneous positions
POSITION_SIZE      = 0.999  # tiny rounding buffer

# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------
COMMISSION = 0.002   # 0.2% per side — CoinDCX actual taker fee

# ---------------------------------------------------------------------------
# Stop loss (HMA coins only — Ichimoku uses Kijun as dynamic stop)
# ---------------------------------------------------------------------------
STOP_LOSS_PCT = 0.15

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