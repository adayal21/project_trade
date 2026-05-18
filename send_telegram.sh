#!/bin/bash

BOT_TOKEN="8578606440:AAGrUqdSqNL97jYsoTfd1t72nXGOmXf6hEE"  # your token
CHAT_ID="5920496287"                                 # your chat ID
LOG_FILE="logs/live.log"                            # adjust path if needed

# Grab last 100 lines of the log — that's where the latest run summary is
LAST_RUN=$(tail -n 150 "$LOG_FILE")

# Extract Portfolio Snapshot block
PORTFOLIO=$(echo "$LAST_RUN" | grep -A 10 "Portfolio Snapshot" | tail -n 9)

# Extract Open Positions block
POSITIONS=$(echo "$LAST_RUN" | grep -A 80 "Open Positions" | grep -v "^=")

# Build message
MESSAGE="📊 Trading Bot Update

$PORTFOLIO

$POSITIONS"

# Send
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=$CHAT_ID" \
  --data-urlencode "text=$MESSAGE" > /dev/null

echo "Sent at $(date)"