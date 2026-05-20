# Crypto Trading Bot — CoinDCX INR

A multi-timeframe momentum trading bot for Indian INR crypto pairs on CoinDCX. Built for paper trading validation before live deployment.

---

## Architecture

### Data Sources
| Timeframe | Purpose |
|---|---|
| 1H candles | Core signal generation — EMA200, EMA50, RSI, ADX, ATR, Supertrend, Volume |
| 15-min candles | Entry timing gate + counter-trend exit monitoring |
| 4H candles | Per-coin macro direction (BULL/BEAR/NEUTRAL) |

All data from `https://public.coindcx.com/market_data/candles` (no auth needed).

### Trading Mode
Set in `.env` or `config.py`:
- `"paper"` — simulates trades in CSV files, no real orders (default)
- `"live"` — connects to CoinDCX, places real market orders

---

## Coin Universe (25 tradeable + BTC regime reference)

| Band | Coins | BTC Correlation |
|---|---|---|
| Negative / near-zero | FTM, ENJ, DENT, MANA, SAND, XLM, CHZ, MATIC, ETH, HOT | -0.65 to +0.33 |
| Low-moderate | ARB, ATOM, AAVE, XRP, NEAR, VET, SHIB, GALA, BNB, SUSHI | +0.38 to +0.70 |
| Moderate, diverse sectors | ALGO, TRX, FIL, DOT, SOL | +0.72 to +0.76 |

BTC/INR is both the regime gate AND tradeable. Blocked from regime override when BTC itself is in SHORT regime (correlation = 1.0 with itself).

Coins with BTC correlation > 0.75 are blocked from the regime override in BTC SHORT — they follow BTC too closely to trade independently in a bear market.

---

## Strategy

### Signal Generation (1H candles)

**Hard filters — both must pass:**
- ADX > 18 (trending market, not choppy)
- ATR >= 50% of 20-bar ATR average (volatility expanding, not compressing)

**Mandatory condition:**
- 1H Supertrend must be bullish

**Soft scoring — need 2 of 4:**
| | Condition | Label |
|---|---|---|
| S1 | RSI > 52 | Momentum |
| S2 | Volume > 20-bar median | Participation |
| S3 | Close > EMA200 | Macro trend |
| S4 | Close > EMA50 | Local trend |

Score 4/4 = strongest signal. Score 2/4 = minimum to enter.

If S3 fails (price below EMA200) → **counter-trend trade** — tighter targets applied.
If 4H direction is BEAR → also flagged as counter-trend regardless of S3.

### Entry Gates (checked in order)

| Gate | Check |
|---|---|
| 1 | Position is flat (no open position on this coin) |
| 1.5 | RSI reset filter — after a losing trade, RSI must reset past 45/55 |
| 2 | BTC regime filter — SHORT regime blocks entries (with override for score ≥3/4, low-corr coins) |
| 3 | Max 6 simultaneous LONGs, max 80% capital deployed |
| 4 | 15-min RSI must be > 50 and rising at entry time |

All qualifying candidates are **ranked by score then ADX** before Gate 3. Highest conviction enters first regardless of list position.

### Exit Logic (fires in this order every 15-min run)

**Tier 5 — Signal deterioration (fastest exit)**
If 1H signal score drops to ≤1/4 → exit immediately. Applies to all positions whether profitable or losing, whether pre or post Tier 1. No waiting.

**Tier 0 — Counter-trend 15-min reversal**
For counter-trend positions only: if 2 consecutive 15-min bars are falling → exit before reversal eats the 1.5% target.

**Tier 3 — Hard stop-loss**
Exit at -4% from entry. Catastrophe brake.

**Tier 1 — Partial profit-taking**
- Trend trades: sell 50% at +3.0%
- Counter-trend trades: sell 50% at +1.5%

**Tier 2 — Trailing stop on remaining 50%**
Follows price upward, exits if price falls back:
- BTC LONG regime + trend trade: 3% below peak (wider, captures bigger bull moves)
- BTC SHORT/NEUTRAL + trend trade: 2% below peak
- Counter-trend: 1% below peak

**Tier 4 — Time-based backstops (hour-based, not bar-based)**
| Sub-tier | Condition | Action |
|---|---|---|
| 4A | 6h held, move < 0.5% either way | Exit — stagnant, free up capital |
| 4D | 4h held, still losing | Exit — thesis failed |
| 4B | 12h held, profitable but below TP | Exit — take small gain |
| 4C | 4h after Tier 1 fired | Exit remaining half — don't wait indefinitely |

---

## Key Configuration (`config.py`)

