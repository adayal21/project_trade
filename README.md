# Crypto Trading Bot — CoinDCX INR

A multi-timeframe momentum trading bot for Indian INR crypto pairs, built for paper trading validation before live deployment on CoinDCX.

---

## Architecture

### Data Sources
| Timeframe | Purpose | Source |
|---|---|---|
| 1H candles | Core signal generation | CoinDCX public API (no auth) |
| 15-min candles | Entry timing + counter-trend exit | CoinDCX public API (no auth) |
| 4H candles | Per-coin macro direction | CoinDCX public API (no auth) |

All candle data is fetched from `https://public.coindcx.com/market_data/candles`. No authentication required for market data. Execution (live mode only) uses the authenticated CoinDCX trading API.

### Trading Mode
Controlled by a single constant in `config.py` or the `TRADING_MODE` environment variable:
- `"paper"` — simulates trades in CSV files, no real orders placed (default)
- `"live"` — connects to CoinDCX account, places real market orders

---

## Strategy

### Signal Generation (1H)

**Hard filters — both must pass:**
| Filter | Condition |
|---|---|
| ADX regime | ADX > 18 (trending market) |
| ATR expansion | ATR >= 50% of 20-bar ATR SMA |

**Mandatory condition:**
- Supertrend must be bullish

**Soft conditions — need 2 of 4:**
| | Condition |
|---|---|
| S1 | RSI > 52 |
| S2 | Volume > 20-bar median volume |
| S3 | Close > EMA200 (macro trend) |
| S4 | Close > EMA50 (local trend) |

If S3 is not satisfied → trade flagged as **counter-trend** (bounce against macro). Counter-trend trades use tighter TP and trailing stop targets.

### Entry Gates (in order)

| Gate | Check |
|---|---|
| 1 | Position is flat (no open position on this coin) |
| 1.5 | RSI reset filter — after a losing trade, RSI must reset past 45/55 before re-entry |
| 2 | BTC regime filter — blocks entries if BTC macro is confirmed SHORT (with override for high-conviction 3+/4 scores) |
| 3 | Position count ≤ MAX_POSITIONS_PER_DIR and capital guard ≤ MAX_ALLOCATION |
| 4 | 15-min momentum gate — 15-min RSI must be > 50 and rising at entry time |

All candidates passing Gates 1–2 are ranked by score (4/4 > 3/4 > 2/4) then ADX before Gate 3 is applied. Highest conviction always enters first.

### Exit Logic

**Tier 1 — Partial profit-taking:**
- Take 50% off the table at +3.0% (trend) or +1.5% (counter-trend)

**Tier 2 — Trailing stop:**
- Trail remaining 50% at 2.0% below high-water mark (trend) or 1.0% (counter-trend)
- Activates only after Tier 1 fires

**Tier 0 — Counter-trend 15-min exit:**
- For counter-trend positions only: if 2 consecutive 15-min bars are falling → exit immediately
- Catches bounce reversals before they erase the 1.5% target

**Tier 3 — Hard stop-loss:**
- Exit at -4% from entry (catastrophe brake)

**Tier 4 — Time-based exits (hour-based, timeframe-agnostic):**
| Sub-tier | Condition | Action |
|---|---|---|
| 4A Stagnant | 6h held, \|move\| < 0.5% | Exit — no momentum |
| 4D Losing | 4h held, move < 0% | Exit — thesis failed |
| 4B Stuck+ | 12h held, 0 < move < TP | Exit — take small gain |
| 4C Trail timeout | 10h after Tier 1 fired | Exit remaining half |

---

## Coin Universe

25 coins total. BTC/INR is the regime reference only (never traded).

| Band | Coins | BTC Correlation |
|---|---|---|
| Very low / negative | ETH, CHZ, DENT, HOT, ATOM, AAVE | -0.43 to +0.48 |
| Low-moderate | XRP, NEAR, VET, SHIB, WIN, GALA, BNB, SUSHI | +0.59 to +0.70 |
| Moderate, diverse sectors | ALGO, TRX, FIL, DOT, CRV, SOL, GRT, AVAX, SNX, UNI | +0.72 to +0.78 |

Correlation measured over 720 bars (30 days) of 1H CoinDCX data. Lower correlation = more independent movement from BTC.

---

## Configuration (`config.py`)

### Key constants

```python
INITIAL_CAPITAL       = 10000    # paper mode starting capital (₹)
RISK_PER_TRADE        = 0.10     # 10% of available cash per trade
MAX_POSITIONS_PER_DIR = 5        # max simultaneous LONG positions
STOP_LOSS_PCT         = 0.040    # hard stop at -4%
PARTIAL_TAKE_PROFIT_PCT = 0.030  # Tier 1 TP at +3% (trend)
COUNTER_TREND_TP_PCT  = 0.015    # Tier 1 TP at +1.5% (counter-trend)
LONG_ONLY             = True     # spot trading — no shorts
VERBOSE_DIAG          = False    # set True to enable detailed indicator logs
```

### Live trading capital

```
LIVE_INITIAL_CAPITAL=1000   # set in .env file
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

### Before going live — connectivity test
```bash
python connectivity_test.py
```
Safe to run anytime. Verifies API keys, reads balance, checks symbol formats. No real orders placed.

### Cron job (recommended: every 15 minutes)
```
*/15 * * * * /path/to/venv/bin/python /path/to/main.py >> /path/to/logs/live.log 2>&1
```

---

## Environment Variables

Create a `.env` file in the project root:

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

1. Deposit INR to your CoinDCX account (minimum ₹500)
2. Run `python connectivity_test.py` — confirm balance shows correctly
3. Set `TRADING_MODE=live` in `.env`
4. Run one cycle manually: `python main.py`
5. Verify the first trade log shows live order placement
6. Enable the 15-minute cron job

---

## Data Output (`data/` directory)

| File | Contents |
|---|---|
| `portfolio.csv` | Equity curve, cash, P&L snapshots per run |
| `{COIN}_position.csv` | Current open position (deleted on close) |
| `{COIN}_trades.csv` | Full trade history: entry, exit, P&L, exit reason |

---

## Telegram Notifications

`send_telegram.py` sends portfolio snapshot + open positions to a Telegram chat after each run. Add to cron after the main bot:

```
*/15 * * * * /path/to/python /path/to/main.py >> /path/to/logs/live.log 2>&1
*/15 * * * * /path/to/python /path/to/send_telegram.py
```

---

## Taxes (India)

- **30% flat tax** on all crypto gains
- **1% TDS** deducted per transaction by CoinDCX automatically
- No loss offset between trades
- Factor this into profitability expectations — consult a CA before going live

---

## Notes

- Paper trading state persists across restarts via CSV files
- Cash balance reloads from `portfolio.csv` on restart
- In live mode, positions are reconciled with actual CoinDCX holdings at startup to prevent double-entry after crashes
- `VERBOSE_DIAG = True` in config enables detailed per-coin indicator logs for debugging