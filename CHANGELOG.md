# Changelog

**🇷🇺 Русская версия:** [CHANGELOG.ru.md](CHANGELOG.ru.md)

All notable changes. Format follows [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`.

## [Unreleased]
### Added
- **Alarm mode — instant heads-up after a quiet spell** (`IDLE_ALERT_AFTER`, off by
  default). Positions the bot as a lightweight intrusion alarm: if there were no events
  for the configured time (e.g. an hour), the first event that breaks the silence sends
  an immediate, audible text ("🚨 Motion after 2 h of quiet — person @ camera; photo and
  video to follow") — *before* the photo+video, which take a few seconds to finalize. So
  a possible unauthorized entry is flagged at once, without waiting for the media. Fires
  once per event across both the MQTT and polling paths; respects pause; muted events
  still reset the idle clock. `0` disables it (default), so existing installs are
  unaffected.
- **README now leads with the alarm use-case**: events are pushed over MQTT (polling is
  only a fallback) so they reach Telegram as fast as possible; set `silent: False` for an
  audible alert.

### Fixed
- **Alert no longer dropped when only the clip is missing.** If Frigate reported the event
  as ready but never actually served `clip.mp4` within the retry window, the whole
  notification was discarded. The chat now gets a compact **text notice** in that case
  ("clip not available yet — watch in Frigate", with the object and camera) instead of
  nothing. A lone photo is deliberately *not* sent: Telegram renders a single media item
  oversized, so media always goes as a photo+video album or not at all.
- **Transient Telegram timeout no longer drops the alert.** A single `Timed out` /
  network blip when sending to `api.telegram.org` used to lose the notification outright.
  The send is now retried a few times with growing backoff (`TELEGRAM_SEND_RETRIES`,
  `TELEGRAM_SEND_BACKOFF`); persistent non-transient errors (bad chat, bad request) still
  fail fast.
### Changed
- The "media never became available" error now records **which** asset was missing
  (photo/clip) and **why** (HTTP status / timeout / half-written file), so the tracker
  says what actually went wrong instead of just "after N attempts".

## [1.3.4] — 2026-07-16
### Fixed
- **OOM on huge clips.** Long events (e.g. a person working in a warehouse for an hour)
  produce clips of hundreds of MB; downloading them into RAM got real installs killed by
  the OOM killer — and Telegram bots can't upload files over 50 MB anyway. Clips are now
  capped (`MAX_CLIP_MB`, default 45; checked via Content-Length and while streaming).
  Oversized events are delivered as the usual compact photo+video album with a **trimmed
  clip** (the first N seconds, sized from the clip's bitrate, via Frigate's time-range
  recordings API) and a caption; photo-only is the very last resort.
- **Webhook conflict.** A webhook left on the bot token (by other bot software) silently
  broke the pause buttons with endless `409 Conflict`. The controller now detects and
  removes it on start; `doctor` reports it too.
- **Bot token no longer leaks into logs.** httpx logged every request URL including the
  token — its INFO logging is now silenced in both processes.

### Added
- **Config hints.** `manage.sh update` brings new code but never touches `config.py`, so
  new settings stayed invisible (one user ran the English UI for weeks without knowing
  `LANG` exists). The monitor's startup log and `doctor` now list options missing from
  your config, and point out a Russian setup (Cyrillic group names) running the default
  English interface. Future options are picked up automatically.
- Optional error reporting to GlitchTip/Sentry: set the `GLITCHTIP_DSN` environment
  variable (plus `pip install sentry-sdk`); without it nothing changes.

### Internal
- Config validation was duplicated in the monitor and the controller (and a third variant
  in `doctor`) — now shared in `config_check.py`.

## [1.3.3] — 2026-07-06
### Fixed
- **Stale pinned pause status.** The pinned "🔕 paused until X" was only refreshed by a
  button press, so an expired pause left it pinned forever. An expiry watcher now flips
  the panel back within ~a minute. (Notifications themselves always resumed on time —
  the monitor checks expiry on its own.)

### Changed
- **State-aware control panel.** No more permanent "▶️ Resume" button while notifications
  are on: the keyboard shows pause buttons only, and gains Resume + extend options while
  paused. One panel message replaces the old keyboard + status pair; pinned only during
  a pause. Legacy messages are cleaned up automatically on update.

## [1.3.2] — 2026-07-05
### Added
- **`manage.sh doctor`** — one-command self-diagnosis of the whole chain: config
  placeholders, Frigate API reachability, camera/zone names (with typo hints),
  snapshots/record per camera, media on recent events, MQTT auth, Telegram token &
  chat access, service status.
- **Startup notice**: each monitor sends one silent "monitoring started" message to
  its chat (cameras, objects, zones, version) — a built-in bot/chat wiring test on
  every start. Disable per group with `"startup_message": False`.
- **Config validation on start**: clear "what exactly is wrong" errors (placeholders,
  missing fields) instead of tracebacks, in both the monitor and the pause controller.

## [1.3.1] — 2026-07-05
### Added
- Per-group `silent` option: `True` (default) keeps the current silent delivery,
  `False` makes notifications arrive with sound/vibration.

### Changed (internal)
- systemd units are generated by `manage.sh install` (separate `.service` files removed);
  units call the venv python directly.
- Friendlier guards: missing `python3`, venv creation hint (`apt install python3-venv`),
  no-systemd fallback (`manage.sh run`), root checks for systemd commands.

## [1.3.0] — 2026-07-05
### Changed
- **One script instead of four.** `manage.sh` gained `setup` (first-time install:
  dependencies + units + start) and `run [group]` (manual foreground run).
  `install_deps.sh`, `run_monitor.sh` and `server_setup.sh` are removed — fresh install
  is now just `cp config.example.py config.py`, edit, `sudo ./manage.sh setup`.
- Shell scripts and systemd units translated to English.

### Fixed
- Manual run never passed a group id to the monitor (it exited with usage);
  `manage.sh run` picks the group automatically when there is exactly one.

### Removed
- One-off `migrate` command (legacy pre-templated units, no longer needed).

## [1.2.0] — 2026-07-05
Code cleanup release. Default behavior is unchanged.

### Fixed
- **Config options now actually work.** `OBJECTS`, `LOG_LEVEL`, `LOG_FORMAT`,
  `STATS_INTERVAL` and `MEDIA_RETRY_ATTEMPTS` were documented but silently ignored
  (values were hardcoded). They are wired up now; missing options fall back to the
  previous defaults, so old configs keep working.
- **paho-mqtt 2.x compatibility.** Fresh installs pulled paho-mqtt 2.x, where
  `mqtt.Client()` without a callback API version fails. A version shim keeps both
  1.x and 2.x working.
- **Media downloaded once, not twice.** The snapshot and clip were fetched from
  Frigate to "check availability" and then re-downloaded for sending; now each file
  is downloaded a single time and sent as-is.

### Changed
- **Much quieter logs.** Routine poll dumps (every event listed every 3 s) moved from
  INFO to DEBUG — log files no longer grow by megabytes per hour. Set
  `LOG_LEVEL = "DEBUG"` to get the verbose output back.
- Log messages and code comments are in English (public repo).
- One shared HTTP session per monitor instead of a new one per request.
- `MEDIA_WAIT_TIME` (never used) replaced by `MEDIA_RETRY_DELAY` (seconds between
  download retries, default 3 — same effective behavior).

### Added
- Per-group `objects` option: override the global `OBJECTS` list for one group
  (e.g. `["person"]` for an indoor camera).
- README: the Frigate example now explicitly recommends
  `record.*.retain.mode: active_objects` (with `motion`, a motion mask can silently
  drop clips — the "photo but no video" trap).

## [1.1.1] — 2026-07-05
### Added
- **Diagnostic logging** per event: source (`mqtt` / `api-poll` / `api-retry`), event
  `duration`, time `since_end` before sending, and downloaded clip size in MB. Helps spot
  a clip that arrives shorter than the event (Frigate still finalizing the recording).

## [1.1.0] — 2026-07-05
### Added
- **English is now the primary language.** English `README.md`, `config.example.py`
  and this changelog. The Russian docs live in [README.ru.md](README.ru.md).
- **`LANG` option** (`en` / `ru`, default `en`) for the pause-controller interface
  (button labels and status text). Event notifications carry no text, so they are
  language-agnostic.

## [1.0.2] — 2026-07-05
### Fixed
- **No history spam on startup.** Previously every launch sent a batch of events from
  the last hour (the first `/api/events` poll treated all recent history as new, since
  the processed set was empty). Events that finished **before** startup are no longer
  sent. Live events (MQTT) work as before.
### Docs
- Added a Telegram example screenshot.

## [1.0.1] — 2026-07-05
### Fixed
- **Portable install.** systemd units are no longer tied to `/opt/frigate-tg`. Installing
  into another directory previously failed with `status=200/CHDIR`. `manage.sh install`
  now substitutes the real install path into the units.
- `server_setup.sh` runs from its own directory and checks for `config.py`.

## [1.0.0] — 2026-07-05
First public release.

### Features
- Frigate event monitoring over MQTT (`frigate/events`), sending photo + video to
  Telegram on person/car detection.
- Multiple camera groups — each to its own chat (`GROUPS`).
- Scales to any number of groups via a templated systemd unit `frigate-telegram@<group>`;
  `manage.sh` reads the group list from the config.
- Zone filtering (`zones`) — notify only when the object is in a chosen zone.
- Pause notifications with in-chat buttons (`mute_controller`): a reply keyboard at the
  bottom of the chat. Per-group, survives restarts. Toggled via `mute_controls`.
- Optional Telegram proxy.
- Management via `manage.sh` (install/start/stop/restart/status/logs/update/version).
