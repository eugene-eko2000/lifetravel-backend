"""
Patches cdp_use's InputClient.dispatchMouseEvent so every mouse interaction
mirrors how a human physically uses a mouse:

  mouseMoved    → replaced with a Bézier curved path (slow→fast→slow)
  mousePressed  → preceded by micro-hover tremor + dwell pause; click coords
                  are slightly offset from the exact element centre
  mouseReleased → 50–150 ms hold, then release; followed by lift-off drift
  everything else passes through unchanged

How it works
------------
browser_use dispatches element clicks as:
  1. mouseMoved   → teleports cursor to target  (replaced with Bézier path)
  2. mousePressed at target                     (prefixed with hover + dwell)
  3. mouseReleased at target                    (suffixed with lift-off drift)

We intercept at InputClient.dispatchMouseEvent — the lowest possible level in
the cdp_use stack — so the patch covers every click path in browser_use with
no code duplication.  Idempotent: safe to call multiple times.

Interaction model
-----------------
  Bézier path    – cubic curve, random perp-offset control points → natural arc
  Ease-in-out    – inverse ease-in-out-sine timing per waypoint → slow→fast→slow
  Hand tremor    – Gaussian micro-jitter on intermediate waypoints
  Pre-click hover– 2–4 tiny random moves simulating the hand settling on target;
                   skipped entirely when the cursor is already inside the target
                   element's bounding box (post-modal-close, accordion expand, …)
  Dwell pause    – 200–500 ms between cursor stop and mousedown (reaction time)
  Click offset   – Gaussian offset from exact element centre (humans miss centre)
  Click hold     – 50–150 ms between mousedown and mouseup (button hold time);
                   overridable per-task via the ``click_hold(duration)``
                   context manager for click-and-hold / long-press scenarios
  Lift-off drift – small movement after mouseReleased (natural finger rebound)
  Global tracking– every path starts from the last known cursor position

Visualization
-------------
When HEADLESS=false (or visualize=True is passed to patch_mouse_movement),
each cursor update is mirrored into the live page as a small overlay so the
operator can watch the simulated motion in the visible browser window:
  • red dot           – current cursor position
  • fading red trail  – recent path
  • green expanding ring + green dot tint – mousedown
  • back to red       – mouseup
"""

import asyncio
import math
import os
import random
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

logger = logging.getLogger("scraper_common.human_mouse")

# ── Global cursor position ────────────────────────────────────────────────────
_mouse_x: float = 100.0
_mouse_y: float = 100.0

# ── Bézier movement tuning ────────────────────────────────────────────────────
_TIME_BASE_S      = 0.10    # minimum path duration even for tiny moves
_TIME_PER_PX      = 0.00035 # +0.35 ms per pixel of travel
_TIME_JITTER      = 0.10    # ±10 % speed variation
_TIME_MAX_S       = 0.55    # cap so very long moves don't feel laggy
_MIN_STEP_SLEEP_S = 0.004   # 4 ms floor per waypoint step
_STEP_JITTER      = 0.15    # ±15 % random fluctuation on each step's sleep time
_TREMOR_SIGMA     = 1.0     # std-dev of Gaussian jitter on intermediate points (~95% within ±2 px)
_TREMOR_CLAMP     = 2.0     # hard cap on per-axis jitter magnitude (pixels)

# Curve radius is sampled from one of three categories so successive moves
# visibly differ in arc shape — tight darts, normal arcs, and wide loopy ones.
# (probability, bulge_min_frac, bulge_max_frac)
_BULGE_CATEGORIES = (
    (0.30, 0.06, 0.13),   # tight    — short, near-direct moves
    (0.45, 0.13, 0.25),   # medium   — typical reach
    (0.25, 0.25, 0.45),   # wide     — loose, swinging path
)
_BULGE_FLOOR_PX   = 6.0   # absolute minimum bulge in pixels for short hops

# Asymmetric easing: peak velocity is shifted earlier in time, giving a
# longer, gradual deceleration phase before the click — matches measured
# ballistic-then-corrective human reach profiles.
_DECEL_SKEW_MIN   = 0.36
_DECEL_SKEW_MAX   = 0.46  # 0.50 would be symmetric ease-in-out-sine

