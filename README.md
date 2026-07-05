# frigate-notify-alert

**🇷🇺 Русская версия:** [README.ru.md](README.ru.md)

**Frigate → Telegram** notifications: when Frigate detects a person or car, the bot
sends the event's **photo + video** to your chat. Multiple camera groups (each to its
own chat), optional zone filtering, and in‑chat pause buttons — all in Telegram.

<p align="center">
  <img src="docs/telegram-example.jpg" alt="Example: Frigate detection snapshot with bounding box + video clip in a Telegram chat" width="300">
  <br><sub>How it looks in the chat: for each event — a snapshot with a detection box and a video clip.</sub>
</p>

## Contents
- [Features](#features)
- [Requirements](#requirements)
- [Frigate setup (required)](#frigate-setup-required)
- [Installation](#installation)
- [Configuration (`config.py`) in detail](#configuration-configpy-in-detail)
- [Updating](#updating)
- [Multiple groups / scaling](#multiple-groups--scaling)
- [Pause notifications](#pause-notifications)
- [Management](#management)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Versioning](#versioning) · [License](#license)

## Features
- 📸 Photo + video of the event in Telegram (media group; silent by default, `silent: False` for loud).
- 📹 Multiple camera groups — each to its own chat.
- 🧭 Zone filtering (`zones`) — notify only when the object enters a chosen Frigate zone.
- ⏸ Pause buttons in the chat (15 min / 1 h / 3 h / until morning) — per group.
- ➕ Scales to any number of groups via a templated systemd unit.
- 🌐 Optional proxy for Telegram (to bypass ISP blocking).
- 🇬🇧🇷🇺 Interface language `en` / `ru`.

## Requirements
- A working **Frigate** install with **MQTT** enabled.
- A **Telegram bot** (create via [@BotFather](https://t.me/BotFather)) and a chat ID.
- **Python 3.9+**, Linux with systemd (for autostart).

## Frigate setup (required)

The script gets events and media from Frigate. For notifications to arrive **with photo
and video**, Frigate needs **three** things enabled:

| What | Why | Without it |
|---|---|---|
| **MQTT** | real-time event delivery (`frigate/events`) | events only via API polling — delayed |
| **Snapshots** | provides the event photo (`has_snapshot`) | no photo → event is never sent |
| **Record** | provides the event video clip (`has_clip`) | no video → event is never sent |

Also, the objects in `objects.track` must overlap with `OBJECTS` in `config.py`, and
zones (if you want the `zones` filter) must be defined on the cameras.

### Minimal Frigate `config.yml` (version 0.14+)
```yaml
mqtt:
  enabled: true
  host: 192.168.1.50          # same host/user/password go into MQTT_* in config.py
  user: frigate
  password: secret

detectors:
  # your detector — coral / cpu / openvino / etc.
  cpu1:
    type: cpu

objects:
  track:
    - person
    - car                     # must overlap with OBJECTS in config.py

# Snapshots — needed for the photo in the notification
snapshots:
  enabled: true
  retain:
    default: 14               # days to keep snapshots

# Recordings — needed for the video clip (Frigate 0.14+)
record:
  enabled: true
  alerts:
    retain:
      days: 14
      mode: active_objects    # recommended — see the note below
  detections:
    retain:
      days: 14
      mode: active_objects

cameras:
  yard:                       # ← this name goes into "cameras": [...] in config.py
    ffmpeg:
      inputs:
        - path: rtsp://LOGIN:PASSWORD@CAMERA_IP:554/stream
          roles: [detect, record]
    detect:
      enabled: true
    zones:                    # optional — for the "zones" filter in config.py
      zone_yard:              # ← this name goes into "zones": [...] in config.py
        coordinates: 0.1,0.9,0.9,0.9,0.9,0.1,0.1,0.1
```

Notes:
- `snapshots`, `record` and `objects.track` work **globally or per‑camera** — what matters
  is the effective value on the camera.
- **`mode: active_objects` is deliberate:** with `mode: motion`, a motion mask over the area
  where objects move silently drops those clips (photo arrives, video never does) — see
  [Troubleshooting](#troubleshooting).
- Example targets Frigate **0.14+** (tested on 0.17); on 0.13 retention was
  `record.events.retain`. Official docs:
  [record](https://docs.frigate.video/configuration/record) ·
  [snapshots](https://docs.frigate.video/configuration/snapshots) ·
  [zones](https://docs.frigate.video/configuration/zones).

### Verify it's ready
After editing the config, **restart Frigate**. A finished event should have
`has_snapshot: true` and `has_clip: true` — visible in the Frigate UI (Explore) or the API:
```bash
curl http://FRIGATE_IP:5000/api/events | python3 -m json.tool | grep -E "has_snapshot|has_clip"
```
If `has_clip` is always `false`, `record` isn't enabled/retained; if `has_snapshot` is
`false`, `snapshots` isn't enabled.

## Installation
```bash
git clone https://github.com/Sysoev86/frigate-notify-alert.git
cd frigate-notify-alert

cp config.example.py config.py     # your config (gitignored, never committed)
nano config.py                     # fill it in (see details below)

sudo ./manage.sh setup             # dependencies + systemd units (one per group) + start
```
Manual run without systemd: `./manage.sh run [group]`. Check version: `./manage.sh version`.

## Configuration (`config.py`) in detail

`config.py` is the only file you edit. Anything written in UPPERCASE like `"SET_ME_..."`
is a placeholder to replace. Quotes around strings are required (this is Python); numbers
(ports) have no quotes.

### Full example
```python
# 1. TELEGRAM --------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TELEGRAM_PROXY_URL = None            # or "http://LOGIN:PASSWORD@IP:PORT"

# 2. CAMERA GROUPS ---------------------------------------------------------
GROUPS = {
    "group1": {
        "telegram_chat_id": "-1001234567890",
        "cameras": ["yard", "gate"],
        "zones": ["zone_yard"],       # optional
        "mute_controls": True,        # optional
        "name": "Yard",
    },
    "group2": {
        "telegram_chat_id": "-1009876543210",
        "cameras": ["entrance"],
        "name": "Entrance",
    },
}

# 3. MQTT (from your Frigate settings) -------------------------------------
MQTT_BROKER_HOST = "192.168.1.50"
MQTT_BROKER_PORT = 1883
MQTT_USERNAME = "frigate"
MQTT_PASSWORD = "secret"
MQTT_TOPIC_PREFIX = "frigate"

# 4. FRIGATE ---------------------------------------------------------------
FRIGATE_URL = "http://192.168.1.50:5000"

# 5. OBJECTS ---------------------------------------------------------------
OBJECTS = ["person", "car", "truck", "bus", "motorcycle", "bicycle"]

# 6. MISC / UI -------------------------------------------------------------
LANG = "en"                          # interface language of the pause controller: "en" or "ru"
LOG_LEVEL = "INFO"
STATS_INTERVAL = 60
MEDIA_RETRY_ATTEMPTS = 15
MEDIA_RETRY_DELAY = 3
```

### Field reference

#### Telegram
| Field | Required | Description |
|---|:---:|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from [@BotFather](https://t.me/BotFather): `/newbot` → name → username → a string like `1234567890:AA...`. |
| `TELEGRAM_PROXY_URL` | no | `None` = no proxy (the usual case). Needed only if Telegram is blocked by your ISP. Format `"http://LOGIN:PASSWORD@IP:PORT"`. |

#### `GROUPS` — camera groups
A group = a set of cameras + one chat. You can have any number of groups
(`group1`, `group2`, …). The group key (`group1`) is also the systemd service name:
`frigate-telegram@group1`.

| Key | Required | Description |
|---|:---:|---|
| `telegram_chat_id` | yes | Where to send. For groups/channels it starts with `-100…`. See "how to find" below. |
| `cameras` | yes | Camera names **exactly as in Frigate** (case‑sensitive). |
| `zones` | no | Frigate zone names. If set, notify only when the object entered one of these zones. Omit / empty = notify for the whole camera. |
| `objects` | no | Override the global `OBJECTS` list for this group only (e.g. `["person"]` for an indoor camera). |
| `silent` | no | `True` (default) → messages arrive silently (no sound/vibration). `False` → full loud notifications. |
| `mute_controls` | no | `True` (default, even if omitted) → pause buttons appear in the chat. `False` → no buttons for this group. |
| `name` | no | Free‑form label, only used in logs. |

**How to find `telegram_chat_id`:**
- DM: message [@userinfobot](https://t.me/userinfobot) — it shows your numeric id.
- Group/channel: add **your** bot to the chat, then message [@getidsbot](https://t.me/getidsbot). Group IDs are usually `-100…`.
- ⚠️ The bot must be a **member** of the chat, otherwise it can't post there.

**How to find camera names (`cameras`):** the keys under `cameras:` in Frigate's `config.yml`.

**How to find zones (`zones`):** the keys under `cameras.<camera>.zones` in Frigate's `config.yml`.

#### MQTT (from Frigate's `mqtt:` section)
| Field | Default | Description |
|---|---|---|
| `MQTT_BROKER_HOST` | — | MQTT broker IP (usually the same host as Frigate). |
| `MQTT_BROKER_PORT` | `1883` | Standard MQTT port. |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | Credentials from Frigate's `mqtt:` config. |
| `MQTT_TOPIC_PREFIX` | `"frigate"` | Frigate topic prefix. |

#### Frigate & misc
| Field | Default | Description |
|---|---|---|
| `FRIGATE_URL` | — | Frigate API URL where photos/videos are fetched. Must be the **unauthenticated internal port 5000** — the authenticated port 8971 won't work (the bot sends no credentials). |
| `OBJECTS` | person, car, truck, bus, motorcycle, bicycle | Which objects to react to (Frigate labels). |
| `LANG` | `"en"` | Pause‑controller interface language: `"en"` or `"ru"`. |
| `LOG_LEVEL` | `"INFO"` | `INFO` or `DEBUG` (DEBUG logs every poll cycle in detail). |
| `STATS_INTERVAL` / `MEDIA_RETRY_ATTEMPTS` / `MEDIA_RETRY_DELAY` | 60 / 15 / 3 | Stats interval (s); media download retries; delay between retries (s). |

## Updating
```bash
sudo ./manage.sh update    # git pull + reinstall units + restart
./manage.sh version        # local version and latest tag on origin
```
`config.py` is never touched (it's gitignored), so updates don't break your settings.

## Multiple groups / scaling
Each group runs as a templated systemd unit `frigate-telegram@<group>`; `manage.sh` reads
the group list straight from `config.py`. To add a group: put it into `GROUPS`, then
`sudo ./manage.sh install && sudo ./manage.sh start` — no new files, no code changes.
The pause controller picks up new groups automatically.

## Pause notifications
The `frigate-telegram-control` service (`mute_controller.py`) keeps a keyboard at the
bottom of each chat: `⏸ 15 min | 1 h | 3 h | Until morning | ▶️ Resume`. Tap it and that
group's notifications go silent until the pause ends (it survives restarts). The pause
applies only to the group whose chat the button was tapped in.

- Toggled per group via `mute_controls` (on by default); button language — via `LANG`.
- To let the bot pin the status and clean up taps, make it a chat **admin** (optional).

## Management
One script for everything — run from the project folder: `./manage.sh <command>`.
Commands that touch systemd (`setup`/`install`/`start`/`stop`/`restart`/`enable`/`disable`/`update`)
need `sudo`.

| Command | What it does |
|---|---|
| `setup` | First-time install: dependencies + systemd units + start. The only command a fresh install needs. |
| `run [group]` | Manual foreground run without systemd (auto-picks the group if there's only one). |
| `install` | (Re)installs systemd units (one per group from `config.py`), enables autostart. |
| `start` / `stop` / `restart` | Control all services. Use `restart` after editing `config.py`. |
| `status` | Status of each group + the controller. |
| `logs` | Live logs of all groups + the controller (`Ctrl+C` to exit). |
| `enable` / `disable` | Toggle autostart on boot (usually already done by `install`). |
| `update` | Update to the latest version (git pull + reinstall + restart). |
| `version` | Show local version and the latest tag. |

## How it works
The script gets events in real time from the MQTT topic `frigate/events` (with `/api/events`
polling as a fallback), filters them by camera, object and (optionally) zone, waits for the
snapshot + clip to be ready, and sends both to the chat as a media group. On startup it
ignores events that finished **before** launch, so it doesn't spam history. The pause
controller (`mute_controller.py`) is a separate process: it listens for button taps and
writes `mute_state.json`, which the monitors read before sending.

## Troubleshooting
- Services won't start → `./manage.sh status`, `journalctl -u 'frigate-telegram@*' -e`.
- No notifications → confirm events exist in Frigate; check `./manage.sh logs`.
- `config.py not found` → run `cp config.example.py config.py`.
- Telegram unreachable (timeouts / `Flood control`) → check network/proxy; set
  `TELEGRAM_PROXY_URL` if Telegram is blocked.
- **One camera sends photos but never video** (event has `has_snapshot: true`,
  `has_clip: false`) → in Frigate this camera's clips aren't retained. A common cause is
  `record.<alerts|detections>.retain.mode: motion` **combined with a motion mask** covering
  the area where objects move: object detection still fires (so the snapshot arrives), but
  Frigate sees "no motion" there, so those recording segments aren't kept and the event
  never gets a clip. **Fix:** use `mode: active_objects` (retain segments that contain a
  tracked object, independent of motion masks). Then `has_clip` becomes `true` and video
  flows. Note `record` is usually global, so this affects every masked camera.

## Versioning
[Semantic Versioning](https://semver.org/). Changes are in [CHANGELOG.md](CHANGELOG.md),
releases on the [Releases](https://github.com/Sysoev86/frigate-notify-alert/releases) tab.

## License
[MIT](LICENSE).
