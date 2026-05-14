# Supertrend Crypto Paper Trading Bot

## Features
- Yahoo Finance data via yfinance
- 1H timeframe across 4 coins
- Multi-indicator strategy: Supertrend + EMA200 + RSI crossover + Volume + ADX + ATR
- Persistent positions across restarts (CSV-backed)
- Stop-loss per trade
- Capital allocation guard
- CSV trade logging with exit reason
- Portfolio equity tracking

## Coins
- BTC-USD
- ETH-USD
- SOL-USD
- BNB-USD

## Strategy

### Hard Filters (both must pass or no signal)
| Filter | Condition |
|--------|-----------|
| ADX regime | ADX > 25 (trending market) |
| ATR expansion | ATR >= 80% of ATR 20-bar SMA (not compressing) |

### Mandatory Conditions (both required per direction)
| Condition | LONG | SHORT |
|-----------|------|-------|
| EMA200 | Close > EMA200 | Close < EMA200 |
| Supertrend | Bullish | Bearish |

### Soft Conditions (at least 1 of 2 required)
| Condition | LONG | SHORT |
|-----------|------|-------|
| RSI crossover | RSI crosses above 50 | RSI crosses below 50 |
| Volume | Volume > Volume SMA20 | Volume > Volume SMA20 |

### Exit Conditions
- **Counter-signal**: opposite directional signal fires
- **Stop-loss**: 4% adverse move from entry (configurable in `main.py`)

## Configuration (`config.py`)
```python
INITIAL_CAPITAL  = 10000
RISK_PER_TRADE   = 0.25     # 25% of cash per trade
TIMEFRAME        = "2h"
```

Additional constants in `main.py`:
```python
STOP_LOSS_PCT   = 0.04   # 4% hard stop
MAX_ALLOCATION  = 0.80   # max 80% of cash deployed at once
```

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
python main.py
```

Designed to be run on a cron job or scheduler every 2 hours.

## Data Output (`data/` directory)
| File | Contents |
|------|----------|
| `portfolio.csv` | Equity curve, cash, PnL snapshots |
| `{COIN}_position.csv` | Current open position (deleted on close) |
| `{COIN}_trades.csv` | Full trade history with entry, exit, PnL, exit reason |

## Notes
- Paper trading only — no real orders placed
- State persists across restarts via CSV files
- Cash balance is reloaded from `portfolio.csv` on restart (not reset to INITIAL_CAPITAL)