INITIAL_CAPITAL = 10000

COINS = [
    "BTC-USD",
    "ETH-USD",
    "XRP-USD",
    "BNB-USD",
    "DOGE-USD",
    "LINK-USD",
    "LTC-USD"
]

# ---------------------------------------------------------------------------
# Core risk constants
# ---------------------------------------------------------------------------

STOP_LOSS_PCT         = 0.04   # 4% adverse move closes the full position
MAX_ALLOCATION        = 0.80   # never deploy more than 80% of cash at once
MAX_POSITIONS_PER_DIR = 2      # [TIER 3] relaxed: allow up to 2 LONGs and 2 SHORTs simultaneously
TIMEFRAME             = "1h"
RISK_PER_TRADE        = 0.10
DATA_DIR              = "data"

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