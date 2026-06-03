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
# Coin universe — all backtested INR pairs on CoinSwitch
# ---------------------------------------------------------------------------
COINS = [
    # ── Core — backtested, proven positive returns ──
    "DOGE/INR",    # Combined +33.4% | HMA  +6.9% | ICHI +26.5%
    "ADA/INR",     # Combined +36.7% | HMA +20.3% | ICHI +16.4% | BEST balanced
    "XRP/INR",     # Combined +22.2% | HMA +17.1% | ICHI  +5.1%
    "ZEC/INR",     # Combined +18.3% | HMA +15.9% | ICHI  +2.4%
    "SOL/INR",     # Combined  +9.9% | HMA  -1.7% | ICHI +11.6%
    "SHIB/INR",    # Combined +10.9% | HMA  +6.5% | ICHI  +4.4%
    "BTC/INR",     # Combined  +5.6% | HMA  +5.4% | ICHI  +0.2%
    "LINK/INR",    # Combined  +3.7% | HMA  +4.1% | ICHI  -0.4%
    "INJ/INR",     # Combined  +2.7% | HMA  -1.8% | ICHI  +4.5%
    "TRX/INR",     # Combined  +2.5% | HMA  +0.3% | ICHI  +2.2%

    # ── Tier 1 additions — established coins, confirmed on CoinSwitch ──
    "DOT/INR",     # Polkadot — strong ecosystem, good HMA trends
    "ATOM/INR",    # Cosmos — consistent trending behaviour
    "NEAR/INR",    # NEAR Protocol — strong momentum coin
    "ARB/INR",     # Arbitrum — leading L2, active trading
    "UNI/INR",     # Uniswap — DeFi leader, clean trends
    "RENDER/INR",  # Render — AI/GPU narrative, strong uptrends
    "TAO/INR",     # Bittensor — AI narrative, high momentum
    "SUI/INR",     # Sui — fresh L1, active on CoinSwitch
    "LDO/INR",     # Lido — liquid staking, decent trend signals

    # ── Tier 2 additions — newer but active ──
    "APT/INR",     # Aptos — L1, decent liquidity
    "FIL/INR",     # Filecoin — storage narrative
    "JUP/INR",     # Jupiter — Solana DEX aggregator
    "VIRTUAL/INR", # Virtuals Protocol — AI agents narrative
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