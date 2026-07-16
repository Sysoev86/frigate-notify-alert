"""Отправка ошибок в GlitchTip (self-hosted, Sentry-совместимый).

DSN берётся из `GLITCHTIP_DSN` (systemd грузит её из серверного env-файла).
Нет переменной — модуль молчит: локальный запуск и разовые скрипты ничего
никуда не шлют.

## Почему тут не просто sentry_sdk.init(dsn=...)

`send_default_pii=False` НЕ защищает от утечки — он закрывает только куки, IP
и заголовки. Замерено на sentry-sdk 2.66: при наивной конфигурации в событие
уезжают ВСЕ данные из окружения ошибки, потому что SDK по умолчанию шлёт:

- **локальные переменные каждого кадра трейсбека** (`include_local_variables=True`) —
  а там лежат токен бота, proxy-URL с логином и паролем, chat_id получателей,
  пароль MQTT и куски событий Frigate;
- **тело HTTP-запроса** (`max_request_body_size="medium"`);
- **хлебные крошки из логов** — включая логи чужих библиотек (telegram, aiohttp,
  paho-mqtt), где секреты попадают в URL и query-строки.

Что реально ходит через ЭТОТ сервис: токен Telegram-бота, proxy-URL вида
`http://LOGIN:PASSWORD@host:port`, логин/пароль MQTT, chat_id чатов (это
идентификатор конкретного человека), тексты сообщений и подписи к альбомам,
а также события Frigate — пути и URL к снимкам/записям с камер, base64-эскизы
кадра с человеком и `sub_label`/распознанный номер, если включено распознавание.
Снимок с камеры и распознанное имя/номер — ПДн (152-ФЗ: не логируем). GlitchTip
свой, но это всё равно вторая копия ПДн с другим сроком жизни и другим доступом.

Поэтому: лишнее выключено на уровне init, а `before_send` — последний рубеж на
случай, если набор отправляемого по умолчанию молча расширится при обновлении SDK.

Проверять замером, а не чтением: см. `scripts/glitchtip_check.py` — он гоняет
настоящее исключение с приманками и падает, если что-то утекло.
"""

import os
import re
from typing import Any

_enabled = False

# Служебные ключи самого Sentry. Их значения — структура события, а не данные:
# замаскируешь — SDK упадёт на собственном событии, и оно потеряется молча.
#
# Именно так и было в соседнем сервисе: подстрока "text" из списка ПДн совпадала
# с "conTEXTs", `contexts` становился строкой, и sentry падал на
# `event["contexts"].get("trace")` внутри себя — УЖЕ ПОСЛЕ before_send. В бою это
# означало трекер, до которого не долетает НИЧЕГО: хуже, чем отсутствие трекера,
# потому что создаёт ощущение присмотра. Проверяется ПЕРВЫМ, не выкидывать.
_STRUCTURAL = frozenset(
    {
        "contexts",
        "exception",
        "values",
        "stacktrace",
        "frames",
        "mechanism",
        "threads",
        "logentry",
        "sdk",
        "modules",
        "packages",
        "integrations",
        "event_id",
        "timestamp",
        "level",
        "platform",
        "server_name",
        "environment",
        "release",
        "transaction",
        "transaction_info",
        "type",
        "value",
        "module",
        "filename",
        "abs_path",
        "function",
        "lineno",
        "context_line",
        "pre_context",
        "post_context",
        "in_app",
    }
)

# Секреты. Здесь подстрока оправдана: одно и то же зовут по-разному
# (psw, token, api_key…), и ни одно из этих слов не встречается в служебных
# ключах Sentry.
#
# "proxy" — специфика этого сервиса: TELEGRAM_PROXY_URL имеет вид
# http://LOGIN:PASSWORD@host:port, то есть сам по себе является кредом.
# Подстрокой ловит и proxy, и proxy_url, и telegram_proxy_url, и https_proxy;
# служебного ключа Sentry с "proxy" внутри нет.
_SECRET_HINTS = (
    "pass",
    "psw",
    "token",
    "secret",
    "api_key",
    "apikey",
    "x-api-key",
    "authorization",
    "cookie",
    "key_hash",
    "session",
    "credential",
    "dsn",
    "proxy",
)

# Персональные данные. ТОЧНОЕ совпадение имени, без подстрок: слова тут слишком
# общие ("text", "body", "message") и подстрокой цепляют служебные ключи Sentry.
#
# Специфика frigate-notify-alert (ниже первой строки):
#   chat_id/telegram_chat_id — идентификатор человека в Telegram;
#   caption/text             — то, что мы отправляем в чат;
#   thumbnail                — base64-кадр с человеком из события Frigate;
#   sub_label/plate/…        — распознанное лицо или автомобильный номер;
#   *_url/*_path/path        — пути и ссылки на снимки и записи с камер.
# Точность имени тут и спасает: "snapshot" не задевает "has_snapshot" (bool,
# нужен для разбора), "label" (класс объекта: person/car) не маскируется вовсе —
# это не ПДн, а без него в трекере не разобраться.
_PII_KEYS = frozenset(
    {
        "phone",
        "to_addr",
        "to_phone",
        "body",
        "text",
        "message",
        "memo",
        "smsnum",
        "email",
        "msg",
        "chat_id",
        "telegram_chat_id",
        "caption",
        "username",
        "mqtt_username",
        "thumbnail",
        "sub_label",
        "plate",
        "recognized_license_plate",
        "snapshot",
        "clip",
        "path",
        "photo_url",
        "clip_url",
        "video_url",
        "snapshot_url",
        "clip_path",
        "snapshot_path",
        "recording_path",
    }
)