# ── Pre-click hover tuning ────────────────────────────────────────────────────
_HOVER_STEPS_MIN  = 2       # fewest micro-hover movements before mousedown
_HOVER_STEPS_MAX  = 4       # most  micro-hover movements before mousedown
_HOVER_SIGMA      = 1.5     # std-dev of each hover micro-offset (pixels)
_HOVER_STEP_MIN_S = 0.012   # min delay between hover micro-steps
_HOVER_STEP_MAX_S = 0.030   # max delay between hover micro-steps
_DWELL_MIN_S      = 0.20    # min pause between cursor stop and mousedown
_DWELL_MAX_S      = 0.50    # max pause between cursor stop and mousedown

# ── Click position offset ─────────────────────────────────────────────────────
_CLICK_SIGMA_FRAC        = 0.33  # σ as a fraction of element dimension
_CLICK_SIGMA_MIN_PX      = 3.0   # floor on σ for tiny elements
_CLICK_MARGIN_FRAC       = 0.05  # inset margin as fraction of element dimension
_CLICK_MARGIN_MIN_PX     = 2.0   # absolute inset floor
_CLICK_FALLBACK_SIGMA_PX = 4.0   # fallback σ when no bounding box

# ── Button hold (mousedown → mouseup) ─────────────────────────────────────────
_CLICK_HOLD_MIN_S = 0.050   # min time the button stays pressed (default range)
_CLICK_HOLD_MAX_S = 0.150   # max time the button stays pressed (default range)

# Per-task override for the click-hold duration. When set, the next
# mouseReleased event will hold the button down for exactly this many seconds
# instead of drawing from the default 50–150 ms range. Backed by a ContextVar
# so concurrent agents in different asyncio tasks don't trample each other.
_click_hold_override: ContextVar[Optional[float]] = ContextVar(
    "_human_mouse_click_hold_override", default=None
)


@contextmanager
def click_hold(duration: float) -> Iterator[None]:
    """
    Force every click dispatched inside this block to hold the mouse button
    down for exactly ``duration`` seconds before releasing.

    Use for long-press tests, anti-bot duration checks, or as the hold phase
    of a drag gesture. Per-asyncio-task scoped, so parallel agents are safe.

        with click_hold(2.5):
            await page.click("#submit")     # this click holds 2.5 s
        await page.click("#cancel")         # back to default 50–150 ms
    """
    if duration < 0:
        raise ValueError(f"click_hold duration must be ≥ 0, got {duration}")
    token = _click_hold_override.set(duration)
    try:
        yield
    finally:
        _click_hold_override.reset(token)


def set_click_hold_override(duration: Optional[float]) -> None:
    """
    Imperative variant of :func:`click_hold` for callers that can't wrap the
    click in a ``with`` block (e.g. event-driven flows). Pass a float to set,
    or ``None`` to clear. Sticks until cleared — affects every subsequent
    click in the current asyncio task.
    """
    if duration is not None and duration < 0:
        raise ValueError(f"click_hold duration must be ≥ 0, got {duration}")
    _click_hold_override.set(duration)

# ── Post-release lift-off tuning ──────────────────────────────────────────────
_LIFT_DELAY_MIN_S = 0.018   # pause before the post-release drift move
_LIFT_DELAY_MAX_S = 0.045
_LIFT_SIGMA       = 1.2     # std-dev of lift-off drift (pixels)

# ── Visualization state ───────────────────────────────────────────────────────
_VIZ_ENABLED: bool = False  # set by patch_mouse_movement()
_VIZ_OK: bool = True        # auto-disabled if Runtime.evaluate fails
_VIZ_TASKS: set[asyncio.Task] = set()

