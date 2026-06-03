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
    "DOGE/INR",    # Combined +271% | HMA +133% | ICHI +138% | BEST
    "ADA/INR",     # Combined +199% | HMA +152% | ICHI  +47%
    "SHIB/INR",    # Replaces MATIC — high volume, active on CoinDCX INR
    "SOL/INR",     # Combined +160% | HMA  +74% | ICHI  +86%
    "BNB/INR",     # Combined  +89% | HMA  +21% | ICHI  +68%
    "XRP/INR",     # Combined  +87% | HMA  +91% | ICHI   -4%
    "BTC/INR",     # Combined  +82% | HMA  +65% | ICHI  +17% | RSI 50
    "AVAX/INR",    # Combined  +82% | HMA  +79% | ICHI   +3%
    "ETH/INR",     # Combined  +40% | HMA  -27% | ICHI  +67% | RSI 50
    "LINK/INR",    # Combined  +36% | HMA  -29% | ICHI  +65%
    "ZEC/INR",     # HMA Sharpe 1.42 | Best HMA Sharpe overall
    "INJ/INR",     # Replaces LTC — strong trending coin, fresh data
    "TRX/INR",     # High volume on CoinDCX INR
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
RSI_THRESHOLD_OVERRIDE = {}

# Exit frequency — ALL coins use 1H exit (backtest validated: best Sharpe 0.98,
# lowest drawdown -24.7%, best profit factor 1.68 vs 4H+4H combo)
# Entry remains 4H only. Exit checked every hourly cron run via 1H candles.
HMA_EXIT_FREQUENCY = {
    "BTC/INR":   "1h",
    "ETH/INR":   "1h",
    "DOGE/INR":  "1h",
    "XRP/INR":   "1h",
    "SOL/INR":   "1h",
    "ADA/INR":   "1h",
    "BNB/INR":   "1h",
    "AVAX/INR":  "1h",
    "LINK/INR":  "1h",
    "ZEC/INR":   "1h",
    "SHIB/INR":  "1h",
    "INJ/INR":   "1h",
    "TRX/INR":   "1h",
}

# Per-coin HMA gap filter for mid-trend entry — based on backtest analysis
# None = crossover only (strict)
# 0.02 = allow mid-trend entry when gap ≤ 2% (not too extended)
# Coins that benefit from mid-trend entry: DOGE +140%, ETH +14%, XRP +27%, BNB +34%
# Coins that perform better with strict crossover: BTC, ZEC, ADA, SOL, AVAX, LINK, JASMY, POL
HMA_GAP_FILTER = {}

# Per-coin Ichimoku Chikou condition override — based on backtest analysis
# True  = require chikou (current strict behavior)
# False = skip chikou check (enter on TK cross + cloud only)
# Removing chikou improves avg return from +22% to +48.4% on most coins
# ETH and BNB perform better WITH chikou — keep strict for those
ICHI_REQUIRE_CHIKOU = {
    "BTC/INR":   False,  # NoCloud best but NoChikou also +8.8% better
    "ETH/INR":   True,   # Full is best — keep chikou
    "DOGE/INR":  False,  # NoCloud +106.6% better
    "ADA/INR":   False,  # NoChikou +18.4% better
    "SOL/INR":   False,  # NoChikou +133% better
    "BNB/INR":   True,   # Full is best — keep chikou
    "XRP/INR":   False,  # NoChikou +44.8% better
    "AVAX/INR":  False,  # NoChikou +31.2% better
    "LINK/INR":  False,  # NoCloud +49.6% better
    "ZEC/INR":   False,  # NoChikou +167.9% better
    "SHIB/INR":  False,  # Default — no backtest yet
    "INJ/INR":   False,  # Default — no backtest yet
    "TRX/INR":   False,  # Default — no backtest yet
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
WARMUP_BARS   = 400

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
VERBOSE = False