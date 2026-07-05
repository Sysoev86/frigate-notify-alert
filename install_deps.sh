#!/bin/bash

# Скрипт для установки зависимостей на сервере

echo "🔧 Установка зависимостей для Frigate Telegram Monitor..."

# Создаем виртуальное окружение если его нет
if [ ! -d "venv" ]; then
    echo "📦 Создание виртуального окружения..."
    python3 -m venv venv
fi

# Активируем виртуальное окружение
echo "🔄 Активация виртуального окружения..."
source venv/bin/activate

# Обновляем pip
echo "⬆️ Обновление pip..."
python -m pip install --upgrade pip

# Устанавливаем зависимости
echo "📥 Установка зависимостей..."
pip install -r requirements.txt

echo "✅ Зависимости установлены в виртуальном окружении!"
echo ""
echo "📋 Дальше:"
echo "  ./manage.sh install  - установка systemd сервисов (group1 + group2)"
echo "  ./manage.sh start    - запуск"
echo "  ./run_monitor.sh     - ручной запуск без systemd"
echo ""
echo "💡 Виртуальное окружение создано в папке 'venv'"
