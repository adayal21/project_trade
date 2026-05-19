"""
connectivity_test.py — verify CoinDCX live trading setup.

Run this BEFORE switching TRADING_MODE to "live" to confirm:
  1. Your API keys are correct
  2. Your balance is readable
  3. Your holdings are readable
  4. Order construction works (dry run — no real orders placed)

Usage:
    python connectivity_test.py

Set your credentials first:
    export COINDCX_API_KEY=your_key
    export COINDCX_API_SECRET=your_secret

Or put them in a .env file in this directory:
    COINDCX_API_KEY=your_key
    COINDCX_API_SECRET=your_secret
"""

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("Loaded .env file")
except ImportError:
    print("python-dotenv not installed — reading env vars from shell")

from config import COINS
from exchange import run_connectivity_test

if __name__ == "__main__":
    run_connectivity_test(COINS)