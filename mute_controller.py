#!/usr/bin/env python3
"""
Mute Controller — пульт паузы уведомлений в Telegram.

Зачем нужен отдельный процесс: у group1/group2 общий токен бота, а принимать
нажатия кнопок (getUpdates) может только ОДИН потребитель на токен. Поэтому приём
кнопок вынесен сюда, а мониторы только читают общий файл паузы (mute_state.json).

Что делает:
  - в каждом чате из GROUPS показывает persistent reply-клавиатуру (всегда внизу):
        ⏸ 15 мин | ⏸ 1 час | ⏸ 3 часа | ⏸ До утра | ▶️ Включить уведомления
  - по нажатию ставит/снимает паузу ТОЛЬКО для группы этого чата (пишет muted_until
    в mute_state.json), правит закреплённое сообщение-статус и удаляет сообщение-нажатие.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# --version: печатаем версию и выходим (до тяжёлых импортов и без config.py)
if "--version" in sys.argv:
    _d = os.path.dirname(os.path.abspath(__file__))
    try:
        print("frigate-notify-alert", open(os.path.join(_d, "VERSION")).read().strip())
    except OSError:
        print("frigate-notify-alert unknown")
    raise SystemExit(0)

from telegram import Bot, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest, TelegramError
from telegram.request import HTTPXRequest

try:
    from config import *  # TELEGRAM_BOT_TOKEN, TELEGRAM_PROXY_URL, GROUPS, [MUTE_STATE_FILE]
except ModuleNotFoundError as _e:
    if getattr(_e, "name", "") == "config":
        print("❌ Не найден config.py. Скопируй пример и заполни его:")
        print("   cp config.example.py config.py")
        raise SystemExit(1)
    raise

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MUTE_STATE_FILE = globals().get("MUTE_STATE_FILE") or os.path.join(_SCRIPT_DIR, "mute_state.json")
STATUS_FILE = os.path.join(_SCRIPT_DIR, "mute_controller_status.json")

# Тексты кнопок → длительность паузы в минутах. None = особые действия.
BTN_15 = "⏸ 15 мин"
BTN_1H = "⏸ 1 час"
BTN_3H = "⏸ 3 часа"
BTN_MORNING = "⏸ До утра"
BTN_ON = "▶️ Включить уведомления"

FIXED_DURATIONS = {BTN_15: 15, BTN_1H: 60, BTN_3H: 180}

KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_15, BTN_1H], [BTN_3H, BTN_MORNING], [BTN_ON]],
    resize_keyboard=True,
    is_persistent=True,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
log = logging.getLogger("mute_controller")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # атомарная замена, чтобы монитор не прочитал полу-файл


def _minutes_until_morning() -> int:
    now = datetime.now()
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, int((target - now).total_seconds() // 60))


def _status_text(group_name: str, muted_until: float) -> str:
    when = datetime.fromtimestamp(muted_until).strftime("%H:%M (%d.%m)")
    return f"🔕 «{group_name}»: уведомления на паузе до {when}\n▶️ включить — кнопкой внизу"


class MuteController:
    def __init__(self):
        request_kw = {"connect_timeout": 30, "read_timeout": 70, "write_timeout": 30}
        if TELEGRAM_PROXY_URL:
            request_kw["proxy"] = TELEGRAM_PROXY_URL
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN, request=HTTPXRequest(**request_kw))

        # chat_id (str) -> (group_id, group_name).
        # Пульт паузы показываем только для групп с mute_controls=True (по умолчанию вкл).
        # Отключённые чаты запоминаем отдельно — чтобы убрать у них клавиатуру.
        self.chat_to_group = {}
        self.disabled_chats = set()
        for gid, cfg in GROUPS.items():
            chat_id = str(cfg["telegram_chat_id"])
            if not cfg.get("mute_controls", True):
                log.info(f"⏭️ Группа {gid}: пульт паузы отключён (mute_controls=False)")
                self.disabled_chats.add(chat_id)
                continue
            self.chat_to_group[chat_id] = (gid, cfg.get("name", gid))

        if not self.chat_to_group:
            log.warning("⚠️ Ни в одной группе не включён mute_controls — пульт показывать негде.")

        # chat_id -> {"kb": <id клавиатуры>, "status": <id статуса>}.
        # Разделяем намеренно: сообщение с reply-клавиатурой Telegram редактировать
        # НЕ даёт («Message can't be edited»), поэтому клавиатуру шлём один раз
        # отдельно (она вечно висит внизу), а статус — обычным сообщением, которое
        # можно править на месте без пересылок и накопления пинов.
        self.status = {}
        for chat_id, entry in _load_json(STATUS_FILE).items():
            if isinstance(entry, dict):
                self.status[chat_id] = {"kb": entry.get("kb"), "status": entry.get("status")}
            else:  # старый формат (одно число) — сбрасываем, пересоздадим чисто
                self.status[chat_id] = {"kb": None, "status": None}

    def _entry(self, chat_id: str) -> dict:
        return self.status.setdefault(chat_id, {"kb": None, "status": None})

    def _muted_until(self, group_id: str) -> float:
        return (_load_json(MUTE_STATE_FILE).get(group_id) or {}).get("muted_until", 0)

    def _set_mute(self, group_id: str, muted_until: float):
        state = _load_json(MUTE_STATE_FILE)
        state[group_id] = {"muted_until": muted_until}
        _save_json(MUTE_STATE_FILE, state)

    async def _disable_chat(self, chat_id: str):
        """Убирает пульт у группы, где mute_controls выключили: снимает статус-пин,
        удаляет статус и прячет клавиатуру. Делается один раз (пока есть запись в
        status-файле), потом чат забываем."""
        entry = self.status.get(chat_id)
        if not entry:
            return  # уже почищено / пульта тут и не было
        sid = entry.get("status")
        if sid:
            try:
                await self.bot.unpin_chat_message(chat_id=int(chat_id), message_id=sid)
            except TelegramError:
                pass
            try:
                await self.bot.delete_message(chat_id=int(chat_id), message_id=sid)
            except TelegramError:
                pass
        try:
            await self.bot.send_message(
                chat_id=int(chat_id),
                text="🎛 Пульт паузы отключён для этой группы.",
                reply_markup=ReplyKeyboardRemove(),
            )
        except TelegramError as e:
            log.error(f"❌ Не удалось убрать клавиатуру в чате {chat_id}: {e}")
        del self.status[chat_id]
        _save_json(STATUS_FILE, self.status)
        log.info(f"🧹 Пульт убран из чата {chat_id}")

    async def _ensure_keyboard(self, chat_id: str):
        """Один раз показывает reply-клавиатуру (она остаётся внизу чата навсегда).
        На рестартах не шлём повторно — клавиатура в Telegram сохраняется сама."""
        entry = self._entry(chat_id)
        if entry.get("kb"):
            return
        try:
            msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text="🎛 Пульт паузы уведомлений — кнопки внизу 👇",
                reply_markup=KEYBOARD,
            )
            entry["kb"] = msg.message_id
            _save_json(STATUS_FILE, self.status)
        except TelegramError as e:
            log.error(f"❌ Не удалось показать клавиатуру в чате {chat_id}: {e}")

    async def _refresh_status(self, chat_id: str):
        """Статус-сообщение нужно ТОЛЬКО во время паузы («🔕 пауза до X», закреплено).
        Когда уведомления включены — статуса нет вообще (не занимаем экран): если он
        оставался с прошлой паузы, удаляем его."""
        group_id, group_name = self.chat_to_group[chat_id]
        until = self._muted_until(group_id)
        muted = bool(until and until > time.time())
        entry = self._entry(chat_id)
        sid = entry.get("status")

        # Уведомления включены — статус не нужен, убираем если оставался
        if not muted:
            if sid:
                try:
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=sid)
                except TelegramError:
                    pass
                entry["status"] = None
                _save_json(STATUS_FILE, self.status)
            return

        # Пауза активна — показать/обновить закреплённый статус
        text = _status_text(group_name, until)
        if sid:
            try:
                await self.bot.edit_message_text(chat_id=int(chat_id), message_id=sid, text=text)
                return  # правим на месте — без пересылок
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                # удалено/не редактируется — пересоздадим ниже
            except TelegramError:
                return

        try:
            msg = await self.bot.send_message(chat_id=int(chat_id), text=text)  # БЕЗ клавиатуры!
        except TelegramError as e:
            log.error(f"❌ Не удалось отправить статус в чате {chat_id}: {e}")
            return
        entry["status"] = msg.message_id
        _save_json(STATUS_FILE, self.status)
        try:
            await self.bot.pin_chat_message(
                chat_id=int(chat_id), message_id=msg.message_id, disable_notification=True
            )
        except TelegramError:
            pass  # бот не админ — не закрепим, статус всё равно виден

    async def _handle_press(self, chat_id: str, text: str, message_id: int):
        group_id, group_name = self.chat_to_group[chat_id]

        if text == BTN_ON:
            self._set_mute(group_id, 0)
            log.info(f"▶️ {group_id}: уведомления включены")
        elif text == BTN_MORNING:
            self._set_mute(group_id, time.time() + _minutes_until_morning() * 60)
            log.info(f"🔕 {group_id}: пауза до утра")
        elif text in FIXED_DURATIONS:
            minutes = FIXED_DURATIONS[text]
            self._set_mute(group_id, time.time() + minutes * 60)
            log.info(f"🔕 {group_id}: пауза на {minutes} мин")
        else:
            return  # не наша кнопка

        await self._refresh_status(chat_id)
        # убираем «нажатие» из чата, чтобы не мусорить (нужны права на удаление)
        try:
            await self.bot.delete_message(chat_id=int(chat_id), message_id=message_id)
        except TelegramError:
            pass

    async def run(self):
        log.info(f"🚀 Mute Controller запущен. Чаты: {list(self.chat_to_group.keys())}")
        # На старте: убрать пульт у отключённых групп...
        for chat_id in self.disabled_chats:
            await self._disable_chat(chat_id)
        # ...и гарантировать клавиатуру + актуальный статус у активных
        for chat_id in self.chat_to_group:
            await self._ensure_keyboard(chat_id)
            await self._refresh_status(chat_id)

        offset = None
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=offset, timeout=50, allowed_updates=["message"]
                )
            except TelegramError as e:
                log.warning(f"⚠️ getUpdates: {e}")
                await asyncio.sleep(5)
                continue

            for u in updates:
                offset = u.update_id + 1
                msg = u.message
                if not msg or not msg.text:
                    continue
                chat_id = str(msg.chat_id)
                if chat_id not in self.chat_to_group:
                    continue  # чужой чат
                try:
                    await self._handle_press(chat_id, msg.text.strip(), msg.message_id)
                except Exception as e:
                    log.error(f"❌ Ошибка обработки нажатия: {e}")


def main():
    controller = MuteController()
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n🛑 Остановлен")


if __name__ == "__main__":
    main()