# Renders/updates the overlay. Runs in the page; safe to call repeatedly.
# X/Y/KIND are templated in by Python (kind ∈ "move"|"press"|"release").
_VIZ_JS_TEMPLATE = r"""
(function(x, y, kind) {
  try {
    var root = document.documentElement;
    if (!root) return;
    var r = window.__humanMouseViz;
    if (!r || !root.contains(r.dot)) {
      var dot = document.createElement('div');
      dot.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;border-radius:50%;background:rgba(255,40,40,0.65);border:2px solid #fff;pointer-events:none;z-index:2147483647;transform:translate(-50%,-50%);box-shadow:0 0 8px rgba(0,0,0,0.6);transition:background 80ms linear';
      root.appendChild(dot);
      r = window.__humanMouseViz = { dot: dot };
    }
    r.dot.style.left = x + 'px';
    r.dot.style.top = y + 'px';
    if (kind === 'press') {
      r.dot.style.background = 'rgba(20,200,20,0.9)';
      var ring = document.createElement('div');
      ring.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;width:18px;height:18px;border-radius:50%;border:2px solid rgba(20,200,20,0.9);pointer-events:none;z-index:2147483646;transform:translate(-50%,-50%);transition:width 500ms linear,height 500ms linear,opacity 500ms linear';
      root.appendChild(ring);
      requestAnimationFrame(function () { ring.style.width = '60px'; ring.style.height = '60px'; ring.style.opacity = '0'; });
      setTimeout(function () { ring.remove(); }, 600);
    } else if (kind === 'release') {
      r.dot.style.background = 'rgba(255,40,40,0.65)';
    }
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;width:5px;height:5px;border-radius:50%;background:rgba(255,40,40,0.45);pointer-events:none;z-index:2147483645;transform:translate(-50%,-50%);transition:opacity 600ms linear';
    root.appendChild(t);
    requestAnimationFrame(function () { t.style.opacity = '0'; });
    setTimeout(function () { t.remove(); }, 700);
  } catch (e) { /* don't break page */ }
})(__X__, __Y__, "__KIND__");
""".strip()


def _viz_active() -> bool:
    return _VIZ_ENABLED and _VIZ_OK


def _emit_viz(client: Any, session_id: Optional[str], x: float, y: float, kind: str) -> None:
    """Fire-and-forget overlay update. Errors disable viz for the rest of the run."""
    if not _viz_active():
        return
    expr = (_VIZ_JS_TEMPLATE
            .replace("__X__", str(int(round(x))))
            .replace("__Y__", str(int(round(y))))
            .replace("__KIND__", kind))

    async def _send() -> None:
        global _VIZ_OK
        try:
            await client.send_raw(
                method="Runtime.evaluate",
                params={
                    "expression": expr,
                    "returnByValue": False,
                    "awaitPromise": False,
                    "silent": True,
                },
                session_id=session_id,
            )
        except Exception as exc:
            _VIZ_OK = False
            logger.debug("Mouse visualization disabled after error: %s", exc)

    try:
        task = asyncio.create_task(_send())
        _VIZ_TASKS.add(task)
        task.add_done_callback(_VIZ_TASKS.discard)
    except RuntimeError:
        # No running loop — give up silently rather than blocking real input
        pass


# ── Element-bounds query (for randomised click position inside element) ──────

# Element-size bounds for treating the bounds query as trustworthy. Anything
# outside is either too tiny (not a real click target) or too large (probably
# the body/a container, where spreading the click would miss the actual button).
_BOUNDS_MIN_PX = 8.0
_BOUNDS_MAX_PX = 400.0


def _point_inside_box(
    x: float,
    y: float,
    bounds: tuple[float, float, float, float],
    edge_margin: float = 2.0,
) -> bool:
    """True when (x,y) sits at least edge_margin pixels in from each side of bounds."""
    bx, by, bw, bh = bounds
    return (
        (bx + edge_margin) <= x <= (bx + bw - edge_margin)
        and (by + edge_margin) <= y <= (by + bh - edge_margin)
    )


async def _query_element_bounds(
    client: Any, session_id: Optional[str], x: float, y: float
) -> Optional[tuple[float, float, float, float]]:
    """
    Return (left, top, width, height) of the topmost element at (x,y) in
    viewport coordinates, or None if it can't be measured. Failures are
    swallowed silently — the caller should fall back to the centre+jitter path.
    """
    expr = (
        "(function(x,y){"
        "var e=document.elementFromPoint(x,y);"
        "if(!e)return null;"
        "var r=e.getBoundingClientRect();"
        "return [r.left,r.top,r.width,r.height];"
        f"}})({x},{y})"
    )
    try:
        res = await client.send_raw(
            method="Runtime.evaluate",
            params={
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": False,
                "silent": True,
            },
            session_id=session_id,
        )
        val = res.get("result", {}).get("value")
        if not val or len(val) != 4:
            return None
        return tuple(float(v) for v in val)  # type: ignore[return-value]
    except Exception:
        return None


