"""
Patches cdp_use's InputClient.dispatchMouseEvent so every mouse interaction
mirrors how a human physically uses a mouse:

  mouseMoved    → replaced with a recorded human path from HumanInteractions
  mousePressed  → preceded by micro-hover tremor + dwell pause; click coords
                  are slightly offset from the exact element centre
  mouseReleased → 50–150 ms hold, then release; followed by lift-off drift
  everything else passes through unchanged

How it works
------------
browser_use dispatches element clicks as:
  1. mouseMoved   → teleports cursor to target  (replaced with recorded path)
  2. mousePressed at target                     (prefixed with hover + dwell)
  3. mouseReleased at target                    (suffixed with lift-off drift)

We intercept at InputClient.dispatchMouseEvent — the lowest possible level in
the cdp_use stack — so the patch covers every click path in browser_use with
no code duplication.  Idempotent: safe to call multiple times.

Interaction model
-----------------
  Recorded path  – nearest matching sample from recorded human sessions,
                   transformed (rotate + scale) to fit the requested move
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

from scraper_common import data_store
from scraper_common.human_interactions import interactions as _interactions

logger = logging.getLogger("scraper_common.human_mouse")

# ── Global cursor position ────────────────────────────────────────────────────
_mouse_x: float = 100.0
_mouse_y: float = 100.0

# ── Pre-click hover tuning ────────────────────────────────────────────────────
_HOVER_STEPS_MIN  = 2       # fewest micro-hover movements before mousedown
_HOVER_STEPS_MAX  = 4       # most  micro-hover movements before mousedown
_HOVER_SIGMA      = 1.5     # std-dev of each hover micro-offset (pixels)
_HOVER_STEP_MIN_S = 0.012   # min delay between hover micro-steps
_HOVER_STEP_MAX_S = 0.030   # max delay between hover micro-steps
_DWELL_MIN_S      = 0.35    # min pause between cursor stop and mousedown
_DWELL_MAX_S      = 0.65    # max pause between cursor stop and mousedown

# ── Click position offset ─────────────────────────────────────────────────────
_CLICK_MARGIN_FRAC       = 0.05  # inset margin as fraction of element dimension
_CLICK_MARGIN_MIN_PX     = 2.0   # absolute inset floor
_CLICK_FALLBACK_SIGMA_PX = 4.0   # fallback Gaussian offset when no bounding box

# ── Button hold (mousedown → mouseup) ─────────────────────────────────────────
_CLICK_HOLD_MIN_S = 0.050   # min time the button stays pressed (default range)
_CLICK_HOLD_MAX_S = 0.150   # max time the button stays pressed (default range)

# ── Click-hold movement constraint ────────────────────────────────────────────
_CLICK_MAX_MOVE_PX = 10.0   # max total mousemove path (px) allowed between mousedown and mouseup

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


# Per-task click pattern for the current mousedown→mouseup hold phase.
# None  → use simple hold (without_mouse_move path).
# list  → replay recorded mousemove steps relative to the press position.
_click_pattern: ContextVar[Optional[list]] = ContextVar(
    "_human_mouse_click_pattern", default=None
)

# ── Recorded click data ───────────────────────────────────────────────────────

def _load_click_data() -> tuple[dict, list]:
    return data_store.get("click_index"), data_store.get("processed")


def _extract_click_pattern(sample: dict) -> list[dict]:
    """
    Extract relative mousemove + mouseup events from a recorded compound click.
    Coordinates are expressed as offsets from the mousedown position; timing is
    in milliseconds from mousedown.
    """
    events = sample.get("events", [])
    mousedown = next((e for e in events if e["type"] == "mousedown"), None)
    if not mousedown:
        return []
    ref_t = mousedown["timestamp"]
    ref_x = mousedown["x"]
    ref_y = mousedown["y"]
    pattern: list[dict] = []
    for ev in events:
        if ev["type"] == "mousemove":
            pattern.append({
                "type": "mousemove",
                "rel_x": ev["x"] - ref_x,
                "rel_y": ev["y"] - ref_y,
                "delay_ms": ev["timestamp"] - ref_t,
            })
        elif ev["type"] == "mouseup":
            pattern.append({
                "type": "mouseup",
                "rel_x": ev["x"] - ref_x,
                "rel_y": ev["y"] - ref_y,
                "delay_ms": ev["timestamp"] - ref_t,
            })
            break
    return pattern


def _truncate_click_pattern(pattern: list[dict]) -> list[dict]:
    """
    Enforce the _CLICK_MAX_MOVE_PX constraint on a click pattern.

    Walks the mousemove steps (coordinates are relative to mousedown) and drops
    any step that would push the cumulative path length past the limit.  The
    terminal mouseup step is always re-appended so the pattern remains valid.
    """
    moves = [e for e in pattern if e["type"] == "mousemove"]
    mouseup = next((e for e in pattern if e["type"] == "mouseup"), None)

    total = 0.0
    kept: list[dict] = []
    prev_x, prev_y = 0.0, 0.0
    for step in moves:
        dist = math.hypot(step["rel_x"] - prev_x, step["rel_y"] - prev_y)
        if total + dist > _CLICK_MAX_MOVE_PX:
            break
        total += dist
        kept.append(step)
        prev_x, prev_y = step["rel_x"], step["rel_y"]

    if mouseup:
        kept.append(mouseup)

    if len(moves) != len(kept) - (1 if mouseup else 0):
        logger.debug(
            "Click pattern truncated: %d → %d mousemove steps (total %.1fpx > %.0fpx limit)",
            len(moves), len(kept) - (1 if mouseup else 0), total, _CLICK_MAX_MOVE_PX,
        )
    return kept


def _pick_click_pattern() -> Optional[list]:
    """
    Randomly choose a click style:
      50 % — with mouse move: pick a recorded sample and return its pattern.
      50 % — without mouse move: return None (simple hold, no movement).
    """
    if random.random() < 0.5:
        try:
            click_index, processed = _load_click_data()
            sample_idx = random.choice(click_index["with_mouse_move"])
            sample = processed[sample_idx]
            pattern = _extract_click_pattern(sample)
            if pattern:
                return _truncate_click_pattern(pattern)
        except Exception:
            logger.debug("Failed to load recorded click pattern; using simple hold")
    return None


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

        # ── mouseMoved → recorded human path ─────────────────────────────────
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
                    return await self._client.send_raw(
                        method="Input.dispatchMouseEvent",
                        params=_move_params(round(_mouse_x), round(_mouse_y)),
                        session_id=session_id,
                    )

            path = _interactions.generate_mousemove(_mouse_x, _mouse_y, target_x, target_y)

            logger.debug(
                "Mouse path (%.0f,%.0f)→(%.0f,%.0f) dist=%.0f steps=%d",
                _mouse_x, _mouse_y, target_x, target_y, distance, len(path),
            )

            result: dict = {}
            for step in path:
                result = await self._client.send_raw(
                    method="Input.dispatchMouseEvent",
                    params=_move_params(step.x, step.y),
                    session_id=session_id,
                )
                _emit_viz(self._client, session_id, step.x, step.y, "move")
                if step.delay_ms > 0:
                    await asyncio.sleep(step.delay_ms / 1000.0)

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

            # Choose with/without-mouse-move style for the upcoming hold phase.
            _click_pattern.set(_pick_click_pattern())

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
            # bounding box we sample uniformly inside the inset box so every
            # interior point is equally likely (no centre-biased density).
            # Otherwise fall back to a small Gaussian offset from the target.
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
                click_x = round(random.uniform(bx + margin_x, bx + bw - margin_x))
                click_y = round(random.uniform(by + margin_y, by + bh - margin_y))
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

        # ── mouseReleased → recorded hold pattern or simple hold + lift-off ──
        if event_type == "mouseReleased":
            override = _click_hold_override.get()
            pattern = _click_pattern.get()
            _click_pattern.set(None)

            if pattern and override is None:
                # Replay recorded mouse movements during the button hold.
                # Coordinates in the pattern are relative to the mousedown position.
                base_x = round(_mouse_x)
                base_y = round(_mouse_y)
                last_delay_ms = 0
                final_x, final_y = base_x, base_y

                for step in pattern:
                    wait_s = (step["delay_ms"] - last_delay_ms) / 1000.0
                    if wait_s > 0:
                        await asyncio.sleep(wait_s)
                    last_delay_ms = step["delay_ms"]

                    abs_x = round(base_x + step["rel_x"])
                    abs_y = round(base_y + step["rel_y"])
                    final_x, final_y = abs_x, abs_y

                    if step["type"] == "mousemove":
                        await self._client.send_raw(
                            method="Input.dispatchMouseEvent",
                            params=_move_params(abs_x, abs_y, buttons=1),
                            session_id=session_id,
                        )
                        _emit_viz(self._client, session_id, abs_x, abs_y, "move")
                    # mouseup step: just captures the final position/timing

                result = await _orig(
                    self,
                    {**params, "x": final_x, "y": final_y, "pointerType": "mouse"},
                    session_id=session_id,
                )
                _emit_viz(self._client, session_id, final_x, final_y, "release")
                _mouse_x, _mouse_y = float(final_x), float(final_y)
            else:
                # Simple hold: wait then release at the press position.
                if override is not None:
                    hold_s = float(override)
                    logger.debug("Click hold %.0f ms (override) before mouseup", hold_s * 1000)
                else:
                    hold_s = random.uniform(_CLICK_HOLD_MIN_S, _CLICK_HOLD_MAX_S)
                    logger.debug("Click hold %.0f ms before mouseup", hold_s * 1000)
                await asyncio.sleep(hold_s)

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
        "click-offset=uniform-inset margin=%.0f%%/floor %.0fpx, viz=%s)",
        _HOVER_STEPS_MIN, _HOVER_STEPS_MAX,
        _DWELL_MIN_S * 1000, _DWELL_MAX_S * 1000,
        _CLICK_HOLD_MIN_S * 1000, _CLICK_HOLD_MAX_S * 1000,
        _CLICK_MARGIN_FRAC * 100, _CLICK_MARGIN_MIN_PX,
        "on" if _VIZ_ENABLED else "off",
    )
