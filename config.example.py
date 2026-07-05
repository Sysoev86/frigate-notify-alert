# ============================================================================
#  Frigate -> Telegram configuration
# ----------------------------------------------------------------------------
#  This is the only file you edit. Copy it to config.py first:
#      cp config.example.py config.py
#
#  Anything written in UPPERCASE inside quotes ("SET_ME_...") is a placeholder
#  you must replace. Quotes around strings are required (this is Python);
#  numbers (like ports) have no quotes.
#
#  Full docs: https://github.com/Sysoev86/frigate-notify-alert#readme
# ============================================================================


# ---------------------------------------------------------------------------
#  1. TELEGRAM — the bot that sends notifications
# ---------------------------------------------------------------------------

# Bot token.
#   How to get it:
#   1) In Telegram open @BotFather -> /newbot -> set a name and username.
#   2) BotFather sends a string like 1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   3) Paste it here, in quotes.
TELEGRAM_BOT_TOKEN = "SET_ME_BOT_TOKEN"

# Proxy for Telegram.
#   Needed ONLY if Telegram is blocked by your ISP and the bot can't reach
#   Telegram servers. In most cases no proxy is needed -> leave it as:
#       TELEGRAM_PROXY_URL = None
#   If you do need a proxy, use the format:
#       TELEGRAM_PROXY_URL = "http://LOGIN:PASSWORD@PROXY_IP:PORT"
#   (None is written without quotes — it means "empty".)
TELEGRAM_PROXY_URL = None


# ---------------------------------------------------------------------------
#  2. CAMERA GROUPS — what to send and where
# ---------------------------------------------------------------------------
#  A group = a set of cameras + one chat that receives their notifications.
#  You can have one or many groups. If one group is enough, keep only "group1"
#  and delete the "group2" block (together with the comma before it).
#
#  Each group takes:
#    telegram_chat_id — WHERE to send (see "how to find" below)
#    cameras          — LIST of camera names, EXACTLY as in your Frigate
#    zones            — OPTIONAL. List of Frigate zones. If set, a notification
#                       is sent only when the object entered one of these zones
#                       (handy to ignore passers-by). Omit / empty list = notify
#                       for the whole camera. Zone names come from Frigate:
#                       config.yml -> cameras.<camera>.zones.
#    objects          — OPTIONAL. Override the global OBJECTS list for this
#                       group only (e.g. ["person"] for an indoor camera).
#    silent           — OPTIONAL. True (default) = messages arrive silently
#                       (no sound/vibration). False = full loud notifications.
#    mute_controls    — OPTIONAL. True (default) = pause buttons appear in the
#                       chat (15m/1h/3h/until morning). False = no buttons.
#                       Requires the mute_controller service (see README).
#    name             — any label, used only in logs.
#
#  How to find telegram_chat_id:
#    - DM: message @userinfobot — it shows your numeric id.
#    - Group/channel: add YOUR bot to the chat, then message @getidsbot.
#      Group IDs usually start with -100...
#      Note: the bot MUST be a member of the chat, or it can't post there.
#
#  How to find camera names (cameras):
#    They are the keys under `cameras:` in Frigate's config.yml. Type them
#    exactly (case-sensitive). Example from Frigate:
#        cameras:
#          yard:        <- use "yard"
#          entrance:
GROUPS = {
    "group1": {
        "telegram_chat_id": "SET_ME_CHAT_ID_1",   # chat for the first group
        "cameras": ["yard"],                       # <- your camera names
        # "zones": ["zone_yard"],                  # <- uncomment to notify only by zone
        # "silent": False,                         # <- False = loud notifications (default: silent)
        "mute_controls": True,                     # pause buttons in the chat
        "name": "Group 1 (yard)",
    },
    "group2": {
        "telegram_chat_id": "SET_ME_CHAT_ID_2",   # chat for the second group
        "cameras": [                               # <- multiple cameras, comma-separated
            "entrance",
            "gate",
            "parking",
        ],
        # "zones": ["zone_gate", "zone_parking"],  # <- optional; omit = whole camera
        "mute_controls": True,
        "name": "Group 2 (outside)",
    },
}


# ---------------------------------------------------------------------------
#  3. MQTT — how the script learns about Frigate events
# ---------------------------------------------------------------------------
#  Frigate publishes events to an MQTT broker; the script reads them there.
#  Take these values from your Frigate config, the `mqtt:` section. The broker
#  (Mosquitto) usually runs on the same host as Frigate.

MQTT_BROKER_HOST = "SET_ME_MQTT_BROKER_IP"    # e.g. "192.168.1.50"
MQTT_BROKER_PORT = 1883                        # standard MQTT port, rarely changed
MQTT_USERNAME = "SET_ME_MQTT_USER"             # from mqtt: user in Frigate config
MQTT_PASSWORD = "SET_ME_MQTT_PASSWORD"         # from mqtt: password
MQTT_TOPIC_PREFIX = "frigate"                  # topic prefix, default "frigate"


# ---------------------------------------------------------------------------
#  4. FRIGATE — where the script downloads the event photo and video
# ---------------------------------------------------------------------------
#  Frigate web URL. Open Frigate in a browser and check the address.
#  Usually http://IP:5000 (5000 is the standard Frigate port).
FRIGATE_URL = "http://SET_ME_FRIGATE_IP:5000"


# ---------------------------------------------------------------------------
#  5. OBJECTS — what to react to
# ---------------------------------------------------------------------------
#  A notification is sent only if Frigate recognized one of these objects.
#  Names are the standard Frigate labels. Trim as needed, e.g. ["person"]
#  if you only care about people.
OBJECTS = ["person", "car", "truck", "bus", "motorcycle", "bicycle"]


# ---------------------------------------------------------------------------
#  6. INTERFACE / MISC — usually no need to change
# ---------------------------------------------------------------------------
LANG = "en"                # pause-controller language: "en" or "ru"
LOG_LEVEL = "INFO"         # log level (INFO / DEBUG; DEBUG shows every poll)
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
STATS_INTERVAL = 60        # how often (seconds) to write stats to the log
MEDIA_RETRY_ATTEMPTS = 15  # how many times to retry downloading photo/video
MEDIA_RETRY_DELAY = 3      # seconds between download retries
