#!/usr/bin/env python3
"""Self-diagnosis for frigate-notify-alert.

Run via manage.sh:
  ./manage.sh doctor  -> checks the whole chain: config, Frigate API,
                         cameras/zones, media on recent events, MQTT,
                         Telegram token & chats, systemd services

(A live "can the bot post to the chat?" check happens automatically: each
monitor sends a silent startup notice to its chat when it starts.)
"""

import asyncio
import shutil
import subprocess
import sys
import time

import aiohttp

OK, WARN, FAIL = "✅", "⚠️", "❌"
_fails = 0
_warns = 0


def ok(msg):
    print(f"{OK} {msg}")


def warn(msg):
    global _warns
    _warns += 1
    print(f"{WARN} {msg}")


def fail(msg):
    global _fails
    _fails += 1
    print(f"{FAIL} {msg}")


def load_config():
    try:
        import config as cfg
    except ModuleNotFoundError:
        print(f"{FAIL} config.py not found. First: cp config.example.py config.py")
        sys.exit(1)
    return cfg


# ------------------------------------------------------------------- config

def check_config(cfg) -> bool:
    """Static sanity: placeholders left in, empty groups, missing keys."""
    groups = getattr(cfg, "GROUPS", None)
    if not isinstance(groups, dict) or not groups:
        fail("GROUPS is missing or empty in config.py")
        return False

    bad = []
    for name in ("TELEGRAM_BOT_TOKEN", "MQTT_BROKER_HOST", "MQTT_USERNAME",
                 "MQTT_PASSWORD", "FRIGATE_URL"):
        v = getattr(cfg, name, None)
        if not isinstance(v, str) or "SET_ME" in v or "ВСТАВЬ" in v:
            bad.append(name)
    for gid, g in groups.items():
        cid = str(g.get("telegram_chat_id", ""))
        if not cid or "SET_ME" in cid or "ВСТАВЬ" in cid:
            bad.append(f"GROUPS[{gid}].telegram_chat_id")
        if not g.get("cameras"):
            fail(f"GROUPS[{gid}]: 'cameras' list is empty")

    if bad:
        fail("config.py still has placeholders / missing values: " + ", ".join(bad))
        return False
    ok(f"config.py: {len(groups)} group(s), no placeholders")
    return True


# ------------------------------------------------------------------ frigate

