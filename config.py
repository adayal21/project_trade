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

    # ── Tier 1 additions — established coins, fresh data confirmed ──
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
HMA_EXIT_FREQUENCY = {c: "1h" for c in [
    "DOGE/INR", "ADA/INR", "XRP/INR", "ZEC/INR", "SOL/INR",
    "SHIB/INR", "BTC/INR", "LINK/INR", "INJ/INR", "TRX/INR",
    "DOT/INR", "ATOM/INR", "NEAR/INR", "ARB/INR", "UNI/INR",
    "RENDER/INR", "TAO/INR", "SUI/INR", "LDO/INR",
    "APT/INR", "FIL/INR", "JUP/INR", "VIRTUAL/INR",
]}

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
    # Backtested coins — validated settings
    "DOGE/INR":    False,
    "ADA/INR":     False,
    "XRP/INR":     False,
    "ZEC/INR":     False,
    "SOL/INR":     False,
    "SHIB/INR":    False,
    "BTC/INR":     False,
    "LINK/INR":    False,
    "INJ/INR":     False,
    "TRX/INR":     False,
    # New additions — default False (no backtest yet)
    "DOT/INR":     False,
    "ATOM/INR":    False,
    "NEAR/INR":    False,
    "ARB/INR":     False,
    "UNI/INR":     False,
    "RENDER/INR":  False,
    "TAO/INR":     False,
    "SUI/INR":     False,
    "LDO/INR":     False,
    "APT/INR":     False,
    "FIL/INR":     False,
    "JUP/INR":     False,
    "VIRTUAL/INR": False,
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