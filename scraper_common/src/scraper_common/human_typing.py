"""
Patches browser_use's DefaultActionWatchdog to use human-like inter-keystroke
timing instead of the default ≤10 ms robotic delays, and to prefix each typing
action with a human-style pre-typing focus sequence (mouse move → click →
post-focus dwell).

Call patch_watchdog_typing() once at import time (scraper_base.py does this).
The patches are idempotent and safe to call multiple times.
"""
import asyncio
import logging
import random

logger = logging.getLogger("scraper_common.human_typing")

# Keystroke timing parameters
_MIN_DELAY = 0.05    # 50 ms  — fast but plausible typist
_MAX_DELAY = 0.15    # 150 ms — normal typing speed
_HESITATION_PROB = 0.08        # 8 % chance of a mid-word pause
_HESITATION_EXTRA = (0.15, 0.40)  # extra 150–400 ms on hesitation

# Pre-typing focus sequence timing
_PRE_TYPE_POST_CLICK_MIN_S = 2.75  # min pause between click release and first keystroke
_PRE_TYPE_POST_CLICK_MAX_S = 3.55  # max pause between click release and first keystroke

# Element size trust range — mirrors human_mouse._BOUNDS_MIN/MAX_PX
_BOUNDS_MIN_PX = 8.0
_BOUNDS_MAX_PX = 400.0


def _human_delay() -> float:
    """Return a randomised human-like inter-keystroke delay in seconds."""
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    if random.random() < _HESITATION_PROB:
        delay += random.uniform(*_HESITATION_EXTRA)
    return delay


def _get_sample_delays(text_len: int) -> list[float]:
    """
    Load a recorded input sample whose length is closest to *text_len* and
    return the inter-event intervals (in seconds) as a list.

    If the sample has fewer intervals than the text is long the list is meant
    to be consumed cyclically by the caller.  Returns [] on any error so the
    caller can fall back to _human_delay().
    """
    try:
        from scraper_common import data_store
        input_index: dict = data_store.get("input_index")
        processed: list = data_store.get("processed")

        int_keys = sorted(int(k) for k in input_index)
        closest = min(int_keys, key=lambda k: abs(k - text_len))
        sample_idx = random.choice(input_index[str(closest)])
        events = processed[sample_idx].get("events", [])

        if len(events) < 2:
            return []

        delays = []
        for i in range(len(events) - 1):
            dt_s = (events[i + 1]["timestamp"] - events[i]["timestamp"]) / 1000.0
            delays.append(max(dt_s, 0.001))

        logger.debug(
            "Loaded %d inter-keystroke delays from sample index %d (key=%d, text_len=%d)",
            len(delays), sample_idx, closest, text_len,
        )
        return delays
    except Exception as exc:
        logger.debug("Failed to load sample delays, falling back to _human_delay(): %s", exc)
        return []


async def _pre_type_focus_sequence(cdp_session, cx: float, cy: float) -> None:
    """
    Execute the human-style pre-typing focus sequence at element centre (cx, cy).

    The three dispatchMouseEvent calls go through the already-patched
    InputClient.dispatchMouseEvent, so the full simulation stack fires:
      mouseMoved    → Bézier curved path, ease-in-out-sine, tremor
      mousePressed  → hover micro-moves (skipped if cursor already on target)
                      + dwell pause + Gaussian click offset
      mouseReleased → hold delay + release + lift-off drift
    Post-click dwell lets JS focus/focusin handlers settle before keystrokes.
    """
    input_client = cdp_session.cdp_client.send.Input
    session_id = cdp_session.session_id

    await input_client.dispatchMouseEvent(
        params={
            "type": "mouseMoved",
            "x": round(cx),
            "y": round(cy),
            "button": "none",
            "buttons": 0,
            "modifiers": 0,
            "pointerType": "mouse",
        },
        session_id=session_id,
    )
    await input_client.dispatchMouseEvent(
        params={
            "type": "mousePressed",
            "x": round(cx),
            "y": round(cy),
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
            "modifiers": 0,
            "pointerType": "mouse",
        },
        session_id=session_id,
    )
    await input_client.dispatchMouseEvent(
        params={
            "type": "mouseReleased",
            "x": round(cx),
            "y": round(cy),
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
            "modifiers": 0,
            "pointerType": "mouse",
        },
        session_id=session_id,
    )
    await asyncio.sleep(random.uniform(_PRE_TYPE_POST_CLICK_MIN_S, _PRE_TYPE_POST_CLICK_MAX_S))