# ── CDP param helpers ─────────────────────────────────────────────────────────

def _move_params(x: int, y: int, buttons: int = 0) -> dict:
    """
    Complete CDP params for a mouseMoved event.

    Including pointerType, button, buttons, and modifiers ensures Chrome's
    PointerEventManager generates the full pointer event sequence alongside the
    mouse event: pointermove (+ pointerover/pointerenter at element boundaries).
    Omitting any of these causes Chrome to skip or misclassify the conversion.
    """
    return {
        "type": "mouseMoved",
        "x": x,
        "y": y,
        "button": "none",
        "buttons": buttons,
        "modifiers": 0,
        "pointerType": "mouse",
    }



# ── Math helpers ──────────────────────────────────────────────────────────────

def _ease_inv(p: float, skew: float = 0.5) -> float:
    """
    Inverse easing — maps normalised spatial position p ∈ [0,1] to normalised
    elapsed time t ∈ [0,1]. Used to compute per-step time budget for
    equally-spaced spatial waypoints, producing the slow→fast→slow profile.

      skew = 0.5  → symmetric ease-in-out-sine (acceleration mirrors deceleration)
      skew < 0.5  → peak velocity shifted earlier in time → quicker acceleration,
                    longer/gentler deceleration toward the click target.

    Real human mouse moves measured in HCI studies show a peak ~30–45 % into
    the trajectory rather than dead centre, so we draw skew randomly per move.
    """
    p = max(0.0, min(1.0, p))
    base = math.acos(max(-1.0, min(1.0, 1.0 - 2.0 * p))) / math.pi
    # Symmetric sine-shaped distortion that preserves the [0,1] endpoints —
    # nudges the curve below `base` when skew<0.5, so a given spatial midpoint
    # is reached earlier in time → first half fast, second half slow.
    bias = (0.5 - skew) * math.sin(math.pi * base) * 0.4
    return max(0.0, min(1.0, base - bias))


def _sample_bulge_fraction() -> tuple[float, float]:
    """
    Pick a (min, max) bulge-fraction range from the categorical distribution
    in _BULGE_CATEGORIES. Returns the bracket the actual sample is drawn from
    so callers can also report the chosen category.
    """
    r = random.random()
    cum = 0.0
    for prob, bmin, bmax in _BULGE_CATEGORIES:
        cum += prob
        if r <= cum:
            return bmin, bmax
    bmin, bmax = _BULGE_CATEGORIES[-1][1], _BULGE_CATEGORIES[-1][2]
    return bmin, bmax


def _bezier_cubic(p0: tuple, p1: tuple, p2: tuple, p3: tuple, t: float) -> tuple:
    mt = 1.0 - t
    x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
    y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
    return x, y


def _build_path(sx: float, sy: float, ex: float, ey: float) -> list[tuple[int, int]]:
    """
    Return integer (x, y) waypoints forming a curved path from (sx,sy) to
    (ex,ey) via a cubic Bézier with randomly offset control points.
    """
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return [(round(ex), round(ey))]

    n = max(10, min(50, int(distance / 12)))
    px, py = (-dy / distance, dx / distance)   # unit perpendicular
    bmin, bmax = _sample_bulge_fraction()
    bulge = max(_BULGE_FLOOR_PX, distance * random.uniform(bmin, bmax))

    # Independent sides per control point — same side gives a single arc,
    # opposite sides produce a gentle S-curve. Both are observed in real humans.
    side1 = random.choice((-1.0, 1.0))
    side2 = side1 if random.random() < 0.7 else -side1

    cp1 = (
        sx + dx * random.uniform(0.20, 0.40) + px * side1 * bulge * random.uniform(0.5, 1.0),
        sy + dy * random.uniform(0.20, 0.40) + py * side1 * bulge * random.uniform(0.5, 1.0),
    )
    cp2 = (
        sx + dx * random.uniform(0.60, 0.80) + px * side2 * bulge * random.uniform(0.5, 1.0),
        sy + dy * random.uniform(0.60, 0.80) + py * side2 * bulge * random.uniform(0.5, 1.0),
    )

    points: list[tuple[int, int]] = []
    for i in range(1, n + 1):
        t = i / n
        x, y = _bezier_cubic((sx, sy), cp1, cp2, (ex, ey), t)
        if i < n:                           # jitter on intermediate points only
            jx = max(-_TREMOR_CLAMP, min(_TREMOR_CLAMP, random.gauss(0.0, _TREMOR_SIGMA)))
            jy = max(-_TREMOR_CLAMP, min(_TREMOR_CLAMP, random.gauss(0.0, _TREMOR_SIGMA)))
            x += jx
            y += jy
        points.append((round(x), round(y)))

    return points


