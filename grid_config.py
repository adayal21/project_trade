# =============================================================================
# grid_config.py — Grid Trading Bot Configuration
# =============================================================================
# Completely separate from HMA + Ichimoku bot.
# Runs on same 4H cron, separate capital, separate positions.
#
# Coins selected: top 7 from backtest (Return > 0, Sharpe > 0.3)
# Excluded: ADA (-0.8%), BNB (-2.8%), POL (-6.3%), AVAX (+1.4% marginal)
# =============================================================================

# ---------------------------------------------------------------------------
# Grid coin universe — CoinDCX format
# ---------------------------------------------------------------------------
GRID_COINS = [
    "ZEC/USDT",    # +33.3% | Sharpe 0.77 | Best performer
    "XRP/USDT",    # +27.2% | Sharpe 0.52
    "LINK/USDT",   # +26.4% | Sharpe 0.49
    "JASMY/USDT",  # +24.8% | Sharpe 0.54
    "SOL/USDT",    # +24.7% | Sharpe 0.54
    "ETH/USDT",    # +13.5% | Sharpe 0.35
    "BTC/USDT",    # +9.0%  | Sharpe 0.27 | Most stable
]

# ---------------------------------------------------------------------------
# Grid parameters — DO NOT CHANGE (validated in backtest)
# ---------------------------------------------------------------------------
GRID_LEVELS        = 20       # grid levels per coin
GRID_RANGE_BARS    = 168      # bars to look back — 168 x 1H = 7 days (1 week range)
GRID_STOP_PCT      = 0.15     # 15% stop loss per grid position
GRID_MAX_OPEN      = 10       # max open grid positions per coin
GRID_ALLOCATION    = 0.10     # 10% of grid capital per coin

# ---------------------------------------------------------------------------
# Grid capital — completely separate from HMA/Ichimoku capital
# ---------------------------------------------------------------------------
GRID_INITIAL_CAPITAL = 10_000.0   # paper: virtual $10k for grid bot

# ---------------------------------------------------------------------------
# Data directory — uses same data dir but distinct filenames
# ---------------------------------------------------------------------------
GRID_DATA_DIR = "data/grid"       # all grid files go here