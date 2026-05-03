"""
Patches cdp_use's InputClient.dispatchMouseEvent so every mouse interaction
mirrors how a human physically uses a mouse:

  mouseMoved    → replaced with a Bézier curved path (slow→fast→slow)
  mousePressed  → preceded by micro-hover tremor + dwell pause; click coords
                  are slightly offset from the exact element centre
  mouseReleased → followed by a natural post-release lift-off drift
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
  Pre-click hover– 2–4 tiny random moves simulating the hand settling on target
  Dwell pause    – 80–180 ms between hover and mousedown (reaction time model)
  Click offset   – Gaussian offset from exact element centre (humans miss centre)
  Lift-off drift – small movement after mouseReleased (natural finger rebound)
  Global tracking– every path starts from the last known cursor position
"""

import asyncio
import math
import random
import logging
from typing import Optional

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
_TREMOR_SIGMA     = 0.5     # std-dev of Gaussian jitter on intermediate points
_BULGE_MIN        = 0.05    # minimum curve bulge (fraction of distance)
_BULGE_MAX        = 0.22    # maximum curve bulge

# ── Pre-click hover tuning ────────────────────────────────────────────────────
_HOVER_STEPS_MIN  = 2       # fewest micro-hover movements before mousedown
_HOVER_STEPS_MAX  = 4       # most  micro-hover movements before mousedown
_HOVER_SIGMA      = 1.5     # std-dev of each hover micro-offset (pixels)
_HOVER_STEP_MIN_S = 0.012   # min delay between hover micro-steps
_HOVER_STEP_MAX_S = 0.030   # max delay between hover micro-steps
_DWELL_MIN_S      = 0.08    # min pause between last hover move and mousedown
_DWELL_MAX_S      = 0.18    # max pause between last hover move and mousedown

# ── Click position offset ─────────────────────────────────────────────────────
_CLICK_OFFSET_SIGMA = 1.8   # std-dev of click position from exact centre (px)

# ── Post-release lift-off tuning ──────────────────────────────────────────────
_LIFT_DELAY_MIN_S = 0.018   # pause before the post-release drift move
_LIFT_DELAY_MAX_S = 0.045
_LIFT_SIGMA       = 1.2     # std-dev of lift-off drift (pixels)


# ── Math helpers ──────────────────────────────────────────────────────────────

def _ease_inv(p: float) -> float:
    """
    Inverse of ease-in-out-sine: maps normalised path position p ∈ [0,1]
    to normalised elapsed time t ∈ [0,1].

    Using equally-spaced *spatial* steps, the time budget per step is
    Δt = ease_inv((i+1)/n) − ease_inv(i/n), which gives:
      • large Δt at start and end  → cursor moves slowly (acceleration phase)
      • small Δt in the middle     → cursor moves quickly (cruise phase)
    matching real measured human mouse trajectories.
    """
    p = max(0.0, min(1.0, p))
    return math.acos(max(-1.0, min(1.0, 1.0 - 2.0 * p))) / math.pi


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
    if distance < 3.0:
        return [(round(ex), round(ey))]

    n = max(10, min(50, int(distance / 12)))
    px, py = (-dy / distance, dx / distance)   # unit perpendicular
    bulge = distance * random.uniform(_BULGE_MIN, _BULGE_MAX)
    side = random.choice((-1.0, 1.0))

    cp1 = (
        sx + dx * random.uniform(0.20, 0.40) + px * side * bulge * random.uniform(0.5, 1.0),
        sy + dy * random.uniform(0.20, 0.40) + py * side * bulge * random.uniform(0.5, 1.0),
    )
    cp2 = (
        sx + dx * random.uniform(0.60, 0.80) + px * side * bulge * random.uniform(0.5, 1.0),
        sy + dy * random.uniform(0.60, 0.80) + py * side * bulge * random.uniform(0.5, 1.0),
    )

    points: list[tuple[int, int]] = []
    for i in range(1, n + 1):
        t = i / n
        x, y = _bezier_cubic((sx, sy), cp1, cp2, (ex, ey), t)
        if i < n:                           # jitter on intermediate points only
            x += random.gauss(0.0, _TREMOR_SIGMA)
            y += random.gauss(0.0, _TREMOR_SIGMA)
        points.append((round(x), round(y)))

    return points


# ── Patch entry point ─────────────────────────────────────────────────────────

