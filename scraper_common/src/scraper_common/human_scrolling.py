"""
Patches DefaultActionWatchdog.on_ScrollEvent so every LLM-initiated page
scroll is decomposed into human-sized wheel-notch events with a bell-shaped
(half-sine) velocity profile, each notch further sub-divided into rapid
micro-events that model the OS wheel-rotation burst for a single detent.

Why on_ScrollEvent and not mouseWheel CDP events
------------------------------------------------
browser-use never emits a mouseWheel CDP event when the LLM issues a scroll
command — the agent calls JavaScript (window.scrollBy / synthesizeScrollGesture)
directly through its CDP layer, bypassing InputClient.dispatchMouseEvent entirely.
Intercepting mouseWheel would therefore never fire for LLM-driven scrolls.

Algorithm
---------
1. Pass-through for tiny scrolls (|amount| < 30 px).
2. Choose step count n ∈ [3, 6] uniformly at random.
3. Compute bell-shaped base weights: w_i = sin(π × (i + 0.5) / n).
4. Normalise weights so raw steps sum exactly to |amount|.
5. Apply per-step independent jitter (10–20 %, random sign).
6. Re-normalise to restore exact total — preserves bell shape.
7. Emit each notch as 3–7 micro-events (8–20 ms apart), modelling the rapid
   OS wheel-rotation burst for a single physical detent. Inter-notch pauses
   (250–600 ms) separate distinct detent engagements. A pre-scroll dwell
   (300–700 ms) precedes the first notch.
8. Horizontal scroll (left/right) uses the same algorithm on deltaX.
   Diagonal scrolls decompose each axis independently; shorter step
   list pads with zero so both axes complete in the same pass.

Idempotent: safe to call multiple times.
"""

import asyncio
import logging
import math
import random

logger = logging.getLogger("scraper_common.human_scrolling")

# ── Scroll simulation tuning ──────────────────────────────────────────────────
_SCROLL_SMALL_THRESHOLD   = 30      # px: |delta| below this is passed through unchanged
_SCROLL_STEPS_MIN         = 3       # minimum wheel-notch events per scroll
_SCROLL_STEPS_MAX         = 6       # maximum wheel-notch events per scroll
_SCROLL_JITTER_MIN        = 0.10    # minimum per-step jitter magnitude (10 %)
_SCROLL_JITTER_MAX        = 0.20    # maximum per-step jitter magnitude (20 %)
_SCROLL_INTER_STEP_MIN_S  = 0.25    # minimum pause between notches
_SCROLL_INTER_STEP_MAX_S  = 0.60    # maximum pause between notches
_SCROLL_DWELL_MIN_S       = 0.30    # pre-scroll finger-placement pause (min)
_SCROLL_DWELL_MAX_S       = 0.70    # pre-scroll finger-placement pause (max)
_SCROLL_MICRO_STEPS_MIN   = 3       # minimum micro-events per notch
_SCROLL_MICRO_STEPS_MAX   = 7       # maximum micro-events per notch
_SCROLL_MICRO_INTER_MIN_S = 0.008   # minimum pause between micro-events within a notch
_SCROLL_MICRO_INTER_MAX_S = 0.020   # maximum pause between micro-events within a notch

_human_scrolling_patched: bool = False


def _build_scroll_steps(delta: float) -> list[float]:
    """
    Split a single-axis scroll delta into n human-sized sub-steps using a
    bell-shaped (half-sine) weight distribution with per-step jitter,
    re-normalised so the sum equals delta exactly.

    Returns [] if delta is zero, [delta] unchanged if |delta| < threshold,
    or a list of n signed step values whose sum equals delta.
    """
    if delta == 0.0:
        return []
    abs_delta = abs(delta)
    sign = 1.0 if delta > 0 else -1.0
    if abs_delta < _SCROLL_SMALL_THRESHOLD:
        return [delta]
    n = random.randint(_SCROLL_STEPS_MIN, _SCROLL_STEPS_MAX)

    # Bell-shaped base weights: symmetric half-sine curve, endpoints smallest,
    # centre peak(s) largest — mirrors the acceleration/deceleration profile of
    # a real human scroll gesture.
    weights = [math.sin(math.pi * (i + 0.5) / n) for i in range(n)]
    w_total = sum(weights)
    raw = [abs_delta * w / w_total for w in weights]

    # Independent per-step jitter: random magnitude (10–20 %) with random sign.
    jittered = [
        r * (1.0 + random.choice((-1.0, 1.0)) * random.uniform(_SCROLL_JITTER_MIN, _SCROLL_JITTER_MAX))
        for r in raw
    ]

    # Re-normalise to preserve exact total; bell shape is unaffected because
    # re-normalisation is a uniform scalar across all steps.
    j_total = sum(jittered)
    return [sign * s * abs_delta / j_total for s in jittered]


