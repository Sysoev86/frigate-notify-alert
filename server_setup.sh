#!/bin/bash
# One-shot server setup: dependencies + systemd units + start.
# Run from the cloned repo: sudo bash server_setup.sh

set -e
cd "$(dirname "$0")"

echo "🚀 Setting up frigate-notify-alert..."
echo "📁 Install directory: $(pwd)"

if [ ! -f config.py ]; then
    echo "❌ config.py not found. First: cp config.example.py config.py and fill it in."
    exit 1
fi

echo "🛑 Stopping any running monitors..."
pkill -f "frigate_telegram_monitor" || true

echo "📦 Installing dependencies..."
./install_deps.sh

echo "⚙️ Installing systemd units..."
./manage.sh install

echo "🚀 Starting services..."
./manage.sh start

echo "📊 Service status:"
./manage.sh status

echo ""
echo "✅ Setup complete!"
echo "📋 Manage with: ./manage.sh {start|stop|restart|status|logs|update|version}"
