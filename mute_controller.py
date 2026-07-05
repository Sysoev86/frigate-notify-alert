#!/usr/bin/env python3
"""Mute Controller — in-chat pause buttons for notifications.

Why a separate process: all groups share one bot token, and only ONE consumer
per token may call getUpdates. So button handling lives here, while the
monitors just read the shared pause file (mute_state.json).

Each chat gets a single "panel" message whose reply keyboard matches the
current state (Telegram can't edit a reply keyboard, so the panel is resent
on every state change):
  - notifications ON:  "🔔 on"        + pause buttons only
  - paused:            "🔕 until X"   (pinned) + Resume button + pause buttons
A watcher removes the paused panel automatically when the pause expires.
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
from telegram.error import TelegramError
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

# Localized strings. Buttons are matched by their text, and both the keyboards
# and the matcher use the same table, so switching LANG keeps them consistent.
STRINGS = {
    "en": {
        "btn_15": "⏸ 15 min",
        "btn_1h": "⏸ 1 hour",
        "btn_3h": "⏸ 3 hours",
        "btn_morning": "⏸ Until morning",
        "btn_on": "▶️ Resume notifications",
        "panel_on": "🔔 Notifications on — pause buttons below 👇",
        "panel_muted": "🔕 «{name}»: notifications paused until {when}\n▶️ resume or extend with the buttons below",
        "disabled": "🎛 Pause controls disabled for this group.",
    },
    "ru": {
        "btn_15": "⏸ 15 мин",
        "btn_1h": "⏸ 1 час",
        "btn_3h": "⏸ 3 часа",
        "btn_morning": "⏸ До утра",
        "btn_on": "▶️ Включить уведомления",
        "panel_on": "🔔 Уведомления включены — кнопки паузы внизу 👇",
        "panel_muted": "🔕 «{name}»: уведомления на паузе до {when}\n▶️ включить или продлить — кнопками внизу",
        "disabled": "🎛 Пульт паузы отключён для этой группы.",
    },
}
S = STRINGS[LANG]

BTN_15 = S["btn_15"]
BTN_1H = S["btn_1h"]
BTN_3H = S["btn_3h"]
BTN_MORNING = S["btn_morning"]
BTN_ON = S["btn_on"]

FIXED_DURATIONS = {BTN_15: 15, BTN_1H: 60, BTN_3H: 180}

# Keyboards match the state: no "Resume" button while notifications are on.
KB_ON = ReplyKeyboardMarkup(
    [[BTN_15, BTN_1H], [BTN_3H, BTN_MORNING]],
    resize_keyboard=True,
    is_persistent=True,
)
KB_MUTED = ReplyKeyboardMarkup(
    [[BTN_ON], [BTN_15, BTN_1H], [BTN_3H, BTN_MORNING]],
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

        # chat_id -> {"panel": <msg id>, "state": "on"|"muted"}.
        # (Older versions stored {"kb": ..., "status": ...} — those legacy
        # messages are cleaned up on the first panel sync.)
        self.status = {}
        for chat_id, entry in _load_json(STATUS_FILE).items():
            self.status[chat_id] = entry if isinstance(entry, dict) else {}

    def _entry(self, chat_id: str) -> dict:
        return self.status.setdefault(chat_id, {})

    def _muted_until(self, group_id: str) -> float:
        return (_load_json(MUTE_STATE_FILE).get(group_id) or {}).get("muted_until", 0)

    def _set_mute(self, group_id: str, muted_until: float):
        state = _load_json(MUTE_STATE_FILE)
        state[group_id] = {"muted_until": muted_until}
        _save_json(MUTE_STATE_FILE, state)

    async def _delete_quietly(self, chat_id: str, message_id):
        if not message_id:
            return
        try:
            await self.bot.unpin_chat_message(chat_id=int(chat_id), message_id=message_id)
        except TelegramError:
            pass
        try:
            await self.bot.delete_message(chat_id=int(chat_id), message_id=message_id)
        except TelegramError:
            pass

    async def _sync_panel(self, chat_id: str, force: bool = False):
        """Bring the chat's panel in line with the actual mute state.

        Telegram reply keyboards can't be edited, so a state change means:
        delete the old panel message, send a new one with the right keyboard.
        Pinned only while paused (so "paused until X" is visible up top);
        while notifications are on there is nothing pinned.
        """
        group_id, group_name = self.chat_to_group[chat_id]
        until = self._muted_until(group_id)
        muted = bool(until and until > time.time())
        desired = "muted" if muted else "on"

        entry = self._entry(chat_id)
        if not force and entry.get("panel") and entry.get("state") == desired:
            return  # already correct

        # Remove the old panel and any legacy (pre-panel) messages
        for key in ("panel", "kb", "status"):
            await self._delete_quietly(chat_id, entry.pop(key, None))

        if muted:
            when = datetime.fromtimestamp(until).strftime("%H:%M (%d.%m)")
            text = S["panel_muted"].format(name=group_name, when=when)
            keyboard = KB_MUTED
        else:
            text = S["panel_on"]
            keyboard = KB_ON

        try:
            msg = await self.bot.send_message(
                chat_id=int(chat_id), text=text,
                reply_markup=keyboard, disable_notification=True,
            )
        except TelegramError as e:
            log.error(f"❌ Failed to send panel to chat {chat_id}: {e}")
            _save_json(STATUS_FILE, self.status)
            return

        entry["panel"] = msg.message_id
        entry["state"] = desired
        _save_json(STATUS_FILE, self.status)

        if muted:
            try:
                await self.bot.pin_chat_message(
                    chat_id=int(chat_id), message_id=msg.message_id, disable_notification=True
                )
            except TelegramError:
                pass  # bot isn't an admin — can't pin, panel is still visible

    async def _disable_chat(self, chat_id: str):
        """Remove the controls from a chat whose group disabled mute_controls."""
        entry = self.status.get(chat_id)
        if not entry:
            return  # already cleaned up / never had controls
        for key in ("panel", "kb", "status"):
            await self._delete_quietly(chat_id, entry.get(key))
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

    async def _handle_press(self, chat_id: str, text: str, message_id: int):
        group_id, _ = self.chat_to_group[chat_id]

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

        # force=True: even same-state presses (e.g. extending a pause) must
        # refresh the "until" time in the panel text
        await self._sync_panel(chat_id, force=True)
        # Delete the tap message to keep the chat clean (needs delete rights)
        try:
            await self.bot.delete_message(chat_id=int(chat_id), message_id=message_id)
        except TelegramError:
            pass

    async def _expire_check(self):
        """Flip panels back to the ON state once a pause expires — without
        waiting for a button press. Runs on every loop tick (~50 s)."""
        for chat_id, (group_id, _) in self.chat_to_group.items():
            entry = self.status.get(chat_id) or {}
            if entry.get("state") != "muted":
                continue
            until = self._muted_until(group_id)
            if not until or until <= time.time():
                log.info(f"⏰ {group_id}: pause expired — switching the panel back on")
                await self._sync_panel(chat_id)

    async def run(self):
        log.info(f"🚀 Mute Controller started. Chats: {list(self.chat_to_group.keys())}")
        # On start: remove controls from disabled groups...
        for chat_id in self.disabled_chats:
            await self._disable_chat(chat_id)
        # ...and sync panels for the active ones (also migrates legacy messages)
        for chat_id in self.chat_to_group:
            await self._sync_panel(chat_id)

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
                await self._expire_check()
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
