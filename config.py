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
    "DOGE/INR",    # Combined +33.4% | HMA  +6.9% | ICHI +26.5% | ICHI dominates
    "ADA/INR",     # Combined +36.7% | HMA +20.3% | ICHI +16.4% | BEST balanced
    "XRP/INR",     # Combined +22.2% | HMA +17.1% | ICHI  +5.1% | HMA dominates
    "ZEC/INR",     # Combined +18.3% | HMA +15.9% | ICHI  +2.4% | HMA dominates
    "SOL/INR",     # Combined  +9.9% | HMA  -1.7% | ICHI +11.6% | ICHI dominates
    "SHIB/INR",    # Combined +10.9% | HMA  +6.5% | ICHI  +4.4% | balanced
    "BTC/INR",     # Combined  +5.6% | HMA  +5.4% | ICHI  +0.2% | HMA dominates
    "LINK/INR",    # Combined  +3.7% | HMA  +4.1% | ICHI  -0.4% | marginal
    "INJ/INR",     # Combined  +2.7% | HMA  -1.8% | ICHI  +4.5% | marginal
    "TRX/INR",     # Combined  +2.5% | HMA  +0.3% | ICHI  +2.2% | marginal
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
    "DOGE/INR":  "1h",
    "ADA/INR":   "1h",
    "XRP/INR":   "1h",
    "ZEC/INR":   "1h",
    "SOL/INR":   "1h",
    "SHIB/INR":  "1h",
    "BTC/INR":   "1h",
    "LINK/INR":  "1h",
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
    "DOGE/INR":  False,  # ICHI dominates — NoCloud better
    "ADA/INR":   False,  # Both strategies strong — NoChikou validated
    "XRP/INR":   False,  # HMA dominates — NoChikou +44.8% better
    "ZEC/INR":   False,  # HMA dominates — NoChikou +167.9% better
    "SOL/INR":   False,  # ICHI dominates — NoChikou +133% better
    "SHIB/INR":  False,  # Default — no prior backtest
    "BTC/INR":   False,  # HMA dominates — NoChikou better
    "LINK/INR":  False,  # NoCloud +49.6% better
    "INJ/INR":   False,  # Default — no prior backtest
    "TRX/INR":   False,  # Default — no prior backtest
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