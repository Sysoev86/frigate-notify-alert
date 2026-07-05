#!/usr/bin/env python3
"""Mute Controller — in-chat pause buttons for notifications.

Why a separate process: all groups share one bot token, and only ONE consumer
per token may call getUpdates. So button handling lives here, while the
monitors just read the shared pause file (mute_state.json).

What it does:
  - keeps a persistent reply keyboard (always at the bottom) in every chat
    from GROUPS with mute_controls enabled:
        ⏸ 15 min | ⏸ 1 hour | ⏸ 3 hours | ⏸ Until morning | ▶️ Resume
  - on a tap, pauses/resumes ONLY that chat's group (writes muted_until into
    mute_state.json), maintains a pinned status message and deletes the tap.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# --version: print and exit before heavy imports (works without config.py)
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
    from config import *  # TELEGRAM_BOT_TOKEN, TELEGRAM_PROXY_URL, GROUPS, [MUTE_STATE_FILE, LANG]
except ModuleNotFoundError as _e:
    if getattr(_e, "name", "") == "config":
        print("config.py not found. Copy the example and fill it in:")
        print("   cp config.example.py config.py")
        raise SystemExit(1)
    raise

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MUTE_STATE_FILE = globals().get("MUTE_STATE_FILE") or os.path.join(_SCRIPT_DIR, "mute_state.json")
STATUS_FILE = os.path.join(_SCRIPT_DIR, "mute_controller_status.json")

# Interface language of the pause controller (button/status text). "en" or "ru".
LANG = str(globals().get("LANG") or "en").lower()
if LANG not in ("en", "ru"):
    LANG = "en"

# Localized strings. Buttons are matched by their text, and both the keyboard
# and the matcher use the same table, so switching LANG keeps them consistent.
STRINGS = {
    "en": {
        "btn_15": "⏸ 15 min",
        "btn_1h": "⏸ 1 hour",
        "btn_3h": "⏸ 3 hours",
        "btn_morning": "⏸ Until morning",
        "btn_on": "▶️ Resume notifications",
        "keyboard_prompt": "🎛 Notification pause — buttons below 👇",
        "disabled": "🎛 Pause controls disabled for this group.",
        "status_paused": "🔕 «{name}»: notifications paused until {when}\n▶️ resume with the button below",
    },
    "ru": {
        "btn_15": "⏸ 15 мин",
        "btn_1h": "⏸ 1 час",
        "btn_3h": "⏸ 3 часа",
        "btn_morning": "⏸ До утра",
        "btn_on": "▶️ Включить уведомления",
        "keyboard_prompt": "🎛 Пульт паузы уведомлений — кнопки внизу 👇",
        "disabled": "🎛 Пульт паузы отключён для этой группы.",
        "status_paused": "🔕 «{name}»: уведомления на паузе до {when}\n▶️ включить — кнопкой внизу",
    },
}
S = STRINGS[LANG]

BTN_15 = S["btn_15"]
BTN_1H = S["btn_1h"]
BTN_3H = S["btn_3h"]
BTN_MORNING = S["btn_morning"]
BTN_ON = S["btn_on"]

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
    os.replace(tmp, path)  # atomic swap so a monitor never reads a half-file


def _minutes_until_morning() -> int:
    now = datetime.now()
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, int((target - now).total_seconds() // 60))


def _status_text(group_name: str, muted_until: float) -> str:
    when = datetime.fromtimestamp(muted_until).strftime("%H:%M (%d.%m)")
    return S["status_paused"].format(name=group_name, when=when)


class MuteController:
    def __init__(self):
        request_kw = {"connect_timeout": 30, "read_timeout": 70, "write_timeout": 30}
        if TELEGRAM_PROXY_URL:
            request_kw["proxy"] = TELEGRAM_PROXY_URL
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN, request=HTTPXRequest(**request_kw))

        # chat_id (str) -> (group_id, group_name).
        # Controls appear only for groups with mute_controls=True (the default).
        # Disabled chats are remembered so we can remove their keyboard once.
        self.chat_to_group = {}
        self.disabled_chats = set()
        for gid, cfg in GROUPS.items():
            chat_id = str(cfg["telegram_chat_id"])
            if not cfg.get("mute_controls", True):
                log.info(f"⏭️ Group {gid}: pause controls disabled (mute_controls=False)")
                self.disabled_chats.add(chat_id)
                continue
            self.chat_to_group[chat_id] = (gid, cfg.get("name", gid))

        if not self.chat_to_group:
            log.warning("⚠️ mute_controls is disabled for every group — nowhere to show controls.")

        # chat_id -> {"kb": <keyboard msg id>, "status": <status msg id>}.
        # Deliberately separate: Telegram refuses to edit a message that carries
        # a reply keyboard ("Message can't be edited"), so the keyboard is sent
        # once (it sticks to the bottom forever) and the status is a plain
        # message we can edit in place — no resends, no piling pins.
        self.status = {}
        for chat_id, entry in _load_json(STATUS_FILE).items():
            if isinstance(entry, dict):
                self.status[chat_id] = {"kb": entry.get("kb"), "status": entry.get("status")}
            else:  # legacy format (bare int) — reset, will be recreated cleanly
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
        """Remove the controls from a chat whose group disabled mute_controls:
        unpin + delete the status and hide the keyboard. Runs once (while the
        chat is still present in the status file), then the chat is forgotten."""
        entry = self.status.get(chat_id)
        if not entry:
            return  # already cleaned up / never had controls
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
                text=S["disabled"],
                reply_markup=ReplyKeyboardRemove(),
            )
        except TelegramError as e:
            log.error(f"❌ Failed to remove keyboard in chat {chat_id}: {e}")
        del self.status[chat_id]
        _save_json(STATUS_FILE, self.status)
        log.info(f"🧹 Controls removed from chat {chat_id}")

    async def _ensure_keyboard(self, chat_id: str):
        """Show the reply keyboard once (it stays at the bottom of the chat).
        Not resent on restarts — Telegram keeps the keyboard on its own."""
        entry = self._entry(chat_id)
        if entry.get("kb"):
            return
        try:
            msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text=S["keyboard_prompt"],
                reply_markup=KEYBOARD,
            )
            entry["kb"] = msg.message_id
            _save_json(STATUS_FILE, self.status)
        except TelegramError as e:
            log.error(f"❌ Failed to show keyboard in chat {chat_id}: {e}")

    async def _refresh_status(self, chat_id: str):
        """The status message exists ONLY while paused ("🔕 paused until X",
        pinned). When notifications are on there is no status at all (don't
        waste screen space): if one is left over from a past pause, delete it."""
        group_id, group_name = self.chat_to_group[chat_id]
        until = self._muted_until(group_id)
        muted = bool(until and until > time.time())
        entry = self._entry(chat_id)
        sid = entry.get("status")

        # Notifications on — no status needed, remove a leftover if any
        if not muted:
            if sid:
                try:
                    await self.bot.unpin_chat_message(chat_id=int(chat_id), message_id=sid)
                except TelegramError:
                    pass
                try:
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=sid)
                except TelegramError:
                    pass
                entry["status"] = None
                _save_json(STATUS_FILE, self.status)
            return

        # Pause active — show/update the pinned status
        text = _status_text(group_name, until)
        if sid:
            try:
                await self.bot.edit_message_text(chat_id=int(chat_id), message_id=sid, text=text)
                return  # edited in place — no resends
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                # deleted / not editable — recreate below
            except TelegramError:
                return

        try:
            msg = await self.bot.send_message(chat_id=int(chat_id), text=text)  # NO keyboard!
        except TelegramError as e:
            log.error(f"❌ Failed to send status in chat {chat_id}: {e}")
            return
        entry["status"] = msg.message_id
        _save_json(STATUS_FILE, self.status)
        try:
            await self.bot.pin_chat_message(
                chat_id=int(chat_id), message_id=msg.message_id, disable_notification=True
            )
        except TelegramError:
            pass  # bot isn't an admin — can't pin, status is still visible

    async def _handle_press(self, chat_id: str, text: str, message_id: int):
        group_id, group_name = self.chat_to_group[chat_id]

        if text == BTN_ON:
            self._set_mute(group_id, 0)
            log.info(f"▶️ {group_id}: notifications resumed")
        elif text == BTN_MORNING:
            self._set_mute(group_id, time.time() + _minutes_until_morning() * 60)
            log.info(f"🔕 {group_id}: paused until morning")
        elif text in FIXED_DURATIONS:
            minutes = FIXED_DURATIONS[text]
            self._set_mute(group_id, time.time() + minutes * 60)
            log.info(f"🔕 {group_id}: paused for {minutes} min")
        else:
            return  # not one of our buttons

        await self._refresh_status(chat_id)
        # Delete the tap message to keep the chat clean (needs delete rights)
        try:
            await self.bot.delete_message(chat_id=int(chat_id), message_id=message_id)
        except TelegramError:
            pass

    async def _expire_stale_statuses(self):
        """Remove the pinned pause status once the pause has expired — without
        waiting for a button press. Runs on every loop tick (~50 s), so the pin
        disappears within a minute of the pause ending."""
        for chat_id, (group_id, _) in self.chat_to_group.items():
            entry = self.status.get(chat_id) or {}
            if not entry.get("status"):
                continue  # nothing pinned for this chat
            until = self._muted_until(group_id)
            if not until or until <= time.time():
                log.info(f"⏰ {group_id}: pause expired — removing the pinned status")
                await self._refresh_status(chat_id)

    async def run(self):
        log.info(f"🚀 Mute Controller started. Chats: {list(self.chat_to_group.keys())}")
        # On start: remove controls from disabled groups...
        for chat_id in self.disabled_chats:
            await self._disable_chat(chat_id)
        # ...and ensure keyboard + up-to-date status for the active ones
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

            try:
                await self._expire_stale_statuses()
            except Exception as e:
                log.warning(f"⚠️ expire check: {e}")

            for u in updates:
                offset = u.update_id + 1
                msg = u.message
                if not msg or not msg.text:
                    continue
                chat_id = str(msg.chat_id)
                if chat_id not in self.chat_to_group:
                    continue  # not one of our chats
                try:
                    await self._handle_press(chat_id, msg.text.strip(), msg.message_id)
                except Exception as e:
                    log.error(f"❌ Error handling button press: {e}")


def config_errors() -> list:
    """Static config validation (clear startup error instead of a traceback)."""
    errors = []

    def placeholder(v):
        return isinstance(v, str) and ("SET_ME" in v or "ВСТАВЬ" in v)

    groups = globals().get("GROUPS")
    if not isinstance(groups, dict) or not groups:
        return ["GROUPS is missing or empty"]
    token = globals().get("TELEGRAM_BOT_TOKEN")
    if not token or placeholder(token):
        errors.append("TELEGRAM_BOT_TOKEN is not filled in (placeholder left)")
    for gid, g in groups.items():
        cid = str((g or {}).get("telegram_chat_id") or "")
        if not cid or placeholder(cid):
            errors.append(f"GROUPS['{gid}'].telegram_chat_id is not filled in")
    return errors


def main():
    errs = config_errors()
    if errs:
        print("❌ config.py has problems:")
        for e in errs:
            print(f"   - {e}")
        print("Fix config.py (see config.example.py) or run: ./manage.sh doctor")
        sys.exit(1)

    controller = MuteController()
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")


if __name__ == "__main__":
    main()
