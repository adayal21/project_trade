# =============================================================================
# config.py — HMA + Ichimoku Combined Crypto Bot (CoinSwitch PRO)
# =============================================================================
# Two strategies running on all INR pairs simultaneously:
#   HMA       : HMA(16/64) + RSI(14) + LinReg(50) on 4H
#   Ichimoku  : TK cross + Chikou + Above cloud on 4H
#
# Data source : CoinSwitch PRO API (coinswitchx exchange)
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
# Coin universe — fallback list used only if CoinSwitch API is unreachable.
# At runtime, all active INR pairs are fetched live from the API instead.
#
# This list = 17 coins that were BOTH HMA-positive AND Ichimoku-positive
# on the INR backtest (333 days, 4H candles). Used as emergency fallback.
# ---------------------------------------------------------------------------
COINS = [
    "ICP/INR",       # HMA +113.5% | ICHI  +92.9% | Combined +206.4%
    "VVV/INR",       # HMA +157.1% | ICHI   +4.0% | Combined +161.1%
    "ORDI/INR",      # HMA  +41.5% | ICHI  +80.1% | Combined +121.6%
    "VIRTUAL/INR",   # HMA  +55.8% | ICHI  +57.5% | Combined +113.3%
    "TON/INR",       # HMA  +57.9% | ICHI  +42.2% | Combined +100.1%
    "ZEC/INR",       # HMA  +20.9% | ICHI  +72.0% | Combined  +92.9%
    "EPIC/INR",      # HMA  +37.1% | ICHI  +43.1% | Combined  +80.2%
    "SAHARA/INR",    # HMA  +15.0% | ICHI  +65.2% | Combined  +80.2%
    "IP/INR",        # HMA  +15.7% | ICHI  +55.4% | Combined  +71.1%
    "NEAR/INR",      # HMA  +17.3% | ICHI  +30.7% | Combined  +48.0%
    "GIGGLE/INR",    # HMA   +9.5% | ICHI  +36.9% | Combined  +46.4%
    "DASH/INR",      # HMA  +10.5% | ICHI  +31.1% | Combined  +41.6%
    "SPX/INR",       # HMA  +30.6% | ICHI   +1.6% | Combined  +32.2%
    "AIXBT/INR",     # HMA  +26.1% | ICHI   +3.5% | Combined  +29.6%
    "PIXEL/INR",     # HMA   +5.9% | ICHI  +19.5% | Combined  +25.4%
    "PUMP/INR",      # HMA  +14.3% | ICHI   +5.0% | Combined  +19.3%
    "STO/INR",       # HMA   +0.2% | ICHI +284.5% | Combined +284.7%
]

STRATEGIES = ["hma", "ichimoku"]

# ---------------------------------------------------------------------------
# HMA strategy parameters
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

RSI_THRESHOLD_OVERRIDE = {}

HMA_EXIT_FREQUENCY = {c: "1h" for c in COINS}

HMA_GAP_FILTER = {}

ICHI_REQUIRE_CHIKOU = {c: False for c in COINS}

# ---------------------------------------------------------------------------
# Ichimoku strategy parameters — DO NOT CHANGE
# ---------------------------------------------------------------------------
ICHI_TENKAN  = 9
ICHI_KIJUN   = 26
ICHI_SENKOU  = 52

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
ALLOCATION_PCT     = 0.20
MAX_OPEN_POSITIONS = 4
POSITION_SIZE      = 0.999

# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------
COMMISSION = 0.002   # 0.2% per side — CoinSwitch taker fee

# ---------------------------------------------------------------------------
# Stop loss (HMA coins only — Ichimoku uses Kijun as dynamic stop)
# ---------------------------------------------------------------------------
STOP_LOSS_PCT = 0.15

# ---------------------------------------------------------------------------
# CoinSwitch PRO API — data fetching
# ---------------------------------------------------------------------------
# Exchange identifier for INR spot pairs on CoinSwitch PRO
CS_EXCHANGE   = "coinswitchx"
CS_BASE_URL   = "https://coinswitch.co"

# Candle intervals in minutes — CoinSwitch uses integer minutes
TIMEFRAME_4H_MIN  = 240    # 4 hours
TIMEFRAME_1H_MIN  = 60     # 1 hour

# How many bars to fetch for warmup
WARMUP_BARS_4H = 400       # 400 × 4H = ~66 days
WARMUP_BARS_1H = 500       # 500 × 1H = ~21 days

# Interval durations in milliseconds (used to compute start_time windows)
INTERVAL_MS_4H = 14_400_000   # 4h in ms
INTERVAL_MS_1H =  3_600_000   # 1h in ms

# Max candles per API request (API doesn't document a hard limit; 500 is safe)
CANDLES_LIMIT  = 500

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
VERBOSE = False