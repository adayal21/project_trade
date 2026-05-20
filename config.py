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
    # -----------------------------------------------------------------------
    # BTC/INR — regime gate AND tradeable.
    # Blocked from override in BTC SHORT regime (corr=1.0 with itself).
    # -----------------------------------------------------------------------
    "BTC/INR",

    # -----------------------------------------------------------------------
    # Band 1 — negative / near-zero BTC correlation (most independent)
    # These move against BTC or on their own narrative entirely
    # -----------------------------------------------------------------------
    "FTM/INR",    # corr=-0.65 — Fantom L1, strongest negative corr available
    "ENJ/INR",    # corr=-0.49 — Gaming NFT / Enjin ecosystem
    "DENT/INR",   # corr=-0.43 — Telecom data token, strongly decorrelated
    "MANA/INR",   # corr=-0.40 — Metaverse / Decentraland, own narrative
    "SAND/INR",   # corr=-0.39 — Metaverse / gaming, own narrative
    "XLM/INR",    # corr=-0.22 — Stellar payments, negative correlation
    "CHZ/INR",    # corr=-0.17 — Sports/fan tokens, event-driven
    "MATIC/INR",  # corr=+0.13 — Polygon L2, most independent L2 available
    "ETH/INR",    # corr=+0.22 — Layer 1, most independent on INR markets
    "HOT/INR",    # corr=+0.33 — Holochain/Web3, own ecosystem, high volume

    # -----------------------------------------------------------------------
    # Band 2 — low-moderate BTC correlation (moves partly independently)
    # -----------------------------------------------------------------------
    "ARB/INR",    # corr=+0.38 — Arbitrum L2, Ethereum ecosystem narrative
    "ATOM/INR",   # corr=+0.47 — Cosmos IBC, proven independent (live trade hit)
    "AAVE/INR",   # corr=+0.48 — DeFi lending, protocol-driven narrative
    "XRP/INR",    # corr=+0.59 — Payments/remittance, regulatory news driven
    "NEAR/INR",   # corr=+0.60 — Layer 1, own developer ecosystem
    "VET/INR",    # corr=+0.65 — Supply chain, enterprise partnerships driven
    "SHIB/INR",   # corr=+0.65 — Meme, community/social driven, huge volume
    "GALA/INR",   # corr=+0.70 — Gaming token, own game launches
    "BNB/INR",    # corr=+0.70 — Exchange token, own exchange dynamics
    "SUSHI/INR",  # corr=+0.70 — DEX protocol, DeFi narrative

    # -----------------------------------------------------------------------
    # Band 3 — moderate BTC correlation, diverse sectors
    # -----------------------------------------------------------------------
    "ALGO/INR",   # corr=+0.72 — Layer 1 / payments, institutional focus
    "TRX/INR",    # corr=+0.72 — Layer 1 / content, TRON ecosystem
    "FIL/INR",    # corr=+0.74 — Decentralised storage, independent demand
    "DOT/INR",    # corr=+0.75 — Parachain, own governance narrative
    "SOL/INR",    # corr=+0.76 — Solana ecosystem, own developer community
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
MAX_POSITIONS_PER_DIR = 6      # allow up to 6 LONGs simultaneously
TIMEFRAME             = "1h"
RISK_PER_TRADE        = 0.08
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
# Tier 4 — Time-based exit backstop (HOUR-based, not bar-based)
# ---------------------------------------------------------------------------
# Using hours instead of bars makes exits timeframe-agnostic — correct
# whether the cron runs every 15 minutes or every hour.
#
# 4A  Stagnant : hours_held >= TIME_EXIT_STAGNANT_HOURS  AND |move| < 0.5%
# 4D  Losing   : hours_held >= TIME_EXIT_LOSING_HOURS    AND move < 0%
# 4B  Stuck+   : hours_held >= TIME_EXIT_EXTENDED_HOURS  AND 0 < move < TP
# 4C  Trail TO : Tier 1 fired AND hours_held >= TIME_EXIT_TRAIL_HOURS

TIME_EXIT_STAGNANT_HOURS  = 6    # 4A: cut stagnant position after 6h
TIME_EXIT_LOSING_HOURS    = 4    # 4D: cut losing position after 4h
TIME_EXIT_MIN_MOVE_PCT    = 0.005 # 4A threshold: |move| < 0.5% = stagnant
TIME_EXIT_EXTENDED_HOURS  = 12   # 4B: exit stuck-profitable after 12h
TIME_EXIT_TRAIL_HOURS     = 10   # 4C: close trailing half 10h after Tier 1

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
# ---------------------------------------------------------------------------
# Multi-timeframe architecture
# ---------------------------------------------------------------------------
# Three candle layers — each serves a different purpose:
#
#   4H candles  → Macro direction per coin. Replaces EMA200-only S3 check.
#                 4H Supertrend bullish = coin is in a genuine multi-hour uptrend.
#                 4H bearish = counter-trend trade, use tighter TP/trail.
#
#   1H candles  → Core signal. EMA200, EMA50, RSI, ADX, ATR, Supertrend, Volume.
#                 Primary scoring system (need 2 of 4 soft conditions).
#
#   15-min candles → Entry timing + counter-trend exit monitoring.
#                 At entry: 15-min RSI must be > 50 and rising.
#                 During hold (counter-trend only): if 15-min Supertrend flips
#                 bearish for 2 consecutive bars → early exit before full reversal.

