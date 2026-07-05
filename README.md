# frigate-notify-alert

Уведомления **Frigate → Telegram**: при обнаружении человека/машины бот присылает
фото + видео события в чат. Несколько групп камер (каждая в свой чат), фильтр по
зонам и кнопки паузы уведомлений прямо в Telegram.

> Не путать с [0x2142/frigate-notify](https://github.com/0x2142/frigate-notify) — это
> отдельный проект.

## Возможности
- 📸 Фото + видео события в Telegram (медиа-группой, беззвучно).
- 📹 Несколько групп камер — каждая в свой чат.
- 🧭 Фильтр по зонам Frigate (`zones`) — слать только когда объект в нужной зоне.
- ⏸ Пауза уведомлений кнопками в чате (15 мин / 1 час / 3 часа / до утра) — по группе.
- ➕ Масштабирование на любое число групп через шаблонный systemd-юнит.
- 🌐 Прокси для Telegram (обход блокировок).

## Требования
- Работающий **Frigate** с включённым **MQTT**.
- **Telegram-бот** (создать у [@BotFather](https://t.me/BotFather)) и ID чата.
- **Python 3.9+**, Linux с systemd (для автозапуска).

## Установка
```bash
git clone https://github.com/Sysoev86/frigate-notify-alert.git
cd frigate-notify-alert

cp config.example.py config.py     # свой конфиг (в .gitignore, в репо не попадёт)
nano config.py                     # заполнить: токен, чат, MQTT, камеры (подсказки внутри)

./install_deps.sh                  # venv + зависимости
sudo ./manage.sh install           # поставить юниты (по группам из config.py) + пульт
sudo ./manage.sh start
./manage.sh status
```
Ручной запуск без systemd: `./run_monitor.sh`.

## Обновление до последней версии
```bash
sudo ./manage.sh update    # git pull + переустановка юнитов + рестарт
./manage.sh version        # локальная версия и последний тег в origin
```
`config.py` не трогается (он в `.gitignore`), поэтому обновление не ломает настройки.

## Несколько групп / масштабирование
Каждая группа запускается шаблонным юнитом `frigate-telegram@<группа>`, а `manage.sh`
берёт список групп прямо из `config.py`. Чтобы добавить группу (хоть 3-ю, хоть 10-ю):
1. впиши её в `GROUPS` в `config.py`;
2. `sudo ./manage.sh install && sudo ./manage.sh start`.
Ни новых файлов, ни правок кода. Пульт паузы новую группу подхватит сам.

## Пауза уведомлений
Сервис `frigate-telegram-control` (`mute_controller.py`) держит в каждом чате
клавиатуру внизу: `⏸ 15 мин | 1 час | 3 часа | До утра | ▶️ Включить`. Нажал —
уведомления этой группы молчат до конца паузы (переживает перезапуск). Включается
флагом `mute_controls` на группу. Чтобы бот мог закреплять статус и убирать нажатия,
сделай его **администратором** чата (не обязательно).

## Зоны
Ключ `zones` в группе — список зон Frigate (`config.yml → cameras.<камера>.zones`).
Если задан, уведомление уйдёт только когда объект зашёл в одну из зон. Нет ключа —
шлём по всей камере.

## Управление
```
./manage.sh {install|start|stop|restart|status|logs|enable|disable|migrate|update|version}
```

## Как это работает
Подписка на MQTT-топик `frigate/events`, ловим завершённые события по нужным камерам,
объектам и (опц.) зонам, дожидаемся готовности snapshot + clip и шлём их в чат.

## Версии
[Semantic Versioning](https://semver.org/lang/ru/). Изменения — в [CHANGELOG.md](CHANGELOG.md),
релизы — на вкладке [Releases](https://github.com/Sysoev86/frigate-notify-alert/releases).

## Лицензия
[MIT](LICENSE).
