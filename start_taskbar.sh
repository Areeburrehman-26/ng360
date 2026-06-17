#!/bin/bash
# start_taskbar.sh
# Launch the NG360 Bot macOS Taskbar App.
#
# Usage:
#   cd /Users/desmondthomas/Desktop/all-in-one/nsg360_bot
#   bash start_taskbar.sh

cd "$(dirname "$0")"

echo "Starting NG360 Bot Taskbar App..."
python3 ng360_bot_taskbar.py
