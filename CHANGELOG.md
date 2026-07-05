# Changelog

All notable changes. Format follows [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`.

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
