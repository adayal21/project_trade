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

# ---------------------------------------------------------------------------
# Coin universe
# ---------------------------------------------------------------------------
# Changes 2026-05-25:
#   Removed : ATOM/INR — ₹1.6L daily vol (too thin), consistent losses
#   Removed : FTM/INR  — corrupted candle data (30,892%/hr move artifact)
#   Added   : SOL/INR  — ₹1Cr+ daily vol, deeply liquid
#   Added   : XRP/INR  — ₹67L daily vol, well-behaved
#   Added   : DOGE/INR — ₹35L daily vol, solid movement
#   Added   : INJ/INR  — highest vol/liquidity ratio (1.37%/hr, ₹10L vol)
#   Added   : ADA/INR  — ₹14L daily vol, stable

COINS = [
    # -----------------------------------------------------------------------
    # Anchors — required regardless of trade-generation
    # -----------------------------------------------------------------------
    "BTC/INR",    # corr=1.00 — regime gate + tradeable

    # -----------------------------------------------------------------------
    # Diversification — independent narratives, different correlation bands
    # -----------------------------------------------------------------------
    "MANA/INR",   # corr=-0.40 — metaverse, own narrative
    "MATIC/INR",  # corr=+0.13 — Polygon L2, very independent
    "ETH/INR",    # corr=+0.22 — Layer 1, deep INR liquidity
    "ARB/INR",    # corr=+0.38 — 100% win rate in backtest (3/3)
    "XRP/INR",    # corr=+0.59 — ₹67L daily vol, liquid and well-behaved

    # -----------------------------------------------------------------------
    # Proven trade-generators — real winners in backtest data
    # -----------------------------------------------------------------------
    "NEAR/INR",   # corr=+0.60 — top coin, +₹40 total, 49% wins, +₹28.60 best
    "BNB/INR",    # corr=+0.70 — 53% win rate, near break-even
    "TRX/INR",    # corr=+0.72 — 35% wins, +₹6.95 best
    "FIL/INR",    # corr=+0.74 — 30% wins, +₹13.07 best
    "DOT/INR",    # corr=+0.75 — 40% wins, +₹12.44 best

    # -----------------------------------------------------------------------
    # New additions (2026-05-25) — passed volume + volatility check
    # -----------------------------------------------------------------------
    "SOL/INR",    # corr=+0.76 — ₹1Cr+ daily vol, deeply liquid
    "DOGE/INR",   # corr=+0.72 — ₹35L daily vol, solid movement
    "INJ/INR",    # corr=+0.65 — highest vol/liquidity ratio (1.37%/hr, ₹10L vol)
    "ADA/INR",    # corr=+0.70 — ₹14L daily vol, stable

    # -----------------------------------------------------------------------
    # Removed coins (kept as comments for reference)
    # -----------------------------------------------------------------------
    # "FTM/INR"  — removed 2026-05-25: corrupted candle data
    # "ATOM/INR" — removed 2026-05-25: too thin volume, consistent losses
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

# Regime-aware position limits:
#   BTC LONG    → 8 positions (bull market, deploy more)
#   BTC NEUTRAL → 6 positions (uncertain, maintain current exposure)
#   BTC SHORT   → 3 positions (bear market, stay mostly cash)
# The capital guard (MAX_ALLOCATION=80%) is the hard backstop regardless.
MAX_POSITIONS_PER_DIR = 6      # default / neutral regime
MAX_POSITIONS_BULL    = 8      # used when btc_regime == "LONG"
MAX_POSITIONS_BEAR    = 3      # used when btc_regime == "SHORT"
TIMEFRAME             = "1h"

# Score-based allocation — higher conviction gets more capital:
#   4/4 score : 10% of cash  (strongest signal)
#   3/4 score : 8%  of cash  (standard)
#   2/4 score : 5%  of cash  (minimum entry, least capital)
# RISK_PER_TRADE is the fallback if score is missing.
RISK_PER_TRADE        = 0.08   # fallback / 3/4 default
RISK_PER_TRADE_HIGH   = 0.10   # 4/4 score
RISK_PER_TRADE_LOW    = 0.05   # 2/4 score
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

ADX_THRESHOLD       = 25.0   # min ADX for a market to count as trending
ATR_EXPANSION_RATIO = 0.50   # ATR must be >= 50% of its 20-bar SMA
                              # (skips entries during volatility compression)

# ---------------------------------------------------------------------------
# Tier 1 — Two-stage partial profit-taking (scale-out at two targets)
# ---------------------------------------------------------------------------
# Based on backtest data: NEAR-style moves often ran from +3% to +6%, and
# many "stuck profit" exits happened at +0.5%-+2% where a single partial at
# +3% never fired. Two-stage capture solves both:
#
#   Tier 1A: close 25% at +2.0%   — lock in a small profit even on weak moves
#   Tier 1B: close 25% at +3.0%   — main partial, original behaviour
#   Remaining 50% trails with effective_trail (3% in BULL, 2% otherwise)
#
# Counter-trend trades skip Tier 1A and use a single partial at +1.5%
# (COUNTER_TREND_TP_PCT below) on 50% — bounces don't have room for
# two-stage scale-out.

PARTIAL_TP_1A_PCT       = 0.020   # Tier 1A: close 25% at +2%
PARTIAL_TP_1B_PCT       = 0.030   # Tier 1B: close 25% at +3% (was the only partial)
PARTIAL_EXIT_RATIO_1A   = 0.25    # fraction of original position closed at Tier 1A
PARTIAL_EXIT_RATIO_1B   = 0.25    # fraction of original position closed at Tier 1B
# Legacy aliases kept so any unchanged caller still works:
PARTIAL_TAKE_PROFIT_PCT = PARTIAL_TP_1B_PCT
PARTIAL_EXIT_RATIO      = 0.50    # combined Tier 1A+1B = 50% of original

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
# 4A  Stagnant : hours_held >= TIME_EXIT_STAGNANT_HOURS  AND |move| < 0.3%
# 4D  Losing   : hours_held >= TIME_EXIT_LOSING_HOURS    AND move < 0%
# 4B  Stuck+   : hours_held >= TIME_EXIT_EXTENDED_HOURS  AND 0 < move < TP
# 4C  Trail TO : Tier 1 fired AND hours_held >= TIME_EXIT_TRAIL_HOURS
#
# Philosophy: capital should never sit idle. If a trade isn't moving toward
# profit within a tight window, the thesis is wrong — free up the slot.
# Maximum any trade can stay alive: 2h (4B stuck profitable, worst case).

TIME_EXIT_STAGNANT_HOURS      = 2    # 4A: NEUTRAL/BEAR — market not supporting move, cut at 2h
TIME_EXIT_STAGNANT_HOURS_BULL = 2    # 4A: BTC LONG — same, 2h across the board
TIME_EXIT_LOSING_HOURS        = 2    # 4D: cut losing position after 2h — thesis failed
TIME_EXIT_LOSING_HOURS_BEAR   = 2    # 4D: same in neutral/bear — no reason to hold longer
TIME_EXIT_MIN_MOVE_PCT        = 0.003 # 4A threshold: |move| < 0.3% = stagnant
TIME_EXIT_EXTENDED_HOURS      = 2    # 4B: stuck profitable but no TP — exit at 2h, take what you have
TIME_EXIT_TRAIL_HOURS         = 2    # 4C: trailing remainder — close 2h after Tier 1, don't wait indefinitely

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
# Set to 3 — requires three independent confirmations.

LONG_SOFT_REQUIRED = 3

# Minimum soft score required for counter-trend entries.
# CT trades are below EMA200 — they need stronger confirmation than trend entries
# to compensate for trading against the macro direction.
# Set higher than LONG_SOFT_REQUIRED to filter out the weakest CT signals.
CT_SOFT_REQUIRED = 4   # CT entries need 4/4 vs 2/4 for trend entries

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
# Coins excluded from entry (still fetched for regime/data purposes)
# ---------------------------------------------------------------------------
# BTC/INR is required in COINS for the regime gate (get_btc_regime reads
# coins_data["BTC/INR"]). But at ₹10k capital the position size is too
# tiny to be meaningful — 8% = ₹800 buys ~0.0001 BTC, spread eats the edge.
# List coins here to fetch their data but never open positions in them.

NO_TRADE_COINS = {"BTC/INR"}

# ---------------------------------------------------------------------------
# Re-entry price move filter (post stagnant/losing exit)
# ---------------------------------------------------------------------------
# After a TIME_EXIT_STAGNANT or TIME_EXIT_LOSING exit, require price to move
# at least this % in the signal direction before re-entry is allowed.
# Prevents the "exit flat coin → immediately re-enter same flat coin" loop.
# RSI reset alone is not enough — price must show actual directional movement.

REENTRY_MIN_MOVE_PCT = 0.005   # 0.5% move required from last exit price

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
    "INJ/INR":   0.650,
    "VET/INR":   0.647,
    "SHIB/INR":  0.648,
    "GALA/INR":  0.698,
    "BNB/INR":   0.699,
    "SUSHI/INR": 0.699,
    # Band 3
    "ALGO/INR":  0.717,
    "TRX/INR":   0.723,
    "DOGE/INR":  0.720,
    "ADA/INR":   0.700,
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

# ---------------------------------------------------------------------------
# Regime flip emergency exit
# ---------------------------------------------------------------------------
# When BTC regime transitions from LONG → SHORT, positions held in high-corr
# coins that are currently losing or flat are exited immediately rather than
# waiting for individual tier exits. Profitable positions with active trailing
# stops are left to trail out naturally.
#
# Correlation threshold for emergency exit on regime flip:
#   corr > REGIME_FLIP_EXIT_CORR  → exit immediately if losing or flat
#   corr <= REGIME_FLIP_EXIT_CORR → let individual tiers handle it (coin is independent)
REGIME_FLIP_EXIT       = True    # enable emergency exit on LONG→SHORT flip
REGIME_FLIP_EXIT_CORR  = 0.65   # coins with corr above this get force-exited on flip
# Previous regime is stored per-run to detect transitions.
# Written to data/btc_regime_prev.txt each run.

# ---------------------------------------------------------------------------
# CT entries in BTC SHORT regime — correlation-gated block
# ---------------------------------------------------------------------------
# Counter-trend entries (below own EMA200) in a BTC SHORT regime are blocked
# for coins that closely track BTC. These coins move with BTC — buying a bounce
# when BTC is falling is like trying to catch a falling knife.
#
# Low-corr coins (DENT, FTM, MANA etc.) are genuinely independent and can still
# CT-enter in BTC SHORT based on their own signal strength.
#
# Coins with corr > CT_BLOCK_CORR_IN_SHORT cannot open CT positions in BTC SHORT.
CT_BLOCK_CORR_IN_SHORT = 0.65   # block CT entries for corr > this in BTC SHORT

# ---------------------------------------------------------------------------
# 4H Per-coin Hard Entry Gate
# ---------------------------------------------------------------------------
# Previously: 4H BEAR direction on a coin just flagged the trade as
# counter-trend (tighter TP/trail).
# Problem: DENT had 5 stop-losses because 1H signals fired bullish bounces
# while 4H was clearly bearish. The CT-flag was not enough protection.
#
# Now: if 4H Supertrend on the coin is BEAR, block the entry entirely.
# This is the right filter at the right timeframe — the 4H reflects the
# coin's actual multi-hour structure, not a 1H bounce inside a downtrend.
#
# Set to False to revert to the old CT-flag-only behaviour.
USE_4H_HARD_GATE = True   # block entry when 4H direction on coin is BEAR