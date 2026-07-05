# Changelog

Все заметные изменения проекта. Формат по [Semantic Versioning](https://semver.org/lang/ru/):
`MAJOR.MINOR.PATCH`.

## [1.0.0] — 2026-07-05
Первый публичный релиз.

### Возможности
- Мониторинг событий Frigate через MQTT (`frigate/events`), отправка фото + видео
  в Telegram при обнаружении человека/машины.
- Несколько групп камер — каждая в свой чат (`GROUPS` в `config.py`).
- Масштабирование на любое число групп через шаблонный systemd-юнит
  `frigate-telegram@<группа>`; `manage.sh` читает список групп из конфига.
- Фильтр по зонам Frigate (`zones`) — слать только когда объект в нужной зоне.
- Пауза уведомлений кнопками в Telegram (`mute_controller`): reply-клавиатура
  внизу чата (⏸ 15м/1ч/3ч/до утра, ▶️ включить). Пауза на группу, переживает
  перезапуск. Включается флагом `mute_controls` на группу.
- Прокси для Telegram (обход блокировок).
- Управление через `manage.sh` (install/start/stop/restart/status/logs/update/version).