def patch_mouse_movement() -> None:
    """
    Replace InputClient.dispatchMouseEvent with a full human-interaction
    simulation covering movement, hover, click offset, and lift-off.
    """
    try:
        from cdp_use.cdp.input.library import InputClient
    except ImportError:
        logger.warning("cdp_use InputClient not found — human mouse patches skipped")
        return

    if getattr(InputClient, "_human_mouse_patched", False):
        return

    _orig = InputClient.dispatchMouseEvent

    async def _human_dispatch(
        self, params: dict, session_id: Optional[str] = None
    ) -> dict:
        global _mouse_x, _mouse_y

        event_type = params.get("type", "")

        # ── mouseMoved → Bézier curved path ──────────────────────────────────
        if event_type == "mouseMoved":
            target_x = float(params.get("x", _mouse_x))
            target_y = float(params.get("y", _mouse_y))
            distance = math.hypot(target_x - _mouse_x, target_y - _mouse_y)

            if distance < 3.0:
                _mouse_x, _mouse_y = target_x, target_y
                return await _orig(self, params, session_id=session_id)

            total_time = _TIME_BASE_S + distance * _TIME_PER_PX
            total_time = min(
                _TIME_MAX_S,
                total_time * random.uniform(1.0 - _TIME_JITTER, 1.0 + _TIME_JITTER),
            )
            path = _build_path(_mouse_x, _mouse_y, target_x, target_y)
            n = len(path)

            logger.debug(
                "Mouse path (%.0f,%.0f)→(%.0f,%.0f) dist=%.0f steps=%d time=%.2fs",
                _mouse_x, _mouse_y, target_x, target_y, distance, n, total_time,
            )

            result: dict = {}
            for i, (wx, wy) in enumerate(path):
                t0 = _ease_inv(i / n)
                t1 = _ease_inv((i + 1) / n)
                step_time = max(_MIN_STEP_SLEEP_S, (t1 - t0) * total_time)
                result = await self._client.send_raw(
                    method="Input.dispatchMouseEvent",
                    params={"type": "mouseMoved", "x": wx, "y": wy},
                    session_id=session_id,
                )
                await asyncio.sleep(step_time)

            _mouse_x, _mouse_y = target_x, target_y
            return result

        # ── mousePressed → hover tremor + dwell + offset click ───────────────
        if event_type == "mousePressed":
            target_x = float(params.get("x", _mouse_x))
            target_y = float(params.get("y", _mouse_y))

            # Hand settling: 2–4 tiny random moves converging on the target
            for _ in range(random.randint(_HOVER_STEPS_MIN, _HOVER_STEPS_MAX)):
                hx = target_x + random.gauss(0.0, _HOVER_SIGMA)
                hy = target_y + random.gauss(0.0, _HOVER_SIGMA)
                await self._client.send_raw(
                    method="Input.dispatchMouseEvent",
                    params={"type": "mouseMoved", "x": round(hx), "y": round(hy)},
                    session_id=session_id,
                )
                await asyncio.sleep(random.uniform(_HOVER_STEP_MIN_S, _HOVER_STEP_MAX_S))

            # Dwell pause — reaction time before committing the click
            await asyncio.sleep(random.uniform(_DWELL_MIN_S, _DWELL_MAX_S))

            # Real humans don't hit the exact pixel centre of an element
            click_x = round(target_x + random.gauss(0.0, _CLICK_OFFSET_SIGMA))
            click_y = round(target_y + random.gauss(0.0, _CLICK_OFFSET_SIGMA))
            _mouse_x, _mouse_y = float(click_x), float(click_y)

            logger.debug(
                "Click press (%d,%d) offset from centre (%.0f,%.0f)",
                click_x, click_y, target_x, target_y,
            )
            return await _orig(self, {**params, "x": click_x, "y": click_y}, session_id=session_id)

        # ── mouseReleased → release at press position + lift-off drift ────────
        if event_type == "mouseReleased":
            # Match the (possibly offset) coordinates used at press time
            result = await _orig(
                self,
                {**params, "x": round(_mouse_x), "y": round(_mouse_y)},
                session_id=session_id,
            )

            # Natural finger rebound after releasing the button
            await asyncio.sleep(random.uniform(_LIFT_DELAY_MIN_S, _LIFT_DELAY_MAX_S))
            drift_x = round(_mouse_x + random.gauss(0.0, _LIFT_SIGMA))
            drift_y = round(_mouse_y + random.gauss(0.0, _LIFT_SIGMA))
            await self._client.send_raw(
                method="Input.dispatchMouseEvent",
                params={"type": "mouseMoved", "x": drift_x, "y": drift_y},
                session_id=session_id,
            )
            _mouse_x, _mouse_y = float(drift_x), float(drift_y)
            return result

        # ── everything else (mouseWheel, etc.) → pass through ────────────────
        evt_x = params.get("x")
        evt_y = params.get("y")
        if evt_x is not None and evt_y is not None:
            _mouse_x, _mouse_y = float(evt_x), float(evt_y)
        return await _orig(self, params, session_id=session_id)

    InputClient.dispatchMouseEvent = _human_dispatch
    InputClient._human_mouse_patched = True
    logger.info(
        "Human mouse-interaction patches applied "
        "(hover %d–%d steps, dwell %.0f–%.0fms, click-offset σ=%.1fpx)",
        _HOVER_STEPS_MIN, _HOVER_STEPS_MAX,
        _DWELL_MIN_S * 1000, _DWELL_MAX_S * 1000,
        _CLICK_OFFSET_SIGMA,
    )
