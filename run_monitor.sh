#!/bin/bash
# Скрипт запуска мониторинга Frigate

echo "🚀 Запуск мониторинга Frigate Telegram..."

# Проверяем наличие виртуального окружения
if [ ! -d "venv" ]; then
    echo "📦 Создание виртуального окружения..."
    python3 -m venv venv
fi

# Активируем виртуальное окружение
echo "🔧 Активация виртуального окружения..."
source venv/bin/activate

# Устанавливаем зависимости
echo "📥 Установка зависимостей..."
pip install -r requirements.txt

# Запускаем мониторинг
echo "🎯 Запуск мониторинга..."
python3 frigate_telegram_monitor.py
