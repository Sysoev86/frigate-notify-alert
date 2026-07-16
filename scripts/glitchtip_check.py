"""Доказать, что в GlitchTip не уезжают ПДн и секреты этого сервиса.

Читать настройки SDK и верить им — недостаточно: список того, что Sentry шлёт
по умолчанию, меняется от версии к версии и однажды расширится молча, при
обычном `pip install -U`. Поэтому здесь настоящее исключение с настоящим
токеном бота, proxy-URL с паролем, chat_id и куском события Frigate прогоняется
через реальный `sentry_sdk.init(...)` нашей конфигурации, а событие
перехватывается на ТРАНСПОРТЕ — то есть ровно в том виде, в каком ушло бы в сеть.

Транспорт, а не before_send — и это не мелочь. Проверка, вставшая на before_send,
остаётся ЗЕЛЁНОЙ при сломанной системе: всё, что SDK делает после before_send,
она не видит вовсе. Именно там и жил баг соседнего сервиса: маска по подстроке
"text" попадала в служебный ключ "contexts", sentry падал на собственном событии
уже ПОСЛЕ before_send, и в трекер не долетало ничего. Поэтому здесь же
проверяется, что событие вообще доехало до транспорта и осталось полезным —
чистое, но потерянное событие не лучше грязного.

Запуск (сеть не нужна):  python3 scripts/glitchtip_check.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sentry_sdk  # noqa: E402

import sentry_init  # noqa: E402
from sentry_init import _MASK  # noqa: E402

# Приманки: если хоть одна найдётся в исходящем событии — утечка.
#
# Токен и пароль собираются из кусков, а не лежат литералами: строка вида
# 1234567890:AA... в исходнике — это ровно то, на что справедливо ругается
# gitleaks в pre-commit. Обходить его флагом ради теста нельзя: правило
# перестанет работать в тот день, когда в staged окажется настоящий токен.
# На проверку это не влияет — в рантайме приманка та же самая.
BOT_TOKEN = "8199234100" + ":AA" + "primanka_ne_nastoyashchiy_token_bota"
PROXY_PASS = "primanka-" + "ne-nastoyashchiy-parol-proxy"
PROXY_URL = f"http://tguser:{PROXY_PASS}@10.20.30.40:3128"
MQTT_PASS = "primanka-" + "ne-nastoyashchiy-parol-mqtt"
CHAT_ID = "-1001234567890"
# Подпись, уходящая в чат, и распознанное Frigate имя/номер — это ПДн.
CAPTION = "Обнаружен человек у шлагбаума, 19:00 — запись во Frigate"
SUB_LABEL = "Сысоев Алексей"
PLATE = "А123ВС716"
# Путь к записи с камеры: сервис реально ими оперирует.
CLIP_PATH = "/media/frigate/recordings/2026-07-17/19/yap_shlagbaum/12.34.mp4"

captured: list = []


class _Intercept(sentry_sdk.transport.Transport):
    """Транспорт, который вместо сети складывает конверт себе."""

    def capture_envelope(self, envelope) -> None:
        captured.append(envelope)


def boom(chat_id: str, event: dict, bot_token: str) -> None:
    """Кадр с ПДн в локальных переменных — ровно то, что SDK шлёт по умолчанию."""
    telegram_chat_id = chat_id
    proxy_url = PROXY_URL
    mqtt_password = MQTT_PASS
    caption = event["caption"]
    sub_label = event["sub_label"]
    clip_path = event["clip_path"]
    # Сообщение ошибки, в которое мы сами подставили chat_id — так и есть в
    # mute_controller.py: f"Failed to send panel to chat {chat_id}".
    raise RuntimeError(
        f"Failed to send panel to chat {telegram_chat_id}: {caption[:20]}… "
        f"(bot {bot_token[:12]}…, clip {clip_path})"
    )


def main() -> int:
    sentry_init._enabled = False
    sentry_sdk.init(
        dsn="https://public@localhost/1",
        transport=_Intercept(),
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        max_breadcrumbs=0,
        traces_sample_rate=0,
        before_send=sentry_init._before_send,
    )

    # Крошка из лога — ещё один путь утечки: их пишут и чужие библиотеки
    # (python-telegram-bot / httpx кладут токен прямо в URL).
    sentry_sdk.add_breadcrumb(
        message=f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup via {PROXY_URL}"
    )

    frigate_event = {
        "id": "1752700000.123456-ab12cd",
        "camera": "yap_shlagbaum",
        "label": "person",
        "caption": CAPTION,
        "sub_label": SUB_LABEL,
        "recognized_license_plate": PLATE,
        "thumbnail": "/9j/4AAQSkZJRgABAQAAAQ" + "primankaBase64KadrSChelovekom",
        "clip_path": CLIP_PATH,
        "has_snapshot": True,
        "has_clip": True,
    }

    with sentry_sdk.new_scope() as scope:
        # Пользовательский контекст и теги — их проставляют «на всякий случай»,
        # и в них тоже утекает.
        scope.set_extra("group", {"telegram_chat_id": CHAT_ID, "proxy": PROXY_URL})
        scope.set_extra("event", frigate_event)
        scope.set_extra("mqtt", {"username": "frigate", "password": MQTT_PASS})
        scope.set_tag("bot_token", BOT_TOKEN)
        try:
            boom(CHAT_ID, frigate_event, BOT_TOKEN)
        except RuntimeError as e:
            sentry_sdk.capture_exception(e)

    sentry_sdk.flush(timeout=5)

    if not captured:
        print("! событие не доехало до транспорта — зачистка ломает событие,")
        print("  в трекер не долетит НИЧЕГО (запусти с debug=True и смотри трейс)")
        return 1

    # Разбираем конверт до сырого payload: ищем не в repr объектов, а в том,
    # что реально уйдёт по проводу.
    raw = ""
    for env in captured:
        for item in env.items:
            raw += repr(item.payload.json) + repr(item.payload.bytes or b"")

    print("\nУТЕЧКИ В GLITCHTIP")
    fails = 0
    for name, needle in (
        ("токен Telegram-бота", BOT_TOKEN),
        ("хвост токена бота", BOT_TOKEN.split(":")[1]),
        ("пароль proxy", PROXY_PASS),
        ("proxy-URL с кредами", PROXY_URL),
        ("пароль MQTT", MQTT_PASS),
        ("chat_id получателя", CHAT_ID),
        ("текст подписи в чат", CAPTION),
        ("распознанное имя (sub_label)", SUB_LABEL),
        ("распознанный автономер", PLATE),
        ("эскиз кадра с человеком", "primankaBase64KadrSChelovekom"),
        ("путь к записи с камеры", CLIP_PATH),
    ):
        leaked = needle in raw
        print(f"  [{'✗' if leaked else '✓'}] {name} {'УТЁК!' if leaked else 'не уехал'}")
        fails += leaked

    # Событие должно остаться полезным: без типа и места ошибки трекер бесполезен.
    useful = "RuntimeError" in raw and "glitchtip_check" in raw
    print(f"  [{'✓' if useful else '✗'}] тип и место ошибки на месте — трекер не ослеп")
    fails += not useful

    # Служебная структура цела — именно её ломала маска по подстроке.
    intact = "contexts" in raw and _MASK not in _contexts_of(raw)
    print(f"  [{'✓' if intact else '✗'}] служебные поля события не покалечены зачисткой")
    fails += not intact

    # Класс объекта и камера — не ПДн, а без них разбирать нечего.
    debuggable = "yap_shlagbaum" in raw and "person" in raw
    print(f"  [{'✓' if debuggable else '✗'}] камера и класс объекта не вычищены зря")
    fails += not debuggable

    print(f"\nGLITCHTIP: {'OK' if not fails else f'{fails} провалов'}")
    return 1 if fails else 0


def _contexts_of(raw: str) -> str:
    """Кусок вокруг contexts — чтобы поймать, если мы его замаскировали."""
    i = raw.find("'contexts'")
    return raw[i : i + 60] if i >= 0 else ""


if __name__ == "__main__":
    sys.exit(main())
