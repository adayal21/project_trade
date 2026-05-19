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

STOP_LOSS_PCT         = 0.025  # tightened from 0.04 → CoinSwitch is 2x noisier;
                               # hard stop now at -2.5% (catastrophe brake only —
                               # TIME_EXIT_LOSING fires first at MAX_HOLD_BARS)
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
# 4A  Stagnant : bars >= MAX_HOLD_BARS AND |move| < 0.5%  → no movement, cut it
# 4D  Losing   : bars >= MAX_HOLD_BARS_LOSING AND move < 0% → thesis failed, cut early
#                3 bars = 3 hours of continuous loss on 1h timeframe; no point waiting
#                (fires before the hard stop-loss; only when Tier 1 hasn't fired)
# 4B  Stuck+   : bars >= MAX_HOLD_BARS_EXTENDED AND 0 < move < +2% → take small gain
# 4C  Trail TO : Tier 1 fired AND bars >= MAX_HOLD_BARS_TRAIL → exit remaining half

MAX_HOLD_BARS            = 6      # bars before 4A (stagnant) time exit is eligible
MAX_HOLD_BARS_LOSING     = 3      # bars before 4D (losing) time exit fires — 3h of
                                  # continuous loss means thesis is dead, cut it fast
TIME_EXIT_MIN_MOVE_PCT   = 0.005  # 4A threshold: |move| < 0.5% = stagnant

MAX_HOLD_BARS_EXTENDED   = 12     # 4B: exit stuck-profitable positions after this many bars
MAX_HOLD_BARS_TRAIL      = 10     # 4C: close trailing half after this many bars post Tier 1