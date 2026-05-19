import os
import requests
from pathlib import Path

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

LOG_FILE = Path("logs/live.log")

# Read last 150 lines
with open(LOG_FILE, "r", encoding="utf-8") as f:
    lines = f.readlines()

last_run = lines[-150:]


# -------------------------------
# Extract Portfolio Snapshot block
# -------------------------------

portfolio = []
portfolio_found = False

for i, line in enumerate(last_run):

    if "Portfolio Snapshot" in line:
        portfolio_found = True

        # take next 15 lines
        portfolio = last_run[i:i + 15]
        break


# -------------------------------
# Extract Open Positions block
# -------------------------------

positions = []
positions_found = False

for i, line in enumerate(last_run):

    if "Open Positions" in line:
        positions_found = True

        # take next 80 lines
        positions = last_run[i:i + 80]
        break


# Remove separator-only lines
positions = [
    line for line in positions
    if not line.strip().startswith("=")
]


# -------------------------------
# Build Telegram message
# -------------------------------

message = (
    "📊 Trading Bot Update\n\n"
    + "".join(portfolio)
    + "\n"
    + "".join(positions)
)


# Telegram max message size safety
message = message[:4000]


# -------------------------------
# Send to Telegram
# -------------------------------

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

payload = {
    "chat_id": CHAT_ID,
    "text": message
}

response = requests.post(url, data=payload)

if response.status_code == 200:
    print("Telegram update sent successfully.")
else:
    print(f"Failed: {response.status_code}")
    print(response.text)