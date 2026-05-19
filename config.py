INITIAL_CAPITAL = 10000

COINS = [
    "BTC/INR",
    "ETH/INR",
    "XRP/INR",
    "BNB/INR",
    "DOGE/INR",
    "LINK/INR",
    "LTC/INR",
]

# ---------------------------------------------------------------------------
# Core risk constants
# ---------------------------------------------------------------------------
# Calibrated for noisy INR pairs (~0.5%+ per-bar volatility vs ~0.3% on
# Coinbase USD pairs). May need re-tuning after a few days of CoinDCX
# paper-trading data.

STOP_LOSS_PCT         = 0.040  # hard stop — catastrophe brake. TIME_EXIT_LOSING
                               # typically fires first at MAX_HOLD_BARS_LOSING.
MAX_ALLOCATION        = 0.80   # never deploy more than 80% of equity at once
MAX_POSITIONS_PER_DIR = 3      # allow up to 3 LONGs and 3 SHORTs simultaneously
TIMEFRAME             = "1h"
RISK_PER_TRADE        = 0.10
DATA_DIR              = "data"

# ---------------------------------------------------------------------------
# BTC Regime filter — 3-indicator majority vote (2-of-3 must agree)
# ---------------------------------------------------------------------------
# Macro market direction is derived from BTC's:
#   1. EMA200      : Close vs EMA200
#   2. Supertrend  : confirmed over REGIME_ST_CONFIRM_BARS consecutive bars
#   3. EMA20/EMA50 : short-term momentum
#
# Majority vote tolerates one disagreeing indicator — important on noisy
# INR pairs where Supertrend can flip on individual bars around EMA200.

REGIME_ST_CONFIRM_BARS = 2  # Supertrend must agree for this many bars
                             # to count as a regime vote (debounces flips)
REGIME_VOTE_THRESHOLD  = 2  # out of 3 indicators must agree for LONG or SHORT

# ---------------------------------------------------------------------------
# Signal generation thresholds
# ---------------------------------------------------------------------------
# INR pairs trend at lower ADX readings than USD pairs because INR
# volatility inflates ATR without adding directional trend strength.

ADX_THRESHOLD       = 18.0   # min ADX for a market to count as trending
ATR_EXPANSION_RATIO = 0.50   # ATR must be >= 50% of its 20-bar SMA
                              # (skips entries during volatility compression)

# ---------------------------------------------------------------------------
# Tier 1 — Partial profit-taking (scale-out at first target)
# ---------------------------------------------------------------------------

PARTIAL_TAKE_PROFIT_PCT = 0.030   # close PARTIAL_EXIT_RATIO of position at +3%
PARTIAL_EXIT_RATIO      = 0.50    # fraction of position to close at first target

# ---------------------------------------------------------------------------
# Tier 2 — Trailing stop on the remaining half
# ---------------------------------------------------------------------------

TRAILING_STOP_PCT       = 0.020   # trail 2% below the high-water mark price
                                   # only activates after Tier 1 partial exit fires

# ---------------------------------------------------------------------------
# Tier 4 — Time-based exit backstop
# ---------------------------------------------------------------------------
# 4A  Stagnant : bars >= MAX_HOLD_BARS         AND |move| < 0.5%   → no movement, cut it
# 4D  Losing   : bars >= MAX_HOLD_BARS_LOSING  AND move < 0%       → thesis failed, cut early
#                3 bars = 3h of continuous loss on 1h timeframe; no point waiting
#                (fires before the hard stop-loss; only when Tier 1 hasn't fired)
# 4B  Stuck+   : bars >= MAX_HOLD_BARS_EXTENDED AND 0 < move < +2% → take small gain
# 4C  Trail TO : Tier 1 fired AND bars >= MAX_HOLD_BARS_TRAIL      → exit remaining half

MAX_HOLD_BARS            = 6      # bars before 4A (stagnant) time exit is eligible
MAX_HOLD_BARS_LOSING     = 3      # bars before 4D (losing) time exit fires
TIME_EXIT_MIN_MOVE_PCT   = 0.005  # 4A threshold: |move| < 0.5% = stagnant
MAX_HOLD_BARS_EXTENDED   = 12     # 4B: exit stuck-profitable positions after this many bars
MAX_HOLD_BARS_TRAIL      = 10     # 4C: close trailing half after this many bars post Tier 1

# ---------------------------------------------------------------------------
# RSI reset thresholds — re-entry filter after losing trades
# ---------------------------------------------------------------------------
# After a losing trade, RSI must reset past these levels (with momentum
# continuing in the right direction) before re-entry is allowed in the
# same direction. Prevents re-firing the same weak signal that just failed.

RSI_RESET_SHORT = 45   # after losing SHORT: RSI must drop below this AND still falling
RSI_RESET_LONG  = 55   # after losing LONG:  RSI must rise above this AND still rising