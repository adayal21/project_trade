
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
# Constants
# ---------------------------------------------------------------------------

STOP_LOSS_PCT         = 0.04   # 4% adverse move closes the position
MAX_ALLOCATION        = 0.80   # never deploy more than 80% of cash at once
MAX_POSITIONS_PER_DIR = 1      # correlation guard: max 1 LONG and 1 SHORT open at once
TIMEFRAME             = "1h"
RISK_PER_TRADE        = 0.10
DATA_DIR              = "data"
