"""Pure camera geometry for the observatory's continuous zoom.

One camera holds a single transform: scale ``s`` and translation ``(tx, ty)``
mapping world coordinates to screen coordinates as ``screen = world * s + t``.
Every operation here is a pure function of numbers, so it can be tested without
a browser. The browser code mirrors these formulas exactly; nothing in this
module reads the DOM, the network, or a clock.

Invariants:
- ``zoom_at`` keeps the world point under the cursor at the same screen point.
- ``pan`` changes only the translation.
- ``rebase`` re-expresses the same view around a new world origin. It is an
  identity on what is visible: a world point maps to the same screen point
  before and after.
- Scale is clamped to ``[MIN_SCALE, MAX_SCALE]`` and every value stays finite.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_SCALE = 0.05
MAX_SCALE = 64.0


def _finite(value: float, name: str) -> float:
    # ``isinstance`` before ``float()`` on purpose. ``float("1")`` returns 1.0,
    # so a string arriving from JSON, a CLI flag or a query parameter used to be
    # silently coerced into a legal camera here, while the browser's
    # ``typeof v === "number"`` check refused the same value outright. The two
    # consumers then disagreed about whether the camera was valid.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True)
class Camera:
    scale: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def __post_init__(self) -> None:
        # The module contract is that every value stays finite, and the browser
        # refuses a non-numeric camera outright. Without this check a Camera
        # built from a coerced string ("1") or a NaN survived here and was only
        # caught later, or not at all, while the JS side returned null: the two
        # consumers disagreed about whether the camera was legal.
        object.__setattr__(self, "scale", _finite(self.scale, "scale"))
        object.__setattr__(self, "tx", _finite(self.tx, "tx"))
        object.__setattr__(self, "ty", _finite(self.ty, "ty"))


def clamp_scale(scale: float) -> float:
    return min(MAX_SCALE, max(MIN_SCALE, _finite(scale, "scale")))


def world_to_screen(cam: Camera, x: float, y: float) -> tuple[float, float]:
    return (x * cam.scale + cam.tx, y * cam.scale + cam.ty)


def screen_to_world(cam: Camera, sx: float, sy: float) -> tuple[float, float]:
    return ((sx - cam.tx) / cam.scale, (sy - cam.ty) / cam.scale)


def zoom_at(cam: Camera, factor: float, sx: float, sy: float) -> Camera:
    """Zoom by ``factor`` around the screen point ``(sx, sy)``.

    The world point under ``(sx, sy)`` stays under it. The factor is applied to
    the scale, then clamped; the translation is solved from the anchor so the
    anchor invariant holds exactly even when the clamp is hit.
    """
    factor = _finite(factor, "factor")
    if factor <= 0:
        raise ValueError("factor must be positive")
    sx = _finite(sx, "sx")
    sy = _finite(sy, "sy")
    wx, wy = screen_to_world(cam, sx, sy)
    scale = clamp_scale(cam.scale * factor)
    return Camera(scale=scale, tx=sx - wx * scale, ty=sy - wy * scale)


def pan(cam: Camera, dx: float, dy: float) -> Camera:
    return Camera(scale=cam.scale, tx=cam.tx + _finite(dx, "dx"), ty=cam.ty + _finite(dy, "dy"))


def rebase(cam: Camera, ox: float, oy: float) -> Camera:
    """Move the world origin to ``(ox, oy)`` without changing what is on screen.

    After rebase, a world point ``p`` is addressed as ``p - o``. The screen
    position of every point is unchanged, which keeps long zooms numerically
    stable: the translation stays small instead of growing without bound.
    """
    ox = _finite(ox, "ox")
    oy = _finite(oy, "oy")
    return Camera(scale=cam.scale, tx=cam.tx + ox * cam.scale, ty=cam.ty + oy * cam.scale)


def fit_rect(width: float, height: float, bounds: tuple[float, float, float, float]) -> Camera:
    """Camera that shows ``bounds`` (x, y, w, h) centred in a ``width x height`` viewport."""
    w = _finite(width, "width")
    h = _finite(height, "height")
    bx, by, bw, bh = (_finite(v, "bounds") for v in bounds)
    if w <= 0 or h <= 0 or bw <= 0 or bh <= 0:
        raise ValueError("viewport and bounds must have positive size")
    scale = clamp_scale(min(w / bw, h / bh) * 0.9)
    tx = (w - bw * scale) / 2 - bx * scale
    ty = (h - bh * scale) / 2 - by * scale
    return Camera(scale=scale, tx=tx, ty=ty)


def visible_rect(cam: Camera, width: float, height: float) -> tuple[float, float, float, float]:
    """World-space rectangle (x, y, w, h) currently visible in the viewport."""
    x0, y0 = screen_to_world(cam, 0.0, 0.0)
    x1, y1 = screen_to_world(cam, _finite(width, "width"), _finite(height, "height"))
    return (x0, y0, x1 - x0, y1 - y0)


def intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


# ---------------------------------------------------------------------------
# District frames
#
# A frame is a local coordinate system inside a shared global space:
#     global = origin + local * units        (units > 0)
# A camera always works in the local coordinates of its current frame, so
# entering or leaving a frame must re-express the same transform. The contract
# (mirrored by the browser code) for a transition F0 -> F1 is:
#     new_scale       = old_scale * (new_units / old_units)
#     new_translation = old_translation + (new_origin - old_origin) / old_units * old_scale
# Both axes use the same formula. Frame conversion never clamps: a result that
# is not finite or falls outside [MIN_SCALE, MAX_SCALE] is refused, and the
# input camera is left untouched.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    origin_x: float = 0.0
    origin_y: float = 0.0
    units: float = 1.0

    def __post_init__(self) -> None:
        ox = _finite(self.origin_x, "origin_x")
        oy = _finite(self.origin_y, "origin_y")
        units = _finite(self.units, "units")
        if units <= 0:
            raise ValueError("units must be positive")
        object.__setattr__(self, "origin_x", ox)
        object.__setattr__(self, "origin_y", oy)
        object.__setattr__(self, "units", units)


ROOT_FRAME = Frame(0.0, 0.0, 1.0)


def frame_to_global(frame: Frame, x: float, y: float) -> tuple[float, float]:
    """Local point in ``frame`` -> global point."""
    return (frame.origin_x + _finite(x, "x") * frame.units, frame.origin_y + _finite(y, "y") * frame.units)


def global_to_frame(frame: Frame, gx: float, gy: float) -> tuple[float, float]:
    """Global point -> local point in ``frame``."""
    return ((_finite(gx, "gx") - frame.origin_x) / frame.units, (_finite(gy, "gy") - frame.origin_y) / frame.units)


def transition_frame(cam: Camera, source: Frame, target: Frame) -> Camera:
    """Re-express ``cam`` (local to ``source``) in local coordinates of ``target``.

    Every selected point keeps its screen position. The result is computed
    completely before it is returned, so a refused transition has no effect.
    Raises ValueError when any intermediate or result is not finite or the new
    scale is outside ``[MIN_SCALE, MAX_SCALE]``.
    """
    if not isinstance(source, Frame) or not isinstance(target, Frame):
        raise ValueError("source and target must be Frame instances")
    # A refused transition must raise ValueError, not leak an AttributeError from
    # touching a missing camera. The browser returns null for a null camera, and
    # the contract here is ValueError; anything else lets a caller's except
    # clause miss the refusal entirely.
    if not isinstance(cam, Camera):
        raise ValueError("cam must be a Camera instance")
    s0 = _finite(cam.scale, "scale")
    t0x = _finite(cam.tx, "tx")
    t0y = _finite(cam.ty, "ty")
    ratio = _finite(target.units / source.units, "units ratio")
    s1 = _finite(s0 * ratio, "scale")
    if not (MIN_SCALE <= s1 <= MAX_SCALE):
        raise ValueError("transition would leave the allowed scale range")
    dx = _finite((target.origin_x - source.origin_x) / source.units * s0, "translation")
    dy = _finite((target.origin_y - source.origin_y) / source.units * s0, "translation")
    tx1 = _finite(t0x + dx, "tx")
    ty1 = _finite(t0y + dy, "ty")
    return Camera(scale=s1, tx=tx1, ty=ty1)


def enter_frame(cam: Camera, parent: Frame, child: Frame) -> Camera:
    """Camera in ``parent`` coordinates -> camera in ``child`` coordinates."""
    return transition_frame(cam, parent, child)


def leave_frame(cam: Camera, child: Frame, parent: Frame) -> Camera:
    """Camera in ``child`` coordinates -> camera in ``parent`` coordinates."""
    return transition_frame(cam, child, parent)


def local_bounds(global_bounds, frame: Frame):
    """Global (x, y, w, h) -> local (x, y, w, h) in ``frame``; None when invalid.

    Mirrors the browser ``localBounds``: refuses non-finite input, a non-positive
    size, or a frame with non-finite origin or non-positive units. Never raises.
    """
    if not isinstance(frame, Frame):
        return None
    try:
        raw = list(global_bounds[:4])
    except (TypeError, IndexError):
        return None
    if len(raw) < 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        return None
    x, y, w, h = (float(v) for v in raw)
    vals = (x, y, w, h, frame.origin_x, frame.origin_y, frame.units)
    if not all(math.isfinite(v) for v in vals) or frame.units <= 0:
        return None
    lx = (x - frame.origin_x) / frame.units
    ly = (y - frame.origin_y) / frame.units
    lw = w / frame.units
    lh = h / frame.units
    out = (lx, ly, lw, lh)
    if not all(math.isfinite(v) for v in out) or lw <= 0 or lh <= 0:
        return None
    return out


# Semantic zoom: on-screen width (px) thresholds. Level = number of edges reached.
# L0 < 24px (aggregate only), L1 >= 24, L2 >= 80, L3 >= 160, L4 >= 320, L5 >= 600.
LOD_EDGES = (24.0, 80.0, 160.0, 320.0, 600.0)


def lod_level(px_width: float) -> int:
    """Semantic zoom level 0..5 for a node drawn ``px_width`` pixels wide. Non-finite -> 0."""
    if not isinstance(px_width, (int, float)) or not math.isfinite(px_width):
        return 0
    return sum(1 for edge in LOD_EDGES if px_width >= edge)
