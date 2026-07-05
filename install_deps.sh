#!/bin/bash
# Install dependencies into a local virtual environment.

set -e
cd "$(dirname "$0")"

echo "🔧 Installing dependencies for frigate-notify-alert..."

if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "⬆️ Upgrading pip..."
python -m pip install -q --upgrade pip

echo "📥 Installing requirements..."
pip install -q -r requirements.txt

echo "✅ Dependencies installed into ./venv"
echo ""
echo "📋 Next steps:"
echo "  sudo ./manage.sh install  - install systemd units (one per group from config.py)"
echo "  sudo ./manage.sh start    - start everything"
echo "  ./run_monitor.sh          - manual run without systemd"