def patch_watchdog_typing() -> None:
    """
    Replace the two typing implementations in DefaultActionWatchdog with
    versions that prefix each typing action with a pre-typing focus sequence
    and use _human_delay() between every keystroke.

    Strategy
    --------
    * _type_to_page      – simple method; fully rewritten here.
      Pre-typing coords come from document.activeElement.getBoundingClientRect().
    * _input_text_element_node_impl – complex (200+ lines); two-layer wrapping:
      Outer: pre-typing focus sequence using get_element_coordinates() on the
             element_node, then delegates to the original.
      Inner: asyncio proxy installed in the watchdog module's namespace intercepts
             per-keystroke asyncio.sleep calls and replaces them with _human_delay().
             Sub-20 ms sleeps (inter-keystroke: 1 ms, 5 ms, 10 ms) are replaced;
             longer sleeps (scroll waits, etc.) pass through unchanged.
    """
    try:
        import browser_use.browser.watchdogs.default_action_watchdog as _wdog_mod
        from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog
    except ImportError:
        logger.warning("browser_use watchdog not found – human typing patches skipped")
        return

    if getattr(DefaultActionWatchdog, "_human_typing_patched", False):
        return

    # ── Build a proxy for asyncio that lives in the watchdog module's globals ─
    # When watchdog code does `asyncio.sleep(...)` it looks up `asyncio` in its
    # own module globals, so swapping that binding to our proxy lets us
    # intercept only that module's sleeps without touching the real asyncio.
    _real_asyncio = _wdog_mod.asyncio  # the genuine asyncio module

    class _AsyncioProxy:
        """Module-level proxy; all attrs delegate to the real asyncio."""
        def __getattr__(self, name: str):
            return getattr(_real_asyncio, name)

    _proxy = _AsyncioProxy()
    _proxy.sleep = _real_asyncio.sleep  # default: real sleep
    _wdog_mod.asyncio = _proxy          # watchdog module now sees the proxy

    # ── Patch 1: _type_to_page (fallback typing path) ─────────────────────────
    # Full rewrite. Prepends pre-typing focus sequence using the currently
    # focused element's viewport coordinates, then types character by character.

    async def _human_type_to_page(self, text: str) -> None:
        try:
            cdp_session = await self.browser_session.get_or_create_cdp_session(
                target_id=None, focus=True
            )

            # Pre-typing focus sequence: query the currently focused element,
            # move the mouse to it, and click to simulate human focus intent.
            # Skipped if no focused element is found or bounds are outside the
            # trust range — typing proceeds normally in that case.
            try:
                res = await cdp_session.cdp_client.send_raw(
                    method="Runtime.evaluate",
                    params={
                        "expression": (
                            "(function(){"
                            "var e=document.activeElement;"
                            "if(!e||e===document.body||e===document.documentElement)return null;"
                            "var r=e.getBoundingClientRect();"
                            "return [r.left,r.top,r.width,r.height];"
                            "})()"
                        ),
                        "returnByValue": True,
                        "awaitPromise": False,
                        "silent": True,
                    },
                    session_id=cdp_session.session_id,
                )
                val = (res or {}).get("result", {}).get("value")
                if val and len(val) == 4:
                    bx, by, bw, bh = map(float, val)
                    if _BOUNDS_MIN_PX <= bw <= _BOUNDS_MAX_PX and _BOUNDS_MIN_PX <= bh <= _BOUNDS_MAX_PX:
                        await _pre_type_focus_sequence(cdp_session, bx + bw / 2, by + bh / 2)
                        logger.debug(
                            "Pre-type focus completed for active element (%.0fx%.0f) at (%.0f,%.0f)",
                            bw, bh, bx + bw / 2, by + bh / 2,
                        )
                    else:
                        logger.debug(
                            "Pre-type focus skipped — active element outside trust range (%.0fx%.0f)", bw, bh
                        )
                else:
                    logger.debug("Pre-type focus skipped — no valid active element")
            except Exception as exc:
                logger.debug("Pre-type focus sequence failed, proceeding without it: %s", exc)

            sample_delays = _get_sample_delays(len(text))

            for char_idx, char in enumerate(text):
                if char == "\n":
                    for params in [
                        {"type": "keyDown", "key": "Enter", "code": "Enter",
                         "windowsVirtualKeyCode": 13},
                        {"type": "char", "text": "\r", "key": "Enter"},
                        {"type": "keyUp", "key": "Enter", "code": "Enter",
                         "windowsVirtualKeyCode": 13},
                    ]:
                        await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                            params=params, session_id=cdp_session.session_id
                        )
                else:
                    for params in [
                        {"type": "keyDown", "key": char},
                        {"type": "char", "text": char},
                        {"type": "keyUp", "key": char},
                    ]:
                        await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                            params=params, session_id=cdp_session.session_id
                        )
                if sample_delays:
                    delay = sample_delays[char_idx % len(sample_delays)]
                else:
                    delay = _human_delay()
                await _real_asyncio.sleep(delay)
        except Exception as exc:
            raise Exception(f"Failed to type to page: {exc}") from exc

    DefaultActionWatchdog._type_to_page = _human_type_to_page

    # ── Patch 2: _input_text_element_node_impl (primary typing path) ──────────
    # Wrap the original with two layers:
    #   Outer: pre-typing focus sequence using get_element_coordinates().
    #   Inner: asyncio proxy (already in watchdog globals) intercepts per-keystroke
    #          sleeps. The proxy is installed first (module-level); the outer
    #          wrapper is applied second (method-level). They compose cleanly.

    _orig_impl = DefaultActionWatchdog._input_text_element_node_impl

    async def _human_input_text_impl(
        self, element_node, text, clear=True, is_sensitive=False
    ):
        # Outer wrapper: pre-typing focus sequence.
        # Uses the same get_element_coordinates() that the original method
        # calls internally, so no extra CDP round-trips are introduced.
        try:
            cdp_session = await self.browser_session.cdp_client_for_node(element_node)
            coords = await self.browser_session.get_element_coordinates(
                element_node.backend_node_id, cdp_session
            )
            if coords is not None:
                if _BOUNDS_MIN_PX <= coords.width <= _BOUNDS_MAX_PX and _BOUNDS_MIN_PX <= coords.height <= _BOUNDS_MAX_PX:
                    cx = coords.x + coords.width / 2
                    cy = coords.y + coords.height / 2
                    await _pre_type_focus_sequence(cdp_session, cx, cy)
                    logger.debug(
                        "Pre-type focus completed for element (%.0fx%.0f) at (%.0f,%.0f)",
                        coords.width, coords.height, cx, cy,
                    )
                else:
                    logger.debug(
                        "Pre-type focus skipped — element dimensions outside trust range (%.0fx%.0f)",
                        coords.width, coords.height,
                    )
            else:
                logger.debug("Pre-type focus skipped — could not resolve element coordinates")
        except Exception as exc:
            logger.debug("Pre-type focus sequence failed, proceeding without it: %s", exc)

        # Inner: asyncio proxy for per-keystroke timing (already installed above).
        _saved_sleep = _proxy.sleep

        async def _fast_to_human(delay: float) -> None:
            if delay < 0.02:           # inter-keystroke sleep (1 ms, 5 ms, 10 ms)
                await _real_asyncio.sleep(_human_delay())
            else:
                await _real_asyncio.sleep(delay)   # scroll waits etc. pass through

        _proxy.sleep = _fast_to_human
        try:
            return await _orig_impl(
                self, element_node, text, clear=clear, is_sensitive=is_sensitive
            )
        finally:
            _proxy.sleep = _saved_sleep

    DefaultActionWatchdog._input_text_element_node_impl = _human_input_text_impl

    DefaultActionWatchdog._human_typing_patched = True
    logger.info(
        "Human-typing patches applied to DefaultActionWatchdog "
        "(delay %.0f–%.0f ms, hesitation %.0f%% +%.0f–%.0f ms, "
        "pre-type post-click dwell %.0f–%.0f ms)",
        _MIN_DELAY * 1000, _MAX_DELAY * 1000,
        _HESITATION_PROB * 100,
        _HESITATION_EXTRA[0] * 1000, _HESITATION_EXTRA[1] * 1000,
        _PRE_TYPE_POST_CLICK_MIN_S * 1000, _PRE_TYPE_POST_CLICK_MAX_S * 1000,
    )