CONFIRM_15MIN         = True    # gate 15-min momentum check at entry
USE_4H_REGIME         = True    # use 4H candles for per-coin macro direction

# Counter-trend early exit via 15-min monitoring
CT_EXIT_15MIN         = True    # monitor counter-trend positions every 15-min run
CT_EXIT_CONSEC_BARS   = 2       # consecutive 15-min bearish bars before early exit

# 15-min RSI entry gate
MIN_15MIN_RSI         = 50      # 15-min RSI must be above this at entry
REQUIRE_15MIN_RSI_RISING = True  # 15-min RSI must also be rising bar-over-bar
# ---------------------------------------------------------------------------
# Diagnostic output switch
# ---------------------------------------------------------------------------
# When True: prints [DIAG], [VOL DIAG], [CT-monitor], [15min] lines every run.
# When False: only prints entries, exits, signals, and portfolio snapshot.
# Default OFF — keeps live.log small when running every 15 minutes.
# Set True temporarily when debugging signal behaviour.

VERBOSE_DIAG = False
# ---------------------------------------------------------------------------
# Correlation-based regime override block
# ---------------------------------------------------------------------------
# Coins with BTC correlation above this threshold are blocked from the regime
# override in BTC SHORT regime. High-correlation coins essentially move with
# BTC — buying them when BTC is falling is the same risk as buying BTC itself.
# BTC itself (corr=1.0) is always blocked from override in SHORT regime.
#
# Coins with corr <= threshold CAN override (genuinely independent momentum).
# Coins with corr >  threshold CANNOT override (too correlated to BTC).

REGIME_OVERRIDE_MAX_CORR = 0.75   # block override for corr > 0.75 in BTC SHORT

# Correlation map — used at entry to check override eligibility
# Values from 720-bar CoinDCX correlation study
COIN_BTC_CORR = {
    # Band 1 — negative / near-zero
    "BTC/INR":   1.000,
    "FTM/INR":  -0.650,
    "ENJ/INR":  -0.494,
    "DENT/INR": -0.430,
    "MANA/INR": -0.400,
    "SAND/INR": -0.390,
    "XLM/INR":  -0.221,
    "CHZ/INR":  -0.165,
    "MATIC/INR":  0.130,
    "ETH/INR":   0.215,
    "HOT/INR":   0.334,
    # Band 2
    "ARB/INR":   0.384,
    "ATOM/INR":  0.470,
    "AAVE/INR":  0.480,
    "XRP/INR":   0.589,
    "NEAR/INR":  0.604,
    "VET/INR":   0.647,
    "SHIB/INR":  0.648,
    "GALA/INR":  0.698,
    "BNB/INR":   0.699,
    "SUSHI/INR": 0.699,
    # Band 3
    "ALGO/INR":  0.717,
    "TRX/INR":   0.723,
    "FIL/INR":   0.741,
    "DOT/INR":   0.750,
    "SOL/INR":   0.764,
}

# ---------------------------------------------------------------------------
# Signal deterioration exit (Tier 5)
# ---------------------------------------------------------------------------
# If a held position's 1H signal score drops to SIGNAL_EXIT_THRESHOLD or below
# on the current run, exit immediately — the thesis that caused entry has
# reversed. No need to wait for the time-based exits.
#
# Entry required score: 2/4 (LONG_SOFT_REQUIRED)
# Exit trigger: score <= 1/4 — only 1 or 0 conditions still agree
#
# Set to -1 to disable this exit entirely.

SIGNAL_DETERIORATION_EXIT = True
SIGNAL_EXIT_THRESHOLD     = 1    # exit if score drops to 1 or below (was 2+ at entry)
# ---------------------------------------------------------------------------
# Regime-aware trailing stop
# ---------------------------------------------------------------------------
# In a bull market (BTC LONG), price trends further and normal pullbacks
# are deeper before resuming. A wider trail gives positions room to breathe.
#
# BTC LONG regime  : 3% trail — more room to run, captures bigger moves
# BTC SHORT/NEUTRAL: 2% trail — tighter, protects capital in weak markets
#
# Counter-trend trades always use COUNTER_TREND_TRAIL_PCT (1%) regardless.

BULL_TRAILING_STOP_PCT = 0.030   # 3% trail when BTC regime is LONG