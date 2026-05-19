# ---------------------------------------------------------------------------
# Environment — load .env file if present (local dev and GCP server)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # python-dotenv not installed — env vars must be set manually

# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------
# "paper" → simulate trades in CSV files, no real orders placed (default)
# "live"  → connect to CoinDCX account, place real market orders
#
# Switch by changing this one value. Everything else is automatic.
# Run connectivity_test.py first to verify your API keys work before
# switching to live.

import os
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")   # override via env var too

# ---------------------------------------------------------------------------
# Initial capital
# ---------------------------------------------------------------------------
# Paper mode : always ₹10,000 simulated
# Live mode  : read from LIVE_INITIAL_CAPITAL env var (default ₹1,000)
#
# To change live capital, update your .env file:
#   LIVE_INITIAL_CAPITAL=5000
# No code change needed — just restart the bot.

if TRADING_MODE == "live":
    INITIAL_CAPITAL = float(os.environ.get("LIVE_INITIAL_CAPITAL", "1000"))
else:
    INITIAL_CAPITAL = 10000

COINS = [
    # Regime reference — never traded, used only for BTC regime gate
    "BTC/INR",

    # Tier 2 — low-to-moderate BTC correlation, good CoinDCX volume
    "ETH/INR",    # corr=0.22 — most independent of BTC on INR markets
    "XRP/INR",    # corr=0.59 — massive INR volume, moves on own news
    "ATOM/INR",   # corr=0.48 — Cosmos ecosystem, own narrative
    "SHIB/INR",   # corr=0.66 — meme dynamics, very high volume

    # Tier 3 — moderate BTC correlation, diverse ecosystems
    "BNB/INR",    # corr=0.70 — exchange token dynamics
    "TRX/INR",    # corr=0.73 — Tron ecosystem, excellent volume
    "SOL/INR",    # corr=0.76 — Solana ecosystem plays
    "DOT/INR",    # corr=0.75 — Polkadot parachain narrative
    "AVAX/INR",   # corr=0.77 — Avalanche DeFi ecosystem
    "UNI/INR",    # corr=0.78 — DeFi token, own protocol narrative
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
MAX_POSITIONS_PER_DIR = 5      # allow up to 5 LONGs simultaneously (10 coins, max 50% in trades)
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

# ---------------------------------------------------------------------------
# Trading mode flags
# ---------------------------------------------------------------------------
# LONG_ONLY = True  → only LONG signals are generated and entered.
#                     SHORT signals are suppressed entirely. Intended for
#                     spot trading where selling short is not possible.
# LONG_ONLY = False → both LONG and SHORT signals are active (default for
#                     margin/futures paper trading).

LONG_ONLY = True

# When LONG_ONLY is True, the BTC regime gate is relaxed:
#   - BTC regime SHORT  → LONG entries blocked (market is falling, don't buy)
#   - BTC regime NEUTRAL → LONG entries ALLOWED (uncertain market, cautious ok)
#   - BTC regime LONG   → LONG entries allowed (market is rising, ideal)
#
# Set to False to use the strict original behaviour (NEUTRAL blocks everything).

REGIME_ALLOWS_LONG_IN_NEUTRAL = True

# ---------------------------------------------------------------------------
# Counter-trend LONG settings
# ---------------------------------------------------------------------------
# When price is BELOW EMA200 (bearish macro) but Supertrend flips bullish
# locally, the strategy can still enter a LONG — but uses tighter targets
# since we are trading a bounce against the primary trend, not a full trend.
#
# Normal trend LONG  (above EMA200): TP=3.0%, trail=2.0%
# Counter-trend LONG (below EMA200): TP=1.5%, trail=1.0%
#
# The signal scoring system (need 2 of 4 soft conditions) determines whether
# an entry fires at all. EMA200 is one of the four soft conditions — not a
# hard gate anymore.

COUNTER_TREND_TP_PCT    = 0.015   # tighter TP for bear-market bounces
COUNTER_TREND_TRAIL_PCT = 0.010   # tighter trail for bear-market bounces

# Soft condition score threshold for LONG entry.
# 4 soft conditions scored: RSI>52, Volume>Baseline, Close>EMA200, Close>EMA50
# Need LONG_SOFT_REQUIRED of 4 to enter.
# Set to 2 — requires two independent confirmations.

LONG_SOFT_REQUIRED = 2

# ---------------------------------------------------------------------------
# BTC regime override — high-conviction independent LONGs
# ---------------------------------------------------------------------------
# When BTC regime is SHORT, LONGs are normally blocked. However, if a coin's
# own indicators score REGIME_OVERRIDE_MIN_SCORE or higher (out of 4) AND
# the trade is NOT counter-trend (coin is above its own EMA200), the entry
# is allowed despite the BTC SHORT regime.
#
# Rationale: a coin scoring 3/4 or 4/4 with Supertrend bullish and above its
# own EMA200 is showing genuine independent momentum. At 0.73 correlation,
# TRX for example can trend up while BTC is flat/down.
#
# Set to 5 to effectively disable this override (no score can reach 5/4).
# Set to 3 to allow 3/4 or 4/4 independent LONGs through in BTC SHORT regime.

REGIME_OVERRIDE_MIN_SCORE = 3