# ── Patch entry point ─────────────────────────────────────────────────────────

def patch_mouse_movement(visualize: Optional[bool] = None) -> None:
    """
    Replace InputClient.dispatchMouseEvent with a full human-interaction
    simulation covering movement, hover, click offset, hold, and lift-off.

    visualize:
      True  → always render the in-page overlay
      False → never render
      None  → enable when HEADLESS=false in env (matches cfg.Cfg.headless)
    """
    global _VIZ_ENABLED
    if visualize is None:
        _VIZ_ENABLED = os.getenv("HEADLESS", "true").lower() == "false"
    else:
        _VIZ_ENABLED = visualize

    try:
        from cdp_use.cdp.input.library import InputClient
    except ImportError:
        logger.warning("cdp_use InputClient not found — human mouse patches skipped")
        return

    if getattr(InputClient, "_human_mouse_patched", False):
        logger.debug("Human mouse patch already applied; visualize=%s", _VIZ_ENABLED)
        return

    _orig = InputClient.dispatchMouseEvent

    async def _human_dispatch(
        self, params: dict, session_id: Optional[str] = None
    ) -> dict:
        global _mouse_x, _mouse_y

        event_type = params.get("type", "")
        print(f"dispatchMouseEvent: {event_type}")

        # ── mouseMoved → Bézier curved path ──────────────────────────────────
        if event_type == "mouseMoved":
            target_x = float(params.get("x", _mouse_x))
            target_y = float(params.get("y", _mouse_y))
            distance = math.hypot(target_x - _mouse_x, target_y - _mouse_y)

            if distance < 1.0:
                _mouse_x, _mouse_y = target_x, target_y
                _emit_viz(self._client, session_id, target_x, target_y, "move")
                return await _orig(self, params, session_id=session_id)

            # Cursor already on (inside) the target element? Don't move it.
            # This happens after a screen change (modal close, lazy-loaded card,
            # accordion expand) where the new clickable lands under the cursor's
            # current position. A real human wouldn't move the mouse in that case.
            if distance < 200.0:
                bounds = await _query_element_bounds(
                    self._client, session_id, target_x, target_y
                )
                if (
                    bounds is not None
                    and _BOUNDS_MIN_PX <= bounds[2] <= _BOUNDS_MAX_PX
                    and _BOUNDS_MIN_PX <= bounds[3] <= _BOUNDS_MAX_PX
                    and _point_inside_box(_mouse_x, _mouse_y, bounds)
                ):
                    logger.debug(
                        "mouseMoved suppressed — cursor (%.0f,%.0f) already inside "
                        "target element (%.0f,%.0f %.0fx%.0f)",
                        _mouse_x, _mouse_y, bounds[0], bounds[1], bounds[2], bounds[3],
                    )
                    # No-op dispatch so the CDP layer still sees an event;
                    # the cursor literally doesn't travel anywhere.
                    return await self._client.send_raw(
                        method="Input.dispatchMouseEvent",
                        params=_move_params(round(_mouse_x), round(_mouse_y)),
                        session_id=session_id,
                    )

            total_time = _TIME_BASE_S + distance * _TIME_PER_PX
            total_time = min(
                _TIME_MAX_S,
                total_time * random.uniform(1.0 - _TIME_JITTER, 1.0 + _TIME_JITTER),
            )
            path = _build_path(_mouse_x, _mouse_y, target_x, target_y)
            n = len(path)

            # Per-move asymmetric ease — peak velocity reached at ~36-46%
            # through the path, leaving a longer deceleration phase.
            skew = random.uniform(_DECEL_SKEW_MIN, _DECEL_SKEW_MAX)

            logger.debug(
                "Mouse path (%.0f,%.0f)→(%.0f,%.0f) dist=%.0f steps=%d time=%.2fs skew=%.2f",
                _mouse_x, _mouse_y, target_x, target_y, distance, n, total_time, skew,
            )

            result: dict = {}
            for i, (wx, wy) in enumerate(path):
                t0 = _ease_inv(i / n, skew)
                t1 = _ease_inv((i + 1) / n, skew)
                step_time = max(_MIN_STEP_SLEEP_S, (t1 - t0) * total_time)
                # Add per-step fluctuation so cruise speed isn't perfectly constant.
                step_time *= random.uniform(1.0 - _STEP_JITTER, 1.0 + _STEP_JITTER)
                result = await self._client.send_raw(
                    method="Input.dispatchMouseEvent",
                    params=_move_params(wx, wy),
                    session_id=session_id,
                )
                _emit_viz(self._client, session_id, wx, wy, "move")
                await asyncio.sleep(step_time)

            _mouse_x, _mouse_y = target_x, target_y
            return result

        # ── mousePressed → hover tremor + dwell + offset click ───────────────
        if event_type == "mousePressed":
            target_x = float(params.get("x", _mouse_x))
            target_y = float(params.get("y", _mouse_y))

            # Look up the element's bounding box *before* the hover sequence
            # so we already know the safe region during the click sequence.
            bounds = await _query_element_bounds(
                self._client, session_id, target_x, target_y
            )

            # Cursor already on the target → no hover, no offset; just dwell
            # and click at the cursor's actual position.
            if (
                bounds is not None
                and _BOUNDS_MIN_PX <= bounds[2] <= _BOUNDS_MAX_PX
                and _BOUNDS_MIN_PX <= bounds[3] <= _BOUNDS_MAX_PX
                and _point_inside_box(_mouse_x, _mouse_y, bounds)
            ):
                await asyncio.sleep(random.uniform(_DWELL_MIN_S, _DWELL_MAX_S))
                click_x = round(_mouse_x)
                click_y = round(_mouse_y)
                logger.debug(
                    "Click press (%d,%d) — cursor already on target, no hover",
                    click_x, click_y,
                )
                _emit_viz(self._client, session_id, click_x, click_y, "press")
                return await _orig(
                    self,
                    {**params, "x": click_x, "y": click_y, "pointerType": "mouse"},
                    session_id=session_id,
                )

            # Hand settling: 2–4 tiny random moves converging on the target
            for _ in range(random.randint(_HOVER_STEPS_MIN, _HOVER_STEPS_MAX)):
                hx = target_x + random.gauss(0.0, _HOVER_SIGMA)
                hy = target_y + random.gauss(0.0, _HOVER_SIGMA)
                await self._client.send_raw(
                    method="Input.dispatchMouseEvent",
                    params=_move_params(round(hx), round(hy)),
                    session_id=session_id,
                )
                _emit_viz(self._client, session_id, hx, hy, "move")
                await asyncio.sleep(random.uniform(_HOVER_STEP_MIN_S, _HOVER_STEP_MAX_S))

            # Dwell pause — reaction time before committing the click
            await asyncio.sleep(random.uniform(_DWELL_MIN_S, _DWELL_MAX_S))

            # Pick the actual click coordinates. When we have a trustworthy
            # bounding box we spread the click anywhere inside the element
            # (Gaussian centred on the requested target, clipped to an inset
            # of the box). Otherwise fall back to a small Gaussian offset.
            click_x = click_y = 0
            click_strategy = "center"
            if (
                bounds is not None
                and _BOUNDS_MIN_PX <= bounds[2] <= _BOUNDS_MAX_PX
                and _BOUNDS_MIN_PX <= bounds[3] <= _BOUNDS_MAX_PX
            ):
                bx, by, bw, bh = bounds
                margin_x = max(_CLICK_MARGIN_MIN_PX, bw * _CLICK_MARGIN_FRAC)
                margin_y = max(_CLICK_MARGIN_MIN_PX, bh * _CLICK_MARGIN_FRAC)
                sigma_x = max(_CLICK_SIGMA_MIN_PX, bw * _CLICK_SIGMA_FRAC)
                sigma_y = max(_CLICK_SIGMA_MIN_PX, bh * _CLICK_SIGMA_FRAC)
                cx = random.gauss(bx + bw / 2, sigma_x)
                cy = random.gauss(by + bh / 2, sigma_y)
                cx = max(bx + margin_x, min(bx + bw - margin_x, cx))
                cy = max(by + margin_y, min(by + bh - margin_y, cy))
                click_x = round(cx)
                click_y = round(cy)
                click_strategy = f"box({bw:.0f}x{bh:.0f})"
            else:
                click_x = round(target_x + random.gauss(0.0, _CLICK_FALLBACK_SIGMA_PX))
                click_y = round(target_y + random.gauss(0.0, _CLICK_FALLBACK_SIGMA_PX))

            _mouse_x, _mouse_y = float(click_x), float(click_y)

            logger.debug(
                "Click press (%d,%d) Δ=(%+.1f,%+.1f) from target (%.0f,%.0f) [%s]",
                click_x, click_y,
                click_x - target_x, click_y - target_y,
                target_x, target_y, click_strategy,
            )
            _emit_viz(self._client, session_id, click_x, click_y, "press")
            return await _orig(self, {**params, "x": click_x, "y": click_y, "pointerType": "mouse"}, session_id=session_id)

        # ── mouseReleased → hold delay + release at press position + lift-off ─
        if event_type == "mouseReleased":
            # Hold for either the caller-supplied override (click-and-hold) or
            # the default random 50–150 ms range (real human click duration).
            override = _click_hold_override.get()
            if override is not None:
                hold_s = float(override)
                logger.debug("Click hold %.0f ms (override) before mouseup", hold_s * 1000)
            else:
                hold_s = random.uniform(_CLICK_HOLD_MIN_S, _CLICK_HOLD_MAX_S)
                logger.debug("Click hold %.0f ms before mouseup", hold_s * 1000)
            await asyncio.sleep(hold_s)

            # Match the (possibly offset) coordinates used at press time
            result = await _orig(
                self,
                {**params, "x": round(_mouse_x), "y": round(_mouse_y), "pointerType": "mouse"},
                session_id=session_id,
            )
            _emit_viz(self._client, session_id, _mouse_x, _mouse_y, "release")

            # Natural finger rebound after releasing the button
            await asyncio.sleep(random.uniform(_LIFT_DELAY_MIN_S, _LIFT_DELAY_MAX_S))
            drift_x = round(_mouse_x + random.gauss(0.0, _LIFT_SIGMA))
            drift_y = round(_mouse_y + random.gauss(0.0, _LIFT_SIGMA))
            await self._client.send_raw(
                method="Input.dispatchMouseEvent",
                params=_move_params(drift_x, drift_y),
                session_id=session_id,
            )
            _emit_viz(self._client, session_id, drift_x, drift_y, "move")
            _mouse_x, _mouse_y = float(drift_x), float(drift_y)
            return result

        # ── everything else → pass through ───────────────────────────────────
        evt_x = params.get("x")
        evt_y = params.get("y")
        if evt_x is not None and evt_y is not None:
            _mouse_x, _mouse_y = float(evt_x), float(evt_y)
            _emit_viz(self._client, session_id, float(evt_x), float(evt_y), "move")
        return await _orig(self, params, session_id=session_id)

    InputClient.dispatchMouseEvent = _human_dispatch
    InputClient._human_mouse_patched = True
    logger.info(
        "Human mouse-interaction patches applied "
        "(hover %d–%d steps, dwell %.0f–%.0fms, hold %.0f–%.0fms, "
        "click-offset σ=%.0f%%×dim/floor %.0fpx, viz=%s)",
        _HOVER_STEPS_MIN, _HOVER_STEPS_MAX,
        _DWELL_MIN_S * 1000, _DWELL_MAX_S * 1000,
        _CLICK_HOLD_MIN_S * 1000, _CLICK_HOLD_MAX_S * 1000,
        _CLICK_SIGMA_FRAC * 100, _CLICK_SIGMA_MIN_PX,
        "on" if _VIZ_ENABLED else "off",
    )
