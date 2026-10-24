"""Hermes native plugin entrypoint: observe-only Nerve live telemetry (staging).

Off unless HERMES_NERVE_LIVE_ENABLED == "1". When off, register() returns
without creating files, threads, or hooks.
"""

try:  # Hermes loads this file as a package module
    from . import live_feed as _live_feed
except ImportError:  # pragma: no cover - direct import fallback
    import live_feed as _live_feed


def register(ctx):
    """Hermes plugin entrypoint. Always returns None."""
    _live_feed.register(ctx)
    return None


# Alias requested by the integration spec.
registration = register