```python
# Capital
INITIAL_CAPITAL         = 10000   # paper mode (₹)
RISK_PER_TRADE          = 0.08    # 8% of cash per trade
MAX_POSITIONS_PER_DIR   = 6       # max 6 simultaneous LONGs
MAX_ALLOCATION          = 0.80    # never deploy more than 80% of capital
STOP_LOSS_PCT           = 0.040   # hard stop at -4%

# Profit targets
PARTIAL_TAKE_PROFIT_PCT = 0.030   # Tier 1 at +3% (trend)
COUNTER_TREND_TP_PCT    = 0.015   # Tier 1 at +1.5% (counter-trend)

# Trailing stops
BULL_TRAILING_STOP_PCT  = 0.030   # 3% trail in BTC LONG regime
TRAILING_STOP_PCT       = 0.020   # 2% trail in BTC SHORT/NEUTRAL
COUNTER_TREND_TRAIL_PCT = 0.010   # 1% trail for counter-trend trades

# Time exits (hours)
TIME_EXIT_LOSING_HOURS    = 4     # cut losing positions after 4h
TIME_EXIT_STAGNANT_HOURS  = 6     # cut flat positions after 6h
TIME_EXIT_EXTENDED_HOURS  = 12    # cut stuck-profitable after 12h
TIME_EXIT_TRAIL_HOURS     = 4     # exit trailing half after 4h post Tier 1

# Signal
SIGNAL_EXIT_THRESHOLD     = 1     # Tier 5 fires at score ≤1/4
REGIME_OVERRIDE_MIN_SCORE = 3     # min score to override BTC SHORT regime
REGIME_OVERRIDE_MAX_CORR  = 0.75  # max BTC corr allowed to override

# Multi-timeframe
CONFIRM_15MIN             = True  # 15-min RSI gate at entry
USE_4H_REGIME             = True  # 4H macro direction per coin
CT_EXIT_15MIN             = True  # 15-min counter-trend exit monitoring

# Logging
VERBOSE_DIAG              = False # set True for detailed per-coin indicator logs
```

### Live trading capital
Set in `.env`:
```
LIVE_INITIAL_CAPITAL=1000
```

---

## Installation

```bash
pip install -r requirements.txt
```

Requirements: `pandas`, `numpy`, `requests`, `ta`, `python-dotenv`

---

## Running

### Paper trading (default)
```bash
python main.py
```

### Cron job — every 15 minutes
```
*/15 * * * * /path/to/python /path/to/main.py >> /path/to/logs/live.log 2>&1
```

### Before going live — connectivity test
```bash
python connectivity_test.py
```
Safe to run anytime. Verifies API keys, reads balance, dry-run order test. No real orders placed.

---

## Environment Variables (`.env` file)

```
COINDCX_API_KEY=your_api_key
COINDCX_API_SECRET=your_api_secret
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TRADING_MODE=paper
LIVE_INITIAL_CAPITAL=1000
```

**Never commit `.env` to git.**

---

## Switching to Live Trading

1. Deposit INR to CoinDCX (minimum ₹500)
2. Run `python connectivity_test.py` — confirm balance shows
3. Set `TRADING_MODE=live` in `.env`
4. Run one cycle manually: `python main.py`
5. Verify first trade log shows live order placement
6. Enable cron job

---

## Data Files (`data/` directory)

| File | Contents |
|---|---|
| `portfolio.csv` | Equity curve — cash, P&L, open positions per run |
| `{COIN}_position.csv` | Current open position (deleted on close) |
| `{COIN}_trades.csv` | Full trade history: entry, exit, P&L, exit reason |

**Never delete CSVs in live mode** — they are the position state. In paper mode, deleting `data/*.csv` gives a fresh start.

---

## Telegram Notifications

`send_telegram.py` sends portfolio snapshot + open positions after each run.
Currently disabled — enable when moving to live trading by adding to cron:
```
*/15 * * * * /path/to/python /path/to/send_telegram.py
```

---

## Indian Tax Notes

- **30% flat tax** on all crypto gains (no deductions)
- **1% TDS** deducted per transaction automatically by CoinDCX
- No loss offset between trades
- Consult a CA before going live — the 1% TDS compounds across every trade

---

## File Reference

| File | Purpose |
|---|---|
| `main.py` | Core bot loop — signal evaluation, entry, exit, portfolio tracking |
| `strategy.py` | Indicator calculations, signal generation, 15-min/4H functions |
| `config.py` | All constants and settings |
| `portfolio.py` | Portfolio CSV logging |
| `exchange.py` | CoinDCX live order execution layer |
| `coindcx_auth.py` | HMAC-SHA256 signing for authenticated API calls |
| `connectivity_test.py` | Pre-live API verification script |
| `send_telegram.py` | Telegram notification script |
| `verify_coins.py` | Verify all coins are available on CoinDCX with sufficient data |
| `find_replacement.py` | Find replacement coins by scanning CoinDCX for correlation/volume |
| `test.py` | BTC correlation and volume study for coin selection |