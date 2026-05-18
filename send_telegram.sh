#!/bin/bash

# Configuration
BOT_TOKEN="8578606440:AAGrUqdSqNL97jYsoTfd1t72nXGOmXf6hEE"  # Replace with your token
CHAT_ID="5920496287"                                # Replace with your user ID
LOG_FILE="data/live.log"
DATA_DIR="data"

# Extract the Open Positions section from live.log
if [ ! -f "$LOG_FILE" ]; then
    echo "Log file not found"
    exit 1
fi

# Extract everything after "Open Positions" section
SUMMARY=$(tail -n 200 "$LOG_FILE" | awk '/^Open Positions/,EOF' | head -n 100)

# Also grab the Portfolio Snapshot
PORTFOLIO=$(tail -n 200 "$LOG_FILE" | awk '/^Portfolio Snapshot/,/^Done\./' | head -n 20)

# Combine into a single message
MESSAGE="*Trading Bot Update*

$PORTFOLIO

$SUMMARY"

# Send to Telegram
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -H 'Content-Type: application/json' \
  -d "{
    \"chat_id\": \"$CHAT_ID\",
    \"text\": \"$MESSAGE\",
    \"parse_mode\": \"Markdown\"
  }" > /dev/null

echo "Telegram notification sent at $(date)"