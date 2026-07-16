"""Shared config validation and hints.

errors() — hard problems that stop the app (placeholders, missing fields).
hints()  — friendly, non-blocking suggestions: options that exist now but are
           missing from an older config.py (users who updated via
           `manage.sh update` never see new settings otherwise), and a language
           mismatch (Russian setup running the English interface).
"""

import os
import re

import config

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.join(_SCRIPT_DIR, "config.example.py")

# Documented top-level options that are safe to suggest. Anything else found in
# config.example.py is compared automatically; these are just never suggested.
_NEVER_SUGGEST = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_PROXY_URL", "GROUPS",
                  "MQTT_BROKER_HOST", "MQTT_BROKER_PORT", "MQTT_USERNAME",
                  "MQTT_PASSWORD", "MQTT_TOPIC_PREFIX", "FRIGATE_URL"}

_CYRILLIC = re.compile("[а-яёА-ЯЁ]")


def _placeholder(v) -> bool:
    return isinstance(v, str) and ("SET_ME" in v or "ВСТАВЬ" in v)


def errors() -> list:
    """Hard config problems — the app can't run. Empty list = fine."""
    problems = []

    groups = getattr(config, "GROUPS", None)
    if not isinstance(groups, dict) or not groups:
        return ["GROUPS is missing or empty"]

    for name in ("TELEGRAM_BOT_TOKEN", "MQTT_BROKER_HOST", "MQTT_USERNAME",
                 "MQTT_PASSWORD", "FRIGATE_URL"):
        v = getattr(config, name, None)
        if not v or _placeholder(v):
            problems.append(f"{name} is not filled in (placeholder left)")

    for gid, g in groups.items():
        if not isinstance(g, dict):
            problems.append(f"GROUPS['{gid}'] must be a dict")
            continue
        cid = str(g.get("telegram_chat_id") or "")
        if not cid or _placeholder(cid):
            problems.append(f"GROUPS['{gid}'].telegram_chat_id is not filled in")
        if not g.get("cameras"):
            problems.append(f"GROUPS['{gid}'].cameras is empty")
    return problems


def _example_options() -> list:
    """Top-level option names documented in config.example.py, in file order."""
    try:
        with open(_EXAMPLE, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    seen = []
    for name in re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", text, re.M):
        if name not in seen:
            seen.append(name)
    return seen


def _looks_russian() -> bool:
    """Does the user's setup look Russian? Group names are the best signal —
    they're written by the user and shown in the chat."""
    groups = getattr(config, "GROUPS", {}) or {}
    names = " ".join(str((g or {}).get("name", "")) for g in groups.values())
    return bool(_CYRILLIC.search(names))


def hints() -> list:
    """Non-blocking suggestions for an outdated config.py."""
    tips = []

    # Russian setup silently running the English interface
    if not hasattr(config, "LANG") and _looks_russian():
        tips.append('Group names are Russian, but LANG is unset → English. '
                    'Add  LANG = "ru"  to config.py.')

    # Options that exist now but are absent from this config
    missing = [o for o in _example_options()
               if o not in _NEVER_SUGGEST and not hasattr(config, o)]
    if missing:
        tips.append(f"New options in config.example.py: {', '.join(missing)} "
                    f"(defaults used).")
    return tips
