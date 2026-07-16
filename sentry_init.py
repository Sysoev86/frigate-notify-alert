"""Отправка ошибок в GlitchTip (self-hosted, Sentry-совместимый).

DSN берётся из `GLITCHTIP_DSN` (systemd грузит её из серверного env-файла).
Нет переменной — модуль молчит: локальный запуск и разовые скрипты ничего
никуда не шлют.

## Почему тут не просто sentry_sdk.init(dsn=...)

`send_default_pii=False` НЕ защищает от утечки — он закрывает только куки, IP
и заголовки. Замерено на sentry-sdk 2.66: при наивной конфигурации в событие
уезжают ВСЕ данные из окружения ошибки, потому что SDK по умолчанию шлёт:

- **локальные переменные каждого кадра трейсбека** (`include_local_variables=True`) —
  а там лежат телефоны, тексты сообщений, распакованные токены и пароли;
- **тело HTTP-запроса** (`max_request_body_size="medium"`);
- **хлебные крошки из логов** — включая логи чужих библиотек, где секреты
  попадают в URL и query-строки.

Через сервисы портфеля идут телефоны и тексты (152-ФЗ: не логируем), а также
чужие креды. GlitchTip свой, но это всё равно вторая копия ПДн с другим сроком
жизни и другим доступом.

Поэтому: лишнее выключено на уровне init, а `before_send` — последний рубеж на
случай, если набор отправляемого по умолчанию молча расширится при обновлении SDK.

Проверять замером, а не чтением: см. `uvedomim/scripts/glitchtip_check.py` —
он гоняет настоящее исключение с приманками и падает, если что-то утекло.
"""

import os
import re
from typing import Any

_enabled = False

# Ключи, значение которых не должно уехать никогда — как бы они ни назывались.
# Сопоставление по подстроке: имён у одного секрета в разных API много
# (psw, token, api_key…), перечислять точные бесполезно.
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
)

# Персональные данные: телефоны, тексты сообщений, адреса.
_PII_HINTS = ("phone", "to_addr", "to_phone", "body", "text", "message", "memo", "smsnum", "email")

_MASK = "[вырезано]"

# Телефон в свободном тексте — в сообщении об ошибке, в URL, в SQL.
# 10-15 цифр подряд, возможно с +: подходит и E.164, и «голый» номер.
_PHONE_RE = re.compile(r"\+?\d{10,15}")


def _looks_sensitive(key: str) -> bool:
    k = str(key).lower()
    return any(h in k for h in _SECRET_HINTS) or any(h in k for h in _PII_HINTS)


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
        # Даже там, где ключ безобиден, телефон может оказаться внутри строки:
        # «нет сессии для +79991234567» — такое сообщение пишем мы сами.
        return _PHONE_RE.sub("[номер]", value)
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
                # В query-строке у некоторых провайдеров лежат логин с паролем.
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