async def check_frigate(cfg, http):
    url = cfg.FRIGATE_URL.rstrip("/")
    try:
        async with http.get(f"{url}/api/config",
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                hint = " (port 8971 is authenticated — use the internal port 5000)" \
                    if r.status in (401, 403) else ""
                fail(f"Frigate API: HTTP {r.status} from {url}/api/config{hint}")
                return None
            fc = await r.json()
    except Exception as e:
        fail(f"Frigate API unreachable at {url}: {e}")
        return None
    ok(f"Frigate API reachable, version {fc.get('version', '?')}")
    return fc


def check_cameras(cfg, fc):
    cams = fc.get("cameras", {})
    for gid, g in cfg.GROUPS.items():
        for cam in g["cameras"]:
            if cam not in cams:
                close = next((c for c in cams if c.lower() == cam.lower()), None)
                fail(f"[{gid}] camera '{cam}' not found in Frigate"
                     + (f" — did you mean '{close}'?" if close else
                        f" (Frigate has: {', '.join(sorted(cams)) or 'none'})"))
                continue
            c = cams[cam]
            snap = (c.get("snapshots") or {}).get("enabled")
            rec = (c.get("record") or {}).get("enabled")
            if not snap:
                fail(f"[{gid}] '{cam}': snapshots disabled in Frigate → events will never be sent")
            if not rec:
                fail(f"[{gid}] '{cam}': record disabled in Frigate → no clips, events will never be sent")
            if snap and rec:
                ok(f"[{gid}] camera '{cam}': found, snapshots + record enabled")

        wanted = g.get("zones") or []
        if wanted:
            avail = set()
            for cam in g["cameras"]:
                avail |= set(((cams.get(cam) or {}).get("zones") or {}).keys())
            missing = [z for z in wanted if z not in avail]
            if missing:
                fail(f"[{gid}] zones not found on this group's cameras: {missing} "
                     f"(available: {sorted(avail) or 'none'})")
            else:
                ok(f"[{gid}] zones OK: {', '.join(wanted)}")


async def check_recent_events(cfg, http):
    """Do recent Frigate events actually carry media for our cameras?"""
    url = cfg.FRIGATE_URL.rstrip("/")
    try:
        async with http.get(f"{url}/api/events?limit=100",
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            events = await r.json() if r.status == 200 else []
    except Exception:
        events = []

    configured = {c for g in cfg.GROUPS.values() for c in g["cameras"]}
    for cam in sorted(configured):
        evs = [e for e in events if e.get("camera") == cam]
        if not evs:
            warn(f"camera '{cam}': no recent events to judge (camera may simply be quiet)")
            continue
        clips = sum(1 for e in evs if e.get("has_clip"))
        snaps = sum(1 for e in evs if e.get("has_snapshot"))
        if clips == 0:
            fail(f"camera '{cam}': {len(evs)} recent events, none has a clip → "
                 f"check record retain mode (README → Troubleshooting)")
        elif snaps == 0:
            fail(f"camera '{cam}': {len(evs)} recent events, none has a snapshot → enable snapshots")
        else:
            ok(f"camera '{cam}': recent events carry media (clips {clips}/{len(evs)})")


# --------------------------------------------------------------------- mqtt

def check_mqtt(cfg):
    import warnings

    import paho.mqtt.client as mqtt
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client()
    client.username_pw_set(cfg.MQTT_USERNAME, cfg.MQTT_PASSWORD)

    result = {}
    client.on_connect = lambda c, u, f, rc: result.setdefault("rc", rc)
    try:
        client.connect(cfg.MQTT_BROKER_HOST, cfg.MQTT_BROKER_PORT, 10)
    except Exception as e:
        fail(f"MQTT: cannot reach {cfg.MQTT_BROKER_HOST}:{cfg.MQTT_BROKER_PORT}: {e}")
        return
    client.loop_start()
    for _ in range(50):  # up to 5 s
        if "rc" in result:
            break
        time.sleep(0.1)
    client.loop_stop()
    client.disconnect()

    rc = result.get("rc")
    if rc == 0:
        ok(f"MQTT: connected to {cfg.MQTT_BROKER_HOST}:{cfg.MQTT_BROKER_PORT}")
    elif rc is None:
        warn("MQTT: TCP connection OK but no broker reply within 5 s")
    elif rc in (4, 5):
        fail("MQTT: authentication failed — check MQTT_USERNAME / MQTT_PASSWORD")
    else:
        fail(f"MQTT: broker refused the connection (rc={rc})")


# ----------------------------------------------------------------- telegram

def _make_bot(cfg):
    from telegram import Bot
    from telegram.request import HTTPXRequest
    kw = {"connect_timeout": 10, "read_timeout": 20}
    if getattr(cfg, "TELEGRAM_PROXY_URL", None):
        kw["proxy"] = cfg.TELEGRAM_PROXY_URL
    return Bot(token=cfg.TELEGRAM_BOT_TOKEN, request=HTTPXRequest(**kw))


def _chat_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw  # e.g. "@channelname"


async def check_telegram(cfg):
    from telegram.error import TelegramError
    bot = _make_bot(cfg)
    try:
        me = await bot.get_me()
        ok(f"Telegram: token valid, bot @{me.username}")
    except TelegramError as e:
        hint = " (Telegram blocked by your ISP? set TELEGRAM_PROXY_URL)" \
            if "timed out" in str(e).lower() or "connect" in str(e).lower() else ""
        fail(f"Telegram: token/connection problem: {e}{hint}")
        return
    for gid, g in cfg.GROUPS.items():
        try:
            chat = await bot.get_chat(_chat_id(g["telegram_chat_id"]))
            ok(f"[{gid}] chat reachable: {chat.title or chat.username or chat.id}")
        except TelegramError as e:
            fail(f"[{gid}] cannot access chat {g['telegram_chat_id']}: {e} "
                 f"— is the bot a member of that chat?")


# ----------------------------------------------------------------- services

def check_services(cfg):
    if not shutil.which("systemctl"):
        warn("systemd not found — skipping service checks (manual mode)")
        return
    units = [f"frigate-telegram@{gid}" for gid in cfg.GROUPS] + ["frigate-telegram-control"]
    for unit in units:
        state = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True).stdout.strip()
        if state == "active":
            ok(f"service {unit}: active")
        else:
            warn(f"service {unit}: {state or 'unknown'} (not installed yet? run: sudo ./manage.sh setup)")


# --------------------------------------------------------------------- main

def summary_and_exit():
    print()
    if _fails:
        print(f"{FAIL} {_fails} problem(s), {_warns} warning(s) — see above")
        sys.exit(1)
    if _warns:
        print(f"{WARN} OK with {_warns} warning(s)")
        sys.exit(0)
    print(f"{OK} All checks passed")
    sys.exit(0)


async def run_doctor(cfg):
    async with aiohttp.ClientSession() as http:
        fc = await check_frigate(cfg, http)
        if fc:
            check_cameras(cfg, fc)
            await check_recent_events(cfg, http)
    await check_telegram(cfg)


def main():
    cfg = load_config()
    print("🩺 frigate-notify-alert doctor\n")
    if not check_config(cfg):
        summary_and_exit()
    asyncio.run(run_doctor(cfg))
    check_mqtt(cfg)
    check_services(cfg)
    summary_and_exit()


if __name__ == "__main__":
    main()
