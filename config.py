INITIAL_CAPITAL = 10000

API_KEY = "38b6bfd0bb3971832f66b726a1e34c31b02c4447fd8e9b295df729edc7f903ca"
API_SECRET = "daa9575ba1a6f3de50e46d787b8a54ed2b4162d4c691acaa3663da9e84305347"
BASE_URL = "https://coinswitch.co"

EXCHANGE = "coinswitchx"

COINS = [
    "BTC/INR",
    "ETH/INR",
    "XRP/INR",
    "BNB/INR",
    "DOGE/INR",
    "LINK/INR",
    "LTC/INR"
]

# ---------------------------------------------------------------------------
# Core risk constants
# ---------------------------------------------------------------------------

STOP_LOSS_PCT         = 0.04   # 4% adverse move closes the full position
MAX_ALLOCATION        = 0.80   # never deploy more than 80% of cash at once
MAX_POSITIONS_PER_DIR = 3      # allow up to 3 LONGs and 3 SHORTs simultaneously
TIMEFRAME             = "1h"
RISK_PER_TRADE        = 0.10
DATA_DIR              = "data"

# ---------------------------------------------------------------------------
# BTC Regime filter — recalibrated for CoinSwitch (INR candles ~2x noisier)
# ---------------------------------------------------------------------------
# Old Coinbase approach: hard AND gate (EMA200 AND Supertrend must both agree).
# Problem: CoinSwitch BTC candles are 2x more volatile (0.55% vs 0.27% per bar),
# causing Supertrend to flip every few bars around the EMA200, yielding permanent
# NEUTRAL and blocking all altcoin entries.
#
# New approach: 3-indicator majority vote. 2-of-3 must agree:
#   1. EMA200 (Close vs EMA200)
#   2. Supertrend (confirmed over 2 consecutive bars to debounce noise)
#   3. EMA20 vs EMA50 short-term momentum
#
# This tolerates one disagreeing indicator — exactly the noise profile seen
# in the CoinSwitch comparison data.

REGIME_ST_CONFIRM_BARS  = 2     # Supertrend must agree for this many consecutive bars
                                 # before counting as a regime vote (debounces flips)
REGIME_VOTE_THRESHOLD   = 2     # out of 3 indicators must agree for LONG or SHORT

# ---------------------------------------------------------------------------
# Signal generation — recalibrated for CoinSwitch noise
# ---------------------------------------------------------------------------
# CoinSwitch INR pairs trend at lower ADX readings than USD pairs on Coinbase
# because INR volatility inflates ATR without adding directional trend strength.

ADX_THRESHOLD       = 18.0   # was 25.0 on Coinbase; CS INR pairs trend at lower ADX
ATR_EXPANSION_RATIO = 0.50   # was 0.60; CS ATR is noisier so ratio vs SMA runs lower

# ---------------------------------------------------------------------------
# Tier 1 — Partial profit-taking (scale-out at first target)
# ---------------------------------------------------------------------------

PARTIAL_TAKE_PROFIT_PCT  = 0.02   # close PARTIAL_EXIT_RATIO of position when +2% in profit
PARTIAL_EXIT_RATIO       = 0.50   # fraction of position to close at first target (50%)

# ---------------------------------------------------------------------------
# Tier 2 — Trailing stop on the remaining half
# ---------------------------------------------------------------------------

TRAILING_STOP_PCT        = 0.015  # trail 1.5% below the highest-water-mark price seen
                                  # only activates after Tier 1 partial exit fires

# ---------------------------------------------------------------------------
# Tier 4 — Time-based exit backstop
# ---------------------------------------------------------------------------

MAX_HOLD_BARS            = 6      # if a position has been open >= 6 bars with no
                                  # meaningful move, close it and free up capital
TIME_EXIT_MIN_MOVE_PCT   = 0.005  # "meaningful move" threshold: if |move| < 0.5%
                                  # after MAX_HOLD_BARS, trigger the time exit

MAX_HOLD_BARS_EXTENDED   = 12     # if a position is up but stuck below the +2% partial
                                  # take-profit target after this many bars, exit with
                                  # whatever small gain exists rather than waiting forever
MAX_HOLD_BARS_TRAIL      = 10     # after Tier 1 partial exit fires, close the remaining
                                  # half if it hasn't trailed out within this many bars