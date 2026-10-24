"""Control-room UI for the Nerve observatory.

Design: dense blue-gray control wall, amber/green/red annunciator tiles, and
analog-style gauge bays. Every value shown is an actual entity or a finite real
measurement from the collector. Missing values render as explicit MISSING,
UNAVAILABLE, STALE or UNRECORDED states; nothing is invented.

Security: the static shell is data-free. All operational data arrives through
authenticated /api/* calls, and the bearer token is held in sessionStorage only
(never in a URL). Rendering uses textContent exclusively; there is no innerHTML,
eval, or external asset.
"""

from __future__ import annotations

from pathlib import Path

_ASSET_DIR = Path(__file__).resolve().parent / "assets"

# Served asset name -> file under _ASSET_DIR. These are the only names that are
# resolved lazily; everything else in this module stays a normal constant.
_ASSET_FILES = {"APP_CSS": "app.css", "APP_JS": "app.js", "FACTORY_CSS": "factory.css", "FACTORY_JS": "factory.js"}

# Cache keyed on (path, mtime_ns, size) -> decoded text. Cleared whenever a new
# key is inserted, so at most one entry is ever live per asset: any edit changes
# the file's key and forces the next access to re-read from disk.
_ASSET_CACHE: dict[tuple[str, str, int, int], str] = {}


def _read_asset(filename: str) -> str:
    """Read an asset file fresh, cached on (path, mtime_ns, size).

    Why this exists: server.py serves /app.js and /app.css through
    `ui.APP_JS` / `ui.APP_CSS`. While those were plain module-level string
    constants, Python bound them once at import, so a long-running server kept
    serving the pre-import snapshot forever and any later edit to the asset was
    invisible to every browser that connected. Resolving per access via
    __getattr__ is what makes staleness structurally impossible.

    Never serves an empty payload: a missing or unreadable asset raises
    RuntimeError carrying the resolved path so the failure is actionable.
    """
    path = _ASSET_DIR / filename
    try:
        stat = path.stat()
    except OSError as exc:
        raise RuntimeError(
            f"observatory asset missing or unreadable: {path} "
            f"(package the assets/ directory alongside ui.py)"
        ) from exc
    key = (filename, str(path), stat.st_mtime_ns, stat.st_size)
    cached = _ASSET_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"observatory asset could not be read: {path} "
            f"(check file permissions)"
        ) from exc
    # Drop entries for this asset only: a replaced file must not be shadowed.
    for stale_key in [k for k in _ASSET_CACHE if k[0] == filename]:
        del _ASSET_CACHE[stale_key]
    _ASSET_CACHE[key] = text
    return text


def __getattr__(name: str) -> str:
    """PEP 562 module-level hook: resolve APP_CSS / APP_JS on every access.

    Deliberately NOT `APP_CSS = _read_asset("app.css")` at module scope — that
    evaluates once at import and reintroduces exactly the staleness this
    module exists to prevent. Because __getattr__ runs on each attribute
    lookup, `ui.APP_CSS` always reflects the current on-disk bytes, and every
    existing consumer (`ui.APP_JS` in server.py and the UI tests) keeps working
    unchanged.
    """
    filename = _ASSET_FILES.get(name)
    if filename is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return _read_asset(filename)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nerve Observatory</title>
<link rel="stylesheet" href="/app.css">
<link rel="stylesheet" href="/factory.css">
</head>
<body>
<header class="panel-top">
  <h1 class="plate">NERVE CONTROL ROOM</h1>
  <div class="master" id="master" role="status" aria-live="polite">
    <span class="lamp" id="master-lamp" aria-hidden="true"></span>
    <span id="master-text">CONNECTING</span>
  </div>
</header>
<section id="auth" class="auth" hidden aria-labelledby="auth-title">
  <h2 id="auth-title" class="plate-sm">ACCESS TOKEN REQUIRED</h2>
  <label for="token">Bearer token</label>
  <input id="token" type="password" autocomplete="off" spellcheck="false">
  <button id="token-save" type="button">CONNECT</button>
  <p class="muted">Held in this browser tab only. Never placed in a URL.</p>
</section>
<nav aria-label="Breadcrumb" class="crumbs"><ol id="crumbs"></ol></nav>
<div class="toolbar">
  <button id="back" type="button">&larr; BACK</button>
  <button id="refresh" type="button">REFRESH</button>
  <button id="mode" type="button" aria-pressed="true">SHOW LEGACY LEVELS</button>
  <button id="factory-toggle" type="button" aria-pressed="true">SHOW FACTORY</button>
  <span class="muted" id="updated">NO DATA YET</span>
</div>
<main id="view" tabindex="-1" aria-live="polite"></main>
<footer class="muted">READ-ONLY. Authority stays with the primary supervisor. Keys: Enter opens, Backspace or Escape goes up, R refreshes.</footer>
<script src="/factory.js"></script>
<script src="/app.js"></script>
</body>
</html>
"""

# APP_CSS and APP_JS are intentionally NOT bound here. They resolve through the
# module-level __getattr__ above, which reads hermes_nerve/observatory/assets/
# on every access, so a running server always serves the current on-disk bytes.