async def _human_scroll_impl(browser_session, direction: str, amount: int) -> None:
    """Drive a human-realistic page scroll via CDP mouseWheel events."""
    logger.debug("Human scroll simulation: direction=%s amount=%d", direction, amount)
    if direction == "down":
        delta_x, delta_y = 0.0, float(amount)
    elif direction == "up":
        delta_x, delta_y = 0.0, -float(amount)
    elif direction == "right":
        delta_x, delta_y = float(amount), 0.0
    else:  # left
        delta_x, delta_y = -float(amount), 0.0

    steps_x = _build_scroll_steps(delta_x)
    steps_y = _build_scroll_steps(delta_y)

    # Obtain the CDP session once and reuse for all notch events.
    cdp_session = await browser_session.get_or_create_cdp_session()
    cdp_client = cdp_session.cdp_client
    session_id = cdp_session.session_id

    # Use cached viewport size when available to avoid an extra round-trip.
    if browser_session._original_viewport_size:
        viewport_width, viewport_height = browser_session._original_viewport_size
    else:
        layout_metrics = await cdp_client.send.Page.getLayoutMetrics(session_id=session_id)
        viewport_width = layout_metrics["layoutViewport"]["clientWidth"]
        viewport_height = layout_metrics["layoutViewport"]["clientHeight"]

    center_x = viewport_width / 2
    center_y = viewport_height / 2

    async def _emit(dx: float, dy: float) -> None:
        await cdp_client.send_raw(
            method="Input.dispatchMouseEvent",
            params={
                "type": "mouseWheel",
                "x": center_x,
                "y": center_y,
                "deltaX": dx,
                "deltaY": dy,
                "pointerType": "mouse",
            },
            session_id=session_id,
        )

    # Single tiny event — pass through as-is without dwell
    if len(steps_x) <= 1 and len(steps_y) <= 1:
        sx = steps_x[0] if steps_x else 0.0
        sy = steps_y[0] if steps_y else 0.0
        logger.debug("scroll pass-through direction=%s amount=%d", direction, amount)
        await _emit(sx, sy)
        return

    n_steps = max(len(steps_x), len(steps_y))
    logger.debug(
        "scroll direction=%s amount=%d → %d bell-notch(es)", direction, amount, n_steps
    )

    # Pre-scroll dwell — models the moment the user positions fingers on the wheel
    await asyncio.sleep(random.uniform(_SCROLL_DWELL_MIN_S, _SCROLL_DWELL_MAX_S))

    for i in range(n_steps):
        sx = steps_x[i] if i < len(steps_x) else 0.0
        sy = steps_y[i] if i < len(steps_y) else 0.0

        # Tier 2: sub-divide each notch into micro-events that model the rapid
        # OS wheel-rotation burst a single physical detent produces (8–20 ms apart).
        m = random.randint(_SCROLL_MICRO_STEPS_MIN, _SCROLL_MICRO_STEPS_MAX)
        micro_dx = sx / m
        micro_dy = sy / m
        for j in range(m):
            await _emit(micro_dx, micro_dy)
            if j < m - 1:
                await asyncio.sleep(
                    random.uniform(_SCROLL_MICRO_INTER_MIN_S, _SCROLL_MICRO_INTER_MAX_S)
                )

        if i < n_steps - 1:
            await asyncio.sleep(
                random.uniform(_SCROLL_INTER_STEP_MIN_S, _SCROLL_INTER_STEP_MAX_S)
            )


def patch_scroll_page() -> None:
    """
    Replace DefaultActionWatchdog.on_ScrollEvent with a human-realistic scroll
    simulation for LLM-initiated page scrolls. Element-level scrolls are
    delegated to the original handler unchanged.

    Idempotent: safe to call multiple times.
    """
    global _human_scrolling_patched

    try:
        from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog
    except ImportError:
        logger.warning("DefaultActionWatchdog not found — human scroll patch skipped")
        return

    if _human_scrolling_patched:
        logger.debug("Human scroll patch already applied")
        return

    _orig = DefaultActionWatchdog.on_ScrollEvent

    async def on_ScrollEvent(self, event) -> None:
        if event.node is not None:
            # Element-specific scroll — use the original handler unchanged
            return await _orig(self, event)
        await _human_scroll_impl(self.browser_session, event.direction, event.amount)

    DefaultActionWatchdog.on_ScrollEvent = on_ScrollEvent
    _human_scrolling_patched = True
    logger.info(
        "Human scroll patch applied (%d–%d bell-notches, jitter=%.0f–%.0f%%, "
        "inter-step=%.0f–%.0fms, dwell=%.0f–%.0fms, "
        "micro-events=%d–%d per notch at %.0f–%.0fms apart)",
        _SCROLL_STEPS_MIN, _SCROLL_STEPS_MAX,
        _SCROLL_JITTER_MIN * 100, _SCROLL_JITTER_MAX * 100,
        _SCROLL_INTER_STEP_MIN_S * 1000, _SCROLL_INTER_STEP_MAX_S * 1000,
        _SCROLL_DWELL_MIN_S * 1000, _SCROLL_DWELL_MAX_S * 1000,
        _SCROLL_MICRO_STEPS_MIN, _SCROLL_MICRO_STEPS_MAX,
        _SCROLL_MICRO_INTER_MIN_S * 1000, _SCROLL_MICRO_INTER_MAX_S * 1000,
    )
