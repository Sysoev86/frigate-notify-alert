#!/bin/bash

# Управление сервисами Frigate Telegram Monitor.
#
# Масштабируется само: список групп берётся прямо из config.py (GROUPS), а каждая
# группа запускается шаблонным юнитом frigate-telegram@<группа>. Чтобы добавить
# группу — впиши её в GROUPS в config.py и выполни:  ./manage.sh install && ./manage.sh start
# Плюс один общий сервис пульта паузы: frigate-telegram-control.

set -euo pipefail
cd "$(dirname "$0")"

APPDIR="$(pwd)"                 # реальный путь установки — подставляется в юниты
SERVICE_CONTROL="frigate-telegram-control"
UNIT_TPL="frigate-telegram@"   # шаблонный юнит, инстанс = имя группы

# Список групп из config.py (config.py — чистый python без зависимостей)
get_groups() {
    python3 -c "from config import GROUPS; print(' '.join(GROUPS))" 2>/dev/null || {
        echo "❌ Не удалось прочитать GROUPS из config.py" >&2; exit 1; }
}

GROUPS_LIST="$(get_groups)"

group_units() {  # печатает frigate-telegram@group1 frigate-telegram@group2 ...
    for g in $GROUPS_LIST; do echo -n "${UNIT_TPL}${g} "; done
}

case "${1:-}" in
    start)
        echo "🚀 Запуск: группы [$GROUPS_LIST] + пульт паузы"
        for g in $GROUPS_LIST; do systemctl start "${UNIT_TPL}${g}"; done
        systemctl start "$SERVICE_CONTROL"
        echo "✅ Запущено"
        ;;
    stop)
        echo "🛑 Остановка всех сервисов"
        for g in $GROUPS_LIST; do systemctl stop "${UNIT_TPL}${g}" || true; done
        systemctl stop "$SERVICE_CONTROL" || true
        echo "✅ Остановлено"
        ;;
    restart)
        echo "🔄 Перезапуск всех сервисов"
        for g in $GROUPS_LIST; do systemctl restart "${UNIT_TPL}${g}"; done
        systemctl restart "$SERVICE_CONTROL"
        echo "✅ Перезапущено"
        ;;
    status)
        for g in $GROUPS_LIST; do
            echo "=== $g ==="
            systemctl status "${UNIT_TPL}${g}" --no-pager || true
            echo ""
        done
        echo "=== Пульт паузы (mute controller) ==="
        systemctl status "$SERVICE_CONTROL" --no-pager || true
        ;;
    logs)
        echo "📋 Логи всех групп + пульта (Ctrl+C для выхода)"
        # -u принимает glob-шаблоны, поэтому ловим все инстансы разом
        journalctl -f -u "${UNIT_TPL}*" -u "$SERVICE_CONTROL"
        ;;
    enable)
        echo "⚙️ Автозапуск для групп [$GROUPS_LIST] + пульта"
        for g in $GROUPS_LIST; do systemctl enable "${UNIT_TPL}${g}"; done
        systemctl enable "$SERVICE_CONTROL"
        echo "✅ Автозапуск включён"
        ;;
    disable)
        echo "❌ Отключение автозапуска"
        for g in $GROUPS_LIST; do systemctl disable "${UNIT_TPL}${g}" || true; done
        systemctl disable "$SERVICE_CONTROL" || true
        echo "✅ Автозапуск отключён"
        ;;
    install)
        echo "📦 Установка юнитов (шаблон групп + пульт), путь: $APPDIR"
        # Подставляем реальный путь установки вместо плейсхолдера __APPDIR__
        sed "s#__APPDIR__#${APPDIR}#g" "${UNIT_TPL}.service"     > "/etc/systemd/system/${UNIT_TPL}.service"
        sed "s#__APPDIR__#${APPDIR}#g" "${SERVICE_CONTROL}.service" > "/etc/systemd/system/${SERVICE_CONTROL}.service"
        systemctl daemon-reload
        for g in $GROUPS_LIST; do systemctl enable "${UNIT_TPL}${g}"; done
        systemctl enable "$SERVICE_CONTROL"
        echo "✅ Установлено и включено для групп: $GROUPS_LIST (+ пульт)"
        echo "   Дальше: ./manage.sh start"
        ;;
    version)
        v="$(cat VERSION 2>/dev/null || echo '?')"
        echo "📌 Локальная версия: $v"
        if git rev-parse --git-dir >/dev/null 2>&1; then
            echo "   коммит: $(git rev-parse --short HEAD 2>/dev/null)"
            git fetch --tags -q origin 2>/dev/null || true
            latest="$(git tag -l 'v*' | sort -V | tail -1)"
            echo "   последний тег: ${latest:-нет}"
        fi
        ;;
    update)
        echo "⬇️  Обновление до последней версии..."
        git pull --ff-only origin main || { echo "❌ git pull не удался"; exit 1; }
        echo "🔧 Обновляю юниты и перезапускаю..."
        "$0" install
        "$0" restart
        "$0" version
        echo "✅ Обновлено"
        ;;
    *)
        echo "🔧 Управление Frigate → Telegram (frigate-notify-alert)"
        echo ""
        echo "Использование: $0 {install|start|stop|restart|status|logs|enable|disable|update|version}"
        echo ""
        echo "Группы (из config.py): $GROUPS_LIST"
        echo ""
        echo "  install  - поставить юниты и включить автозапуск (читает группы из config.py)"
        echo "  update   - git pull + переустановка юнитов + рестарт (обновиться до последней версии)"
        echo "  version  - показать локальную версию и последний тег в origin"
        echo "  start    - запустить все группы + пульт паузы"
        echo "  stop     - остановить всё"
        echo "  restart  - перезапустить всё"
        echo "  status   - статус всех групп + пульта"
        echo "  logs     - логи всех групп + пульта"
        echo ""
        echo "➕ Добавить группу: впиши её в GROUPS (config.py), затем:"
        echo "   ./manage.sh install && ./manage.sh start"
        ;;
esac