_MASK = "[вырезано]"

# Телефон в свободном тексте — в сообщении об ошибке, в URL, в SQL.
# 10-15 цифр подряд, возможно с +: подходит и E.164, и «голый» номер.
# Здесь заодно накрывает chat_id вида -1001234567890 и числовую часть
# токена бота, попавшие в текст сообщения об ошибке.
_PHONE_RE = re.compile(r"\+?\d{10,15}")

# Путь или URL к снимку/записи с камеры в свободном тексте. Маскировки по имени
# ключа тут мало: такой путь регулярно оказывается ВНУТРИ текста ошибки, где
# ключа нет вовсе — aiohttp пишет URL в сообщение («Cannot connect to host …
# /api/events/<id>/snapshot.jpg»), и мы сами подставляем путь к клипу в свои
# сообщения. Замерено: без этого путь к записи уезжал в трекер, пройдя весь
# key-based скраб насквозь.
#
# Список расширений намеренно узкий — только медиа Frigate. Широкий шаблон
# «любой путь» съел бы `abs_path`/`filename` кадров трейсбека (`…/monitor.py`),
# то есть само место ошибки, и трекер бы ослеп. `.py` тут нет и быть не должно.
_MEDIA_RE = re.compile(r"\S*\.(?:mp4|jpg|jpeg|png|webp|gif)\b", re.IGNORECASE)


def _looks_sensitive(key: str) -> bool:
    k = str(key).lower()
    # Служебное не трогаем ни при каких совпадениях — иначе ломаем само событие.
    if k in _STRUCTURAL:
        return False
    return k in _PII_KEYS or any(h in k for h in _SECRET_HINTS)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Рекурсивно вычистить структуру события.

    Ограничение по глубине — защита от зацикленных структур: падать в
    обработчике ошибок особенно глупо.
    """
    if depth > 12:
        return value
    if isinstance(value, dict):
        return {
            k: (_MASK if _looks_sensitive(k) else _scrub(v, depth + 1)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, str):
        # Даже там, где ключ безобиден, номер может оказаться внутри строки:
        # «Failed to send panel to chat -1001234567890» — такое сообщение пишем
        # мы сами (mute_controller.py). То же и с путём к записи с камеры.
        return _MEDIA_RE.sub("[медиа]", _PHONE_RE.sub("[номер]", value))
    return value


def _before_send(event: dict, hint: dict) -> dict | None:
    """Последний рубеж перед отправкой.

    Настройки init'а уже должны были всё выключить. Этот проход существует
    потому, что список отправляемого по умолчанию меняется от версии к версии
    SDK и однажды расширится молча — при обычном обновлении зависимостей.
    """
    try:
        event.pop("breadcrumbs", None)

        req = event.get("request")
        if isinstance(req, dict):
            # Тело и заголовки не нужны: разбираться по ним всё равно нечем
            # без ПДн, а url+метод для поиска места ошибки хватает.
            for k in ("data", "cookies", "headers", "env"):
                req.pop(k, None)
            req.pop("query_string", None)
            if isinstance(req.get("url"), str):
                # В query-строке лежит и токен бота (api.telegram.org/bot<TOKEN>/…),
                # и логин с паролем от proxy.
                req["url"] = _PHONE_RE.sub("[номер]", req["url"].split("?")[0])

        return _scrub(event)
    except Exception:
        # Не смогли вычистить — не отправляем. Событие потерять не жалко,
        # ПДн отправить — жалко.
        return None


def init(component: str) -> None:
    """Поднять отправку ошибок. component — метка процесса."""
    global _enabled
    dsn = (os.environ.get("GLITCHTIP_DSN") or "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("GLITCHTIP_ENV", "prod"),
        server_name=component,
        # Нужны только ошибки: трейсинг GlitchTip поддерживает частично.
        traces_sample_rate=0,
        # Ни заголовков, ни кук, ни IP. Сам по себе НЕ защищает — см. шапку.
        send_default_pii=False,
        # Локальные переменные кадров — главный источник утечки. Трейсбек без
        # них читается хуже, но имя файла со строкой остаётся, а этого хватает.
        include_local_variables=False,
        # Тело запроса не забирать вообще, ни при каком размере.
        max_request_body_size="never",
        # Крошки из логов чужих библиотек — ещё один путь для URL с секретами.
        max_breadcrumbs=0,
        before_send=_before_send,
    )
    sentry_sdk.set_tag("component", component)
    _enabled = True


def capture(exc) -> None:
    """Отправить пойманное исключение. Без init() — ничего не делает."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
