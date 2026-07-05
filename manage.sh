#!/bin/bash

# Service management for frigate-notify-alert.
#
# Scales by itself: the group list is read straight from config.py (GROUPS), and
# each group runs as a templated systemd unit frigate-telegram@<group>. To add a
# group, put it into GROUPS and run:  ./manage.sh install && ./manage.sh start
# Plus one shared pause-controller service: frigate-telegram-control.

set -euo pipefail
cd "$(dirname "$0")"

APPDIR="$(pwd)"                 # real install path — substituted into the units
SERVICE_CONTROL="frigate-telegram-control"
UNIT_TPL="frigate-telegram@"   # templated unit, instance = group name

if [ ! -f config.py ]; then
    echo "❌ config.py not found. First: cp config.example.py config.py (then edit it)"
    exit 1
fi

# Group list from config.py (config.py is plain python with no dependencies)
get_groups() {
    python3 -c "from config import GROUPS; print(' '.join(GROUPS))" 2>/dev/null || {
        echo "❌ Could not read GROUPS from config.py" >&2; exit 1; }
}

GROUPS_LIST="$(get_groups)"

# venv + dependencies (idempotent, quiet when already installed)
install_deps() {
    if [ ! -d venv ]; then
        echo "📦 Creating virtual environment..."
        python3 -m venv venv
    fi
    echo "📥 Installing requirements..."
    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
}

case "${1:-}" in
    setup)
        # Full first-time setup: dependencies + systemd units + start
        echo "🚀 Setting up frigate-notify-alert in $APPDIR"
        install_deps
        "$0" install
        "$0" start
        "$0" status
        echo ""
        echo "✅ Setup complete. Manage with: ./manage.sh {start|stop|restart|status|logs|update}"
        ;;
    run)
        # Manual foreground run without systemd: ./manage.sh run [group]
        GROUP="${2:-}"
        if [ -z "$GROUP" ]; then
            set -- $GROUPS_LIST
            if [ "$#" -eq 1 ]; then
                GROUP="$1"
            else
                echo "Usage: $0 run <group>"
                echo "Available groups: $GROUPS_LIST"
                exit 1
            fi
        fi
        install_deps
        echo "🎯 Starting monitor for group: $GROUP (Ctrl+C to stop)"
        exec ./venv/bin/python frigate_telegram_monitor.py "$GROUP"
        ;;
    start)
        echo "🚀 Starting: groups [$GROUPS_LIST] + pause controller"
        for g in $GROUPS_LIST; do systemctl start "${UNIT_TPL}${g}"; done
        systemctl start "$SERVICE_CONTROL"
        echo "✅ Started"
        ;;
    stop)
        echo "🛑 Stopping all services"
        for g in $GROUPS_LIST; do systemctl stop "${UNIT_TPL}${g}" || true; done
        systemctl stop "$SERVICE_CONTROL" || true
        echo "✅ Stopped"
        ;;
    restart)
        echo "🔄 Restarting all services"
        for g in $GROUPS_LIST; do systemctl restart "${UNIT_TPL}${g}"; done
        systemctl restart "$SERVICE_CONTROL"
        echo "✅ Restarted"
        ;;
    status)
        for g in $GROUPS_LIST; do
            echo "=== $g ==="
            systemctl status "${UNIT_TPL}${g}" --no-pager || true
            echo ""
        done
        echo "=== Pause controller ==="
        systemctl status "$SERVICE_CONTROL" --no-pager || true
        ;;
    logs)
        echo "📋 Live logs for all groups + controller (Ctrl+C to exit)"
        # -u accepts glob patterns, so this catches every instance at once
        journalctl -f -u "${UNIT_TPL}*" -u "$SERVICE_CONTROL"
        ;;
    enable)
        echo "⚙️ Enabling autostart for groups [$GROUPS_LIST] + controller"
        for g in $GROUPS_LIST; do systemctl enable "${UNIT_TPL}${g}"; done
        systemctl enable "$SERVICE_CONTROL"
        echo "✅ Autostart enabled"
        ;;
    disable)
        echo "❌ Disabling autostart"
        for g in $GROUPS_LIST; do systemctl disable "${UNIT_TPL}${g}" || true; done
        systemctl disable "$SERVICE_CONTROL" || true
        echo "✅ Autostart disabled"
        ;;
    install)
        echo "📦 Installing units (group template + controller), path: $APPDIR"
        # Substitute the real install path for the __APPDIR__ placeholder
        sed "s#__APPDIR__#${APPDIR}#g" "${UNIT_TPL}.service"     > "/etc/systemd/system/${UNIT_TPL}.service"
        sed "s#__APPDIR__#${APPDIR}#g" "${SERVICE_CONTROL}.service" > "/etc/systemd/system/${SERVICE_CONTROL}.service"
        systemctl daemon-reload
        for g in $GROUPS_LIST; do systemctl enable "${UNIT_TPL}${g}"; done
        systemctl enable "$SERVICE_CONTROL"
        echo "✅ Installed and enabled for groups: $GROUPS_LIST (+ controller)"
        echo "   Next: ./manage.sh start"
        ;;
    version)
        v="$(cat VERSION 2>/dev/null || echo '?')"
        echo "📌 Local version: $v"
        if git rev-parse --git-dir >/dev/null 2>&1; then
            echo "   commit: $(git rev-parse --short HEAD 2>/dev/null)"
            git fetch --tags -q origin 2>/dev/null || true
            latest="$(git tag -l 'v*' | sort -V | tail -1)"
            echo "   latest tag: ${latest:-none}"
        fi
        ;;
    update)
        echo "⬇️  Updating to the latest version..."
        git pull --ff-only origin main || { echo "❌ git pull failed"; exit 1; }
        echo "🔧 Reinstalling units and restarting..."
        "$0" install
        "$0" restart
        "$0" version
        echo "✅ Updated"
        ;;
    *)
        echo "🔧 frigate-notify-alert service management"
        echo ""
        echo "Usage: $0 {setup|run|install|start|stop|restart|status|logs|enable|disable|update|version}"
        echo ""
        echo "Groups (from config.py): $GROUPS_LIST"
        echo ""
        echo "  setup    - first-time install: dependencies + systemd units + start (sudo)"
        echo "  run      - manual foreground run without systemd: $0 run [group]"
        echo "  install  - install units and enable autostart (reads groups from config.py)"
        echo "  update   - git pull + reinstall units + restart (get the latest version)"
        echo "  version  - show local version and the latest tag on origin"
        echo "  start    - start all groups + pause controller"
        echo "  stop     - stop everything"
        echo "  restart  - restart everything"
        echo "  status   - status of all groups + controller"
        echo "  logs     - live logs of all groups + controller"
        echo ""
        echo "➕ Add a group: put it into GROUPS (config.py), then:"
        echo "   ./manage.sh install && ./manage.sh start"
        ;;
esac
