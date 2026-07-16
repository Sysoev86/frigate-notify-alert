"""Error reporting to GlitchTip (self-hosted, Sentry-compatible).

The DSN is read from the GLITCHTIP_DSN environment variable. If the variable is
not set, this module stays silent, so running without it works as before.
"""

import os

_enabled = False


def init(component: str) -> None:
    """Enable error reporting. component — a label for this process."""
    global _enabled
    dsn = (os.environ.get("GLITCHTIP_DSN") or "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("GLITCHTIP_ENV", "prod"),
        server_name=component,
        traces_sample_rate=0,
        send_default_pii=False,
    )
    sentry_sdk.set_tag("component", component)
    _enabled = True


def capture(exc) -> None:
    """Report a caught exception. Does nothing without init()."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
