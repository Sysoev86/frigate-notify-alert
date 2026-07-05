#!/bin/bash

# Скрипт для настройки сервера Frigate Telegram Monitor
# Запускать на сервере: bash server_setup.sh

echo "🚀 Настройка Frigate Telegram Monitor на сервере..."

# Работаем в каталоге, где лежит этот скрипт (куда склонирован репозиторий)
cd "$(dirname "$0")"

echo "📁 Каталог установки: $(pwd)"

# Проверка, что config.py создан
if [ ! -f config.py ]; then
    echo "❌ Нет config.py. Сначала: cp config.example.py config.py и заполни его."
    exit 1
fi

# Останавливаем все запущенные процессы
echo "🛑 Остановка существующих процессов..."
pkill -f "frigate_telegram_monitor" || true

# Устанавливаем зависимости
echo "📦 Установка зависимостей..."
./install_deps.sh

# Устанавливаем systemd сервисы (group1 + group2)
echo "⚙️ Установка systemd сервисов..."
./manage.sh install

# Запускаем сервисы
echo "🚀 Запуск сервисов..."
./manage.sh start

# Проверяем статус
echo "📊 Статус сервисов:"
./manage.sh status

echo ""
echo "✅ Настройка завершена!"
echo ""
echo "📋 Управление: ./manage.sh {start|stop|restart|status|logs}"
echo "  Логи:        ./manage.sh logs"
echo ""
