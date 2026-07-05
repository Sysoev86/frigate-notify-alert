#!/bin/bash
# Manual run without systemd: ./run_monitor.sh [group]
# If [group] is omitted and config.py has exactly one group, it is used automatically.

set -e
cd "$(dirname "$0")"

if [ ! -f config.py ]; then
    echo "❌ config.py not found. First: cp config.example.py config.py and fill it in."
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "📥 Installing dependencies..."
pip install -q -r requirements.txt

GROUP="${1:-}"
if [ -z "$GROUP" ]; then
    GROUPS_LIST="$(python3 -c "from config import GROUPS; print(' '.join(GROUPS))")"
    set -- $GROUPS_LIST
    if [ "$#" -eq 1 ]; then
        GROUP="$1"
    else
        echo "Usage: ./run_monitor.sh <group>"
        echo "Available groups: $GROUPS_LIST"
        exit 1
    fi
fi

echo "🎯 Starting monitor for group: $GROUP"
exec python3 frigate_telegram_monitor.py "$GROUP"
