#!/usr/bin/env python3
"""Frigate -> Telegram monitor.

Watches Frigate events (MQTT for real-time + API polling as a safety net) and
sends the event's snapshot + clip to a Telegram chat as a media group.
Supports multiple camera groups, each with its own chat (see config.py).

Run: python frigate_telegram_monitor.py <group_id>
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time

# --version: print and exit before heavy imports (works without config.py)
if "--version" in sys.argv:
    _d = os.path.dirname(os.path.abspath(__file__))
    try:
        print("frigate-notify-alert", open(os.path.join(_d, "VERSION")).read().strip())
    except OSError:
        print("frigate-notify-alert unknown")
    raise SystemExit(0)

from typing import Any, Dict, Optional

import aiohttp
import paho.mqtt.client as mqtt
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError, TimedOut, NetworkError
from telegram.request import HTTPXRequest

import sentry_init

try:
    from config import *
except ModuleNotFoundError as _e:
    if getattr(_e, "name", "") == "config":
        print("config.py not found. Copy the example and fill it in:")
        print("   cp config.example.py config.py")
        raise SystemExit(1)
    raise

# --- Optional config values (defaults keep previous behavior) --------------
# `from config import *` puts user settings into this module's globals; for
# anything the user omitted we fall back to a sane default here, so old
# configs keep working and every documented option actually has an effect.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OBJECTS = list(globals().get("OBJECTS") or
               ["person", "car", "truck", "bus", "motorcycle", "bicycle"])
LOG_LEVEL = str(globals().get("LOG_LEVEL") or "INFO").upper()
LOG_FORMAT = str(globals().get("LOG_FORMAT") or "%(asctime)s - %(levelname)s - %(message)s")
STATS_INTERVAL = int(globals().get("STATS_INTERVAL") or 60)
MEDIA_RETRY_ATTEMPTS = int(globals().get("MEDIA_RETRY_ATTEMPTS") or 15)
MEDIA_RETRY_DELAY = int(globals().get("MEDIA_RETRY_DELAY") or 3)
# A single transient timeout to api.telegram.org used to drop the whole alert.
# Retry the send a few times with growing backoff before giving up.
TELEGRAM_SEND_RETRIES = int(globals().get("TELEGRAM_SEND_RETRIES") or 3)
TELEGRAM_SEND_BACKOFF = int(globals().get("TELEGRAM_SEND_BACKOFF") or 5)  # sec, ×attempt
# Alarm feature: after this many seconds without any event, the FIRST event that
# breaks the silence gets an instant text heads-up — sent before the photo+video
# (which take a few seconds to finalize), so a possible unauthorized entry is
# flagged as fast as possible. 0 = off (nothing changes for existing installs).
IDLE_ALERT_AFTER = int(globals().get("IDLE_ALERT_AFTER") or 0)
# Shared pause-state file written by mute_controller, read by the monitors.
MUTE_STATE_FILE = globals().get("MUTE_STATE_FILE") or os.path.join(_SCRIPT_DIR, "mute_state.json")
# Language of chat-facing texts (startup notice); logs are always English.
LANG = str(globals().get("LANG") or "en").lower()
if LANG not in ("en", "ru"):
    LANG = "en"


def _version() -> str:
    try:
        return open(os.path.join(_SCRIPT_DIR, "VERSION")).read().strip()
    except OSError:
        return "?"


POLL_INTERVAL = 3          # seconds between /api/events polls
MIN_MEDIA_BYTES = 1000     # smaller responses are treated as "not ready yet"
QUEUE_MAX_RETRIES = 10     # polls to wait for media before dropping an event

# Telegram bots can't upload files over 50 MB; long events (a person working in
# the room for an hour) produce clips of hundreds of MB — downloading those into
# RAM got real installs OOM-killed. Cap the clip: oversized events fall back to
# a trimmed clip, then a short text note. Override with MAX_CLIP_MB in config.py.
MAX_CLIP_MB = int(globals().get("MAX_CLIP_MB") or 45)
TOO_BIG = object()  # sentinel returned by _download when the cap is exceeded
PROBE_SECONDS = 10   # short slice used to measure the clip's real bitrate
MAX_TRIM_SECONDS = 300  # never send more than 5 minutes of a trimmed clip


import config_check  # noqa: E402  (must come after the config import above)


class FrigateTelegramMonitor:
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.group_config = GROUPS[group_id]

        # Events that finished BEFORE this moment are history from /api/events
        # (the processed set starts empty) — never send them.
        self.startup_ts = time.time()

        self.logger = self._setup_logging()

        self.stats = {
            "start_time": time.time(),
            "events_processed": 0,
            "telegram_sent": 0,
            "errors": 0,
        }

        # Why the last media download failed — surfaced in the drop/fallback log
        # so "clip never arrived" tells us HTTP 404 vs timeout vs half-written.
        self._last_dl_reason: Optional[str] = None

        # Telegram bot (generous timeouts; optional proxy for blocked ISPs)
        request_kw: dict = {
            "connect_timeout": 30,
            "read_timeout": 120,
            "write_timeout": 90,
            "media_write_timeout": 180,
        }
        if TELEGRAM_PROXY_URL:
            request_kw["proxy"] = TELEGRAM_PROXY_URL
            self.logger.info(f"📡 Telegram via proxy: {TELEGRAM_PROXY_URL.split('@')[-1]}")
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN, request=HTTPXRequest(**request_kw))

        # Cameras / tracked objects; a group may override the global OBJECTS
        self.cameras = self.group_config["cameras"]
        self.objects = list(self.group_config.get("objects") or OBJECTS)

        # Silent delivery (no sound/vibration on the phone). Default True —
        # set "silent": False on a group to get full loud notifications.
        self.silent = bool(self.group_config.get("silent", True))

        # One quiet "monitoring started" message on each start — doubles as a
        # built-in bot/chat wiring test. Disable with "startup_message": False.
        self.startup_message = bool(self.group_config.get("startup_message", True))

        # Optional Frigate zone filter: if set, notify only when the object
        # entered at least one of these zones. Empty/missing = whole camera.
        self.zones = self.group_config.get("zones") or []
        if self.zones:
            self.logger.info(f"🧭 Zone filter enabled: {', '.join(self.zones)}")

        # Dedup: IDs of already-handled events (bounded so memory stays flat)
        self.processed_events = set()
        self.max_processed_events = 1000

        # Events whose media isn't ready yet: {event_id: (event, retry_count)}
        self.retry_events: Dict[str, tuple] = {}

        # Alarm (IDLE_ALERT_AFTER): when the last qualifying event happened, and
        # the ids we've already sent an idle-break heads-up for. Starts "now" so
        # a fresh boot doesn't fire on its very first event.
        self._last_event_ts = self.startup_ts
        self._idle_notified: set = set()

        # Real-time path
        self.mqtt_client = None
        self.event_loop = None
        self.mqtt_event_queue: Optional[asyncio.Queue] = None

        # One HTTP session for all Frigate requests (created in start_monitoring)
        self.http: Optional[aiohttp.ClientSession] = None

        self.logger.info(f"🚀 Monitor initialized for group: {self.group_config['name']}")

    # ------------------------------------------------------------------ setup

    def _setup_logging(self) -> logging.Logger:
        # httpx logs full request URLs including the bot token — keep it quiet
        logging.getLogger("httpx").setLevel(logging.WARNING)

        logger = logging.getLogger(f"frigate_monitor_{self.group_id}")
        logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        if logger.handlers:  # don't stack handlers on re-init
            return logger

        formatter = logging.Formatter(LOG_FORMAT, datefmt="%d.%m.%Y %H:%M:%S")

        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

        file_handler = logging.FileHandler(
            os.path.join(_SCRIPT_DIR, f"frigate_monitor_{self.group_id}.log")
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        return logger

    # ---------------------------------------------------------------- filters

    def _zone_ok(self, event: Dict[str, Any]) -> bool:
        """True if no zone filter is set OR the object entered a wanted zone.

        Frigate exposes zones under different keys: `entered_zones` in MQTT
        payloads, `zones` in /api/events — check the union so both paths agree.
        """
        if not self.zones:
            return True
        entered = set(event.get("entered_zones") or []) | set(event.get("zones") or [])
        return bool(entered & set(self.zones))

    def _is_muted(self) -> bool:
        """Whether this group is currently paused via the Telegram buttons.

        The mute_controller writes {"<group>": {"muted_until": <epoch>}} —
        missing file/key means not muted.
        """
        try:
            with open(MUTE_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        until = (state.get(self.group_id) or {}).get("muted_until", 0)
        return bool(until and until > time.time())

    # ------------------------------------------------------------- API polling

    async def _check_frigate_events(self):
        """Poll /api/events: pick up finished events and drive the retry queue."""
        try:
            async with self.http.get(f"{FRIGATE_URL}/api/events") as response:
                if response.status != 200:
                    self.logger.warning(f"⚠️ /api/events returned HTTP {response.status}")
                    return
                events = await response.json()
        except Exception as e:
            self.logger.error(f"❌ Failed to poll Frigate events: {e}")
            return

        ours = [e for e in events if e.get("camera") in self.cameras]
        self.logger.debug(f"📊 API events: {len(events)} total, {len(ours)} for our cameras")

        counters = {
            "new": 0, "already": 0, "no_end_time": 0, "other_camera": 0,
            "other_object": 0, "other_zone": 0, "old": 0, "media_timeout": 0,
        }

        # Newest first
        events_sorted = sorted(events, key=lambda x: x.get("end_time", 0) or 0, reverse=True)

        for event in events_sorted:
            event_id = event.get("id")
            camera = event.get("camera")
            object_type = event.get("label")
            end_time = event.get("end_time")
            has_snapshot = event.get("has_snapshot", False)
            has_clip = event.get("has_clip", False)

            if not end_time:
                counters["no_end_time"] += 1
                continue  # still in progress
            if event_id in self.processed_events:
                counters["already"] += 1
                continue
            if camera not in self.cameras:
                counters["other_camera"] += 1
                continue
            if object_type not in self.objects:
                counters["other_object"] += 1
                self.logger.debug(f"⏭️ {event_id}: '{object_type}' not tracked, skipping")
                continue

            # Finished before we started -> history, mark and never send
            if end_time < self.startup_ts:
                self.processed_events.add(event_id)
                counters["old"] += 1
                continue

            if not self._zone_ok(event):
                counters["other_zone"] += 1
                self.logger.debug(
                    f"⏭️ {event_id}: outside wanted zones {self.zones} "
                    f"(was in {event.get('entered_zones') or event.get('zones') or []})"
                )
                continue

            # Alarm: first event after a long quiet spell -> instant text now,
            # before we wait on the media (fires once per id across both paths).
            await self._maybe_idle_alert(event)

            # Media not ready yet -> park it in the retry queue
            if not has_snapshot or not has_clip:
                if event_id not in self.retry_events:
                    self.retry_events[event_id] = (event, 0)
                    self.logger.info(
                        f"⏳ {event_id} ({object_type}@{camera}): media not ready "
                        f"(snapshot={has_snapshot}, clip={has_clip}), queued for retry"
                    )
                else:
                    _, retry_count = self.retry_events[event_id]
                    self.retry_events[event_id] = (event, retry_count + 1)
                    if retry_count + 1 >= QUEUE_MAX_RETRIES:
                        self.logger.warning(
                            f"❌ {event_id} ({object_type}@{camera}): media never appeared "
                            f"after {QUEUE_MAX_RETRIES} polls, dropping"
                        )
                        del self.retry_events[event_id]
                        counters["media_timeout"] += 1
                continue

            if event_id in self.retry_events:
                _, retry_count = self.retry_events.pop(event_id)
                self.logger.info(f"✅ {event_id}: media appeared after {retry_count + 1} polls")

            self.logger.info(
                f"🎯 Detected: {object_type} on {camera} "
                f"(snapshot={has_snapshot}, clip={has_clip}, end_time={end_time})"
            )
            await self._process_frigate_event(event, source="api-poll")

            self.processed_events.add(event_id)
            self._trim_processed()
            counters["new"] += 1

        await self._check_retry_queue({e.get("id") for e in events})

        summary = (
            f"📊 Poll: new={counters['new']}, already={counters['already']}, "
            f"in_progress={counters['no_end_time']}, other_camera={counters['other_camera']}, "
            f"other_object={counters['other_object']}, other_zone={counters['other_zone']}, "
            f"old={counters['old']}, media_timeout={counters['media_timeout']}, "
            f"known_ids={len(self.processed_events)}, retry_queue={len(self.retry_events)}"
        )
        # INFO only when something was actually sent; routine polls stay at DEBUG
        self.logger.log(logging.INFO if counters["new"] else logging.DEBUG, summary)

    async def _check_retry_queue(self, ids_in_api: set):
        """Re-check queued events that dropped out of the /api/events list."""
        now = time.time()
        to_remove = []

        for event_id, (event, retry_count) in list(self.retry_events.items()):
            if event_id in ids_in_api:
                continue  # still in the list, main loop handles it

            try:
                async with self.http.get(
                    f"{FRIGATE_URL}/api/events/{event_id}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status == 200:
                        updated = await response.json()
                        if updated.get("has_snapshot") and updated.get("has_clip"):
                            self.logger.info(
                                f"✅ {event_id}: found via direct URL, media ready "
                                f"(poll {retry_count + 1})"
                            )
                            await self._process_frigate_event(updated, source="api-retry")
                            del self.retry_events[event_id]
                        else:
                            self.retry_events[event_id] = (updated, retry_count + 1)
                            if retry_count + 1 >= QUEUE_MAX_RETRIES:
                                to_remove.append(event_id)
                    elif response.status == 404:
                        # Gone from the API; give up 5 minutes after it ended
                        if (event.get("end_time") or 0) and now - event["end_time"] > 300:
                            to_remove.append(event_id)
                    else:
                        self.retry_events[event_id] = (event, retry_count + 1)
                        if retry_count + 1 >= QUEUE_MAX_RETRIES:
                            to_remove.append(event_id)
            except Exception as e:
                self.logger.debug(f"⚠️ Direct check failed for {event_id}: {e}")
                self.retry_events[event_id] = (event, retry_count + 1)
                if retry_count + 1 >= QUEUE_MAX_RETRIES:
                    to_remove.append(event_id)

        for event_id in to_remove:
            _, retry_count = self.retry_events.pop(event_id)
            self.logger.warning(
                f"🗑️ {event_id}: dropped from retry queue "
                f"(gone from API or retry limit hit after {retry_count} polls)"
            )

    def _trim_processed(self):
        """Keep the dedup set bounded (drop the oldest IDs)."""
        if len(self.processed_events) > self.max_processed_events:
            overflow = len(self.processed_events) - self.max_processed_events + 100
            for old_id in sorted(self.processed_events)[:overflow]:
                self.processed_events.discard(old_id)

    # -------------------------------------------------------------- idle alarm

    def _fmt_gap(self, seconds: float) -> str:
        """Human-readable idle duration for the alarm text."""
        total_m = int(seconds // 60)
        h, m = divmod(total_m, 60)
        if LANG == "ru":
            if h and m:
                return f"{h} ч {m} мин"
            return f"{h} ч" if h else f"{m} мин"
        if h and m:
            return f"{h} h {m} min"
        return f"{h} h" if h else f"{m} min"

    async def _maybe_idle_alert(self, event: Dict[str, Any]):
        """Alarm heads-up: the first event after a long quiet spell gets an
        instant text — sent BEFORE the photo+video album (which needs a few
        seconds to finalize), so a possible unauthorized entry is flagged as
        fast as possible.

        No-op unless IDLE_ALERT_AFTER is set. Fires at most once per event, from
        whichever path (MQTT or polling) reaches it first. The idle clock is
        updated for every qualifying event — including muted ones (activity is
        activity) — so the gap always reflects the real quiet period."""
        if not IDLE_ALERT_AFTER:
            return
        event_id = event.get("id")
        if not event_id or event_id in self._idle_notified:
            return
        end_time = event.get("end_time")
        if end_time and end_time < self.startup_ts:
            return  # history from before we started — not a live event

        self._idle_notified.add(event_id)
        if len(self._idle_notified) > self.max_processed_events:
            for old in sorted(self._idle_notified)[:200]:
                self._idle_notified.discard(old)

        gap = time.time() - self._last_event_ts
        self._last_event_ts = time.time()
        if gap < IDLE_ALERT_AFTER or self._is_muted():
            return

        obj = event.get("label") or ("объект" if LANG == "ru" else "object")
        cam = event.get("camera")
        where = f" @ {cam}" if cam else ""
        if LANG == "ru":
            text = (f"🚨 Движение после тишины ({self._fmt_gap(gap)}): {obj}{where}. "
                    f"Фото и видео сейчас придут.")
        else:
            text = (f"🚨 Motion after {self._fmt_gap(gap)} of quiet: {obj}{where}. "
                    f"Photo and video to follow.")
        try:
            await self.bot.send_message(
                chat_id=self.group_config["telegram_chat_id"],
                text=text,
                disable_notification=False,  # the alarm is meant to be noticed
            )
            self.logger.info(f"🚨 Idle-break alert sent (quiet {self._fmt_gap(gap)})")
        except TelegramError as e:
            self.logger.error(f"❌ Telegram error (idle alert): {e}")

    # ---------------------------------------------------------------- sending

    async def _process_frigate_event(self, event: Dict[str, Any], source: str = "?"):
        """Send one finished event to Telegram (photo + clip)."""
        try:
            event_id = event.get("id")
            camera = event.get("camera")
            object_type = event.get("label")

            if self._is_muted():
                self.logger.info(
                    f"🔕 Pause active for {self.group_id} — not sending "
                    f"{event_id} ({object_type}@{camera})"
                )
                return

            # Diagnostics: event length and how long after its end we send.
            # A clip much shorter than `duration` means Frigate was still
            # finalizing the recording.
            start_time = event.get("start_time")
            end_time = event.get("end_time")
            duration = round(end_time - start_time, 1) if (start_time and end_time) else "?"
            since_end = round(time.time() - end_time, 1) if end_time else "?"
            self.logger.info(
                f"🎬 Event {event_id}: {object_type}@{camera} | source={source} | "
                f"duration={duration}s | since_end={since_end}s | "
                f"snapshot={event.get('has_snapshot')} clip={event.get('has_clip')}"
            )

            success = await self._send_event_media(event)
            if success:
                self.logger.info(f"✅ Media sent for event {event_id}")
                self.stats["telegram_sent"] += 1
            else:
                self.logger.error(f"❌ Failed to send media for event {event_id}")
                self.stats["errors"] += 1

            self.stats["events_processed"] += 1

        except Exception as e:
            self.logger.error(f"❌ Error processing event: {e}")
            self.stats["errors"] += 1

    async def _download(self, url: str, timeout_s: int, label: str, max_bytes: int = 0):
        """Fetch a media file; None if unavailable or suspiciously small.

        With max_bytes set, returns the TOO_BIG sentinel instead of loading an
        oversized file into RAM (checked via Content-Length first, then while
        streaming — a 1 GB clip must never end up in memory)."""
        try:
            async with self.http.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as response:
                if response.status != 200:
                    self._last_dl_reason = f"HTTP {response.status}"
                    self.logger.debug(f"{label}: HTTP {response.status}")
                    return None
                if max_bytes and response.content_length and response.content_length > max_bytes:
                    self.logger.warning(
                        f"{label}: {response.content_length / 1024 / 1024:.0f} MB exceeds "
                        f"the {MAX_CLIP_MB} MB limit — not downloading"
                    )
                    return TOO_BIG
                data = bytearray()
                async for chunk in response.content.iter_chunked(1 << 20):
                    data.extend(chunk)
                    if max_bytes and len(data) > max_bytes:
                        self.logger.warning(
                            f"{label}: exceeded the {MAX_CLIP_MB} MB limit while "
                            f"downloading — aborted"
                        )
                        return TOO_BIG
                if len(data) < MIN_MEDIA_BYTES:
                    self._last_dl_reason = f"only {len(data)}B (<{MIN_MEDIA_BYTES})"
                    self.logger.debug(f"{label}: too small ({len(data)} bytes), not ready")
                    return None
                self._last_dl_reason = None
                return bytes(data)
        except Exception as e:
            self._last_dl_reason = f"{type(e).__name__}: {e}"
            self.logger.debug(f"{label}: download error: {e}")
            return None

    async def _send_event_media(self, event: Dict[str, Any]) -> bool:
        """Download snapshot + clip (retrying while Frigate finalizes them),
        then send both as one media group. Each file is downloaded once.

        An oversized clip (Telegram bots can't upload > 50 MB; hour-long events
        used to OOM the process) degrades to a TRIMMED clip — the first N
        seconds fetched via Frigate's time-range recordings API — so the chat
        still gets the usual compact photo+video album, just with a caption.
        A short text notice is the very last resort — never a lone photo, which
        Telegram would render oversized."""
        event_id = event.get("id")
        photo_url = f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg?crop=1"
        video_url = f"{FRIGATE_URL}/api/events/{event_id}/clip.mp4"
        cap = MAX_CLIP_MB * 1024 * 1024

        photo_data = video_data = None
        caption = None

        for attempt in range(1, MEDIA_RETRY_ATTEMPTS + 1):
            if photo_data is None:
                photo_data = await self._download(photo_url, 45, f"📸 snapshot {event_id}")
            if video_data is None:
                v = await self._download(video_url, 90, f"🎥 clip {event_id}", max_bytes=cap)
                if v is TOO_BIG:
                    # Full clip won't fit — send the first N seconds instead
                    v, _secs, caption = await self._download_trimmed_clip(event, cap)
                    video_data = v if v is not None else TOO_BIG  # TOO_BIG = give up on video
                elif v is not None:
                    video_data = v

            if photo_data is not None and isinstance(video_data, bytes):
                self.logger.info(
                    f"🎥 Clip ready: {len(video_data) / 1024 / 1024:.2f} MB"
                    f"{' (trimmed)' if caption else ''} | "
                    f"snapshot {len(photo_data)} bytes (attempt {attempt})"
                )
                return await self._send_media_group(photo_data, video_data, caption)

            if photo_data is not None and video_data is TOO_BIG:
                return await self._send_text_notice(event, reason="too_large")

            self.logger.debug(
                f"⏳ {event_id}: media not ready (attempt {attempt}/{MEDIA_RETRY_ATTEMPTS}, "
                f"photo={'ok' if photo_data else 'no'}, video={'ok' if video_data else 'no'})"
            )
            if attempt < MEDIA_RETRY_ATTEMPTS:
                await asyncio.sleep(MEDIA_RETRY_DELAY)

        # The snapshot arrived but Frigate never served the clip within the
        # window. Don't drop the whole alert — but don't send a lone photo
        # either (a single item renders oversized in Telegram); a compact text
        # heads-up keeps the alert without the ugly full-size image.
        if isinstance(photo_data, bytes):
            self.logger.warning(
                f"🎥 {event_id}: clip unavailable after {MEDIA_RETRY_ATTEMPTS} "
                f"attempts (last: {self._last_dl_reason}) — text notice only"
            )
            return await self._send_text_notice(event, reason="clip_unavailable")

        self.logger.error(
            f"❌ Media never became available for {event_id} after "
            f"{MEDIA_RETRY_ATTEMPTS} attempts "
            f"(photo={'ok' if photo_data else 'no'}, "
            f"clip={'ok' if isinstance(video_data, bytes) else 'no'}, "
            f"last: {self._last_dl_reason})"
        )
        return False

    async def _download_trimmed_clip(self, event: Dict[str, Any], cap: int):
        """First-N-seconds fallback for oversized clips, via Frigate's
        time-range recordings API (/api/<camera>/start/<t1>/end/<t2>/clip.mp4).

        Frigate streams clips without Content-Length, so the bitrate can't be
        known upfront: fetch a short probe, measure bytes/second from it, then
        ask for exactly as many seconds as fit the cap. Two requests, no
        guessing. Returns (bytes, seconds, caption) or (None, 0, None) if the
        range API gave nothing usable."""
        camera = event.get("camera")
        start = event.get("start_time")
        end = event.get("end_time")
        event_id = event.get("id")
        if not (camera and start and end):
            return None, 0, None

        duration = max(1, int(end - start))

        def range_url(secs: int) -> str:
            return (f"{FRIGATE_URL}/api/{camera}/start/{int(start)}"
                    f"/end/{int(start) + secs}/clip.mp4")

        probe_secs = min(PROBE_SECONDS, duration)
        probe = await self._download(range_url(probe_secs), 60,
                                     f"🎥 probe {event_id} ({probe_secs}s)", max_bytes=cap)
        if not isinstance(probe, bytes):
            self.logger.warning(f"🎥 {event_id}: range API gave no usable probe — "
                                f"cannot trim the clip")
            return None, 0, None

        bps = len(probe) / probe_secs
        secs = int(cap * 0.85 / bps) if bps else probe_secs
        secs = max(probe_secs, min(secs, duration, MAX_TRIM_SECONDS))
        self.logger.info(
            f"🎥 {event_id}: clip too big — probe {len(probe) / 1024 / 1024:.1f} MB/"
            f"{probe_secs}s ⇒ {bps / 1024:.0f} KB/s, trimming to {secs}s of {duration}s"
        )

        data = probe
        if secs > probe_secs:
            trimmed = await self._download(range_url(secs), 120,
                                           f"🎥 trimmed clip {event_id} ({secs}s)",
                                           max_bytes=cap)
            if isinstance(trimmed, bytes):
                data = trimmed
            else:  # estimate was optimistic — keep the probe we already have
                self.logger.warning(f"🎥 {event_id}: {secs}s still too big, "
                                    f"falling back to the {probe_secs}s probe")
                secs = probe_secs

        # Log: the full story for whoever debugs. Chat caption: one short line.
        self.logger.info(
            f"✂️ {event_id}: sending a trimmed clip — first {secs}s of {duration}s "
            f"({len(data) / 1024 / 1024:.1f} MB of an estimated "
            f"{bps * duration / 1024 / 1024:.0f} MB full clip, cap {MAX_CLIP_MB} MB). "
            f"Full recording stays in Frigate."
        )
        minutes = max(1, round(duration / 60))
        if LANG == "ru":
            caption = f"✂️ Первые {secs} с из {minutes} мин — полное видео во Frigate"
        else:
            caption = f"✂️ First {secs}s of {minutes} min — full video in Frigate"
        return data, secs, caption

    async def _send_text_notice(self, event: Dict[str, Any],
                                reason: str = "too_large") -> bool:
        """Compact text-only heads-up when the photo+video album can't be built.

        We deliberately never fall back to a lone photo (or video): Telegram
        renders a single media item at full size, which looks bad in the chat —
        the album is what keeps previews compact. A one-line text with the
        object and camera keeps the alert useful without the oversized image.
        `reason`: clip oversized ("too_large") or Frigate never served it
        ("clip_unavailable")."""
        obj = event.get("label") or ("объект" if LANG == "ru" else "object")
        cam = event.get("camera")
        where = f" @ {cam}" if cam else ""
        if reason == "clip_unavailable":
            body = ("клип пока недоступен во Frigate"
                    if LANG == "ru" else "the clip isn’t available in Frigate yet")
        else:
            body = (f"клип слишком большой для Telegram (> {MAX_CLIP_MB} МБ)"
                    if LANG == "ru" else
                    f"the clip is too large for Telegram (> {MAX_CLIP_MB} MB)")
        tail = "смотри запись во Frigate" if LANG == "ru" else "watch the recording in Frigate"
        text = f"🎥 {obj}{where}: {body} — {tail}"
        try:
            await self.bot.send_message(
                chat_id=self.group_config["telegram_chat_id"],
                text=text,
                disable_notification=self.silent,
            )
            self.logger.info(f"✅ Text notice sent ({reason})")
            return True
        except TelegramError as e:
            self.logger.error(f"❌ Telegram error (text notice): {e}")
            return False

    async def _send_startup_notice(self):
        """Silent one-liner on start: proves the bot can post to this chat and
        shows what this group is watching."""
        name = self.group_config.get("name", self.group_id)
        zones = ", ".join(self.zones) if self.zones else ("all" if LANG == "en" else "все")
        if LANG == "ru":
            text = (f"🚀 Мониторинг запущен: «{name}» (v{_version()})\n"
                    f"📹 Камеры: {', '.join(self.cameras)}\n"
                    f"🎯 Объекты: {', '.join(self.objects)}\n"
                    f"🧭 Зоны: {zones}")
        else:
            text = (f"🚀 Monitoring started: “{name}” (v{_version()})\n"
                    f"📹 Cameras: {', '.join(self.cameras)}\n"
                    f"🎯 Objects: {', '.join(self.objects)}\n"
                    f"🧭 Zones: {zones}")
        try:
            await self.bot.send_message(
                chat_id=self.group_config["telegram_chat_id"],
                text=text,
                disable_notification=True,
            )
            self.logger.info("📨 Startup notice sent")
        except TelegramError as e:
            self.logger.error(f"❌ Startup notice failed: {e} — check the bot token "
                              f"and that the bot is a member of the chat")

    async def _send_media_group(self, photo_data: bytes, video_data: bytes,
                                caption: Optional[str] = None) -> bool:
        """Send photo + video to the group's chat as one media group.
        A caption on the first item shows up as the album caption.

        A transient timeout / network blip to api.telegram.org is retried with
        backoff — one hiccup used to drop the alert outright. (A timeout can in
        theory fire after Telegram already accepted the album, so a retry may
        rarely duplicate it — acceptable for an entrance camera where a missed
        alert is the worse failure.) Other Telegram errors — bad chat, bad
        request — won't fix themselves, so they fail fast."""
        for attempt in range(1, TELEGRAM_SEND_RETRIES + 1):
            try:
                await self.bot.send_media_group(
                    chat_id=self.group_config["telegram_chat_id"],
                    media=[InputMediaPhoto(media=photo_data, caption=caption),
                           InputMediaVideo(media=video_data)],
                    disable_notification=self.silent,
                )
                self.logger.info("✅ Media group sent")
                return True
            except (TimedOut, NetworkError) as e:
                if attempt < TELEGRAM_SEND_RETRIES:
                    self.logger.warning(
                        f"⏳ Telegram {type(e).__name__} "
                        f"(attempt {attempt}/{TELEGRAM_SEND_RETRIES}) — retrying"
                    )
                    await asyncio.sleep(TELEGRAM_SEND_BACKOFF * attempt)
                    continue
                self.logger.error(f"❌ Telegram error: {e}")
                return False
            except TelegramError as e:
                self.logger.error(f"❌ Telegram error: {e}")
                return False
            except Exception as e:
                self.logger.error(f"❌ Failed to send media: {e}")
                return False
        return False

    # ------------------------------------------------------------------ stats

    def _start_stats_timer(self):
        def stats_timer():
            while True:
                time.sleep(STATS_INTERVAL)
                uptime = int(time.time() - self.stats["start_time"])
                self.logger.info(
                    f"📊 Stats ({self.group_id}): uptime={uptime}s, "
                    f"processed={self.stats['events_processed']}, "
                    f"sent={self.stats['telegram_sent']}, errors={self.stats['errors']}"
                )

        threading.Thread(target=stats_timer, daemon=True).start()

    # ------------------------------------------------------------------- MQTT

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.logger.info("✅ Connected to MQTT broker")
            client.subscribe(f"{MQTT_TOPIC_PREFIX}/events")
            self.logger.info(f"📡 Subscribed to {MQTT_TOPIC_PREFIX}/events")
        else:
            self.logger.error(f"❌ MQTT connect failed: rc={rc}")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        if rc != 0:
            self.logger.warning(f"⚠️ Unexpected MQTT disconnect: rc={rc}")
        else:
            self.logger.info("🔌 Disconnected from MQTT broker")

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            event_type = payload.get("type")

            # Care about finished events: type=end, or type=update carrying end_time
            if event_type not in ("end", "update"):
                return
            event_data = payload.get("after", {})
            if not event_data.get("end_time"):
                return  # not finished yet

            camera = event_data.get("camera")
            object_type = event_data.get("label")
            event_id = event_data.get("id")

            if camera not in self.cameras or object_type not in self.objects:
                return
            if not self._zone_ok(event_data):
                self.logger.debug(
                    f"⏭️ MQTT: {object_type}@{camera} ({event_id}) outside wanted "
                    f"zones {self.zones} (was in {event_data.get('entered_zones') or []})"
                )
                return

            self.logger.info(
                f"📨 MQTT event ({event_type}): {object_type} on {camera} (ID: {event_id})"
            )
            if self.event_loop and self.mqtt_event_queue:
                asyncio.run_coroutine_threadsafe(
                    self.mqtt_event_queue.put(event_data), self.event_loop
                )
        except Exception as e:
            self.logger.error(f"❌ Error handling MQTT message: {e}")

    def _start_mqtt_client(self):
        def mqtt_thread():
            # paho-mqtt 2.x requires an explicit callback API version;
            # VERSION1 keeps the same handler signatures as 1.x.
            import warnings
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            except AttributeError:  # paho-mqtt 1.x
                self.mqtt_client = mqtt.Client()
            self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_message = self._on_mqtt_message
            try:
                self.mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
                self.mqtt_client.loop_forever()
            except Exception as e:
                self.logger.error(f"❌ MQTT client error: {e}")

        threading.Thread(target=mqtt_thread, daemon=True).start()
        self.logger.info("🚀 MQTT client started in a background thread")

    async def _process_mqtt_events(self):
        """Consume real-time events queued by the MQTT thread."""
        while True:
            try:
                event = await asyncio.wait_for(self.mqtt_event_queue.get(), timeout=1.0)

                event_id = event.get("id")
                camera = event.get("camera")
                object_type = event.get("label")

                if event_id in self.processed_events:
                    self.logger.debug(f"⏭️ {event_id} already handled, skipping")
                    continue
                # Claim immediately so the API poll can't double-send it
                self.processed_events.add(event_id)

                self.logger.info(f"📨 Handling MQTT event: {object_type}@{camera} ({event_id})")

                # Alarm: first event after a long quiet spell -> instant text now,
                # before the API fetch + media wait below.
                await self._maybe_idle_alert(event)

                # Fetch the full event from the API (it may lag a few seconds)
                full_event = None
                for attempt in range(1, 21):  # up to ~60s
                    try:
                        async with self.http.get(
                            f"{FRIGATE_URL}/api/events/{event_id}",
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as response:
                            if response.status == 200:
                                full_event = await response.json()
                                self.logger.debug(
                                    f"✅ {event_id} fetched from API (attempt {attempt})"
                                )
                                break
                            if response.status != 404:
                                self.logger.debug(
                                    f"⚠️ HTTP {response.status} fetching {event_id} "
                                    f"(attempt {attempt})"
                                )
                    except Exception as e:
                        self.logger.debug(f"⚠️ Error fetching {event_id} (attempt {attempt}): {e}")
                    if attempt < 20:
                        await asyncio.sleep(3)
                else:
                    self.logger.warning(f"⚠️ {event_id} never appeared in the API")

                if full_event and full_event.get("has_snapshot") and full_event.get("has_clip"):
                    await self._process_frigate_event(full_event, source="mqtt")
                else:
                    # Media (or the event itself) not ready — let the poll loop retry
                    self.retry_events[event_id] = (full_event or event, 0)
                    self.logger.info(f"⏳ {event_id} queued for retry (media not ready)")

            except asyncio.TimeoutError:
                continue  # idle tick
            except Exception as e:
                sentry_init.capture(e)
                self.logger.error(f"❌ Error processing MQTT event: {e}")

    # ------------------------------------------------------------------- main

    async def start_monitoring(self):
        self.logger.info(f"🚀 Starting Frigate monitor ({self.group_config['name']})")
        self.logger.info(f"📹 Cameras: {', '.join(self.cameras)}")
        self.logger.info(f"🎯 Tracked objects: {', '.join(self.objects)}")

        # Point out settings this config predates (updating via `manage.sh
        # update` brings new code but never touches config.py)
        for tip in config_check.hints():
            self.logger.info(f"💡 {tip}")

        self.event_loop = asyncio.get_running_loop()
        self.mqtt_event_queue = asyncio.Queue()
        self.http = aiohttp.ClientSession()

        if self.startup_message:
            await self._send_startup_notice()

        self._start_stats_timer()
        self._start_mqtt_client()
        mqtt_task = asyncio.create_task(self._process_mqtt_events())

        try:
            while True:
                await self._check_frigate_events()
                await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            self.logger.error(f"❌ Fatal error: {e}")
            raise
        finally:
            mqtt_task.cancel()
            await self.http.close()


def main():
    errs = config_check.errors()
    if errs:
        print("❌ config.py has problems:")
        for e in errs:
            print(f"   - {e}")
        print("Fix config.py (see config.example.py) or run: ./manage.sh doctor")
        sys.exit(1)

    if len(sys.argv) != 2:
        print("Usage: python frigate_telegram_monitor.py <group_id>")
        print("Available groups:", list(GROUPS.keys()))
        sys.exit(1)

    group_id = sys.argv[1]
    if group_id not in GROUPS:
        print(f"Error: group '{group_id}' not found")
        print("Available groups:", list(GROUPS.keys()))
        sys.exit(1)

    sentry_init.init(f"frigate-monitor-{group_id}")

    monitor = FrigateTelegramMonitor(group_id)
    try:
        asyncio.run(monitor.start_monitoring())
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    except Exception as e:
        sentry_init.capture(e)
        print(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
