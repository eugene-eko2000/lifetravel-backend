"""
Patches browser_use's DefaultActionWatchdog to use human-like inter-keystroke
timing instead of the default ≤10 ms robotic delays.

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


def _human_delay() -> float:
    """Return a randomised human-like inter-keystroke delay in seconds."""
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    if random.random() < _HESITATION_PROB:
        delay += random.uniform(*_HESITATION_EXTRA)
    return delay


def patch_watchdog_typing() -> None:
    """
    Replace the two typing implementations in DefaultActionWatchdog with
    versions that use _human_delay() between every keystroke.

    Strategy
    --------
    * _type_to_page      – simple method; fully rewritten here.
    * _input_text_element_node_impl – complex (200+ lines); instead of copying
      it, we install a per-call proxy for asyncio.sleep in the watchdog
      module's namespace.  Only sub-20 ms sleeps (the inter-keystroke ones)
      are replaced; longer sleeps (scroll waits, etc.) pass through unchanged.
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
        # sleep is set as an instance attribute below so it can be swapped
        # per-call without affecting the class.
        def __getattr__(self, name: str):
            return getattr(_real_asyncio, name)

    _proxy = _AsyncioProxy()
    _proxy.sleep = _real_asyncio.sleep  # default: real sleep
    _wdog_mod.asyncio = _proxy          # watchdog module now sees the proxy

    # ── Patch 1: _type_to_page (fallback typing path) ─────────────────────────
    # Rewrite of the original; only change is replacing asyncio.sleep(0.010)
    # with _human_delay().

    async def _human_type_to_page(self, text: str) -> None:
        try:
            cdp_session = await self.browser_session.get_or_create_cdp_session(
                target_id=None, focus=True
            )
            for char in text:
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
                await _real_asyncio.sleep(_human_delay())
        except Exception as exc:
            raise Exception(f"Failed to type to page: {exc}") from exc

    DefaultActionWatchdog._type_to_page = _human_type_to_page

    # ── Patch 2: _input_text_element_node_impl (primary typing path) ──────────
    # Wrap the original so that for the duration of each call the proxy's
    # .sleep attribute becomes a human-speed version.  Sub-20 ms delays
    # (the inter-keystroke ones: 1 ms, 5 ms, 10 ms) become _human_delay();
    # larger delays (scroll waits, etc.) pass through unchanged.

    _orig_impl = DefaultActionWatchdog._input_text_element_node_impl

    async def _human_input_text_impl(
        self, element_node, text, clear=True, is_sensitive=False
    ):
        _saved_sleep = _proxy.sleep

        async def _fast_to_human(delay: float) -> None:
            if delay < 0.02:
                await _real_asyncio.sleep(_human_delay())
            else:
                await _real_asyncio.sleep(delay)

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
        "(delay %.0f–%.0f ms, hesitation %.0f%% +%.0f–%.0f ms)",
        _MIN_DELAY * 1000, _MAX_DELAY * 1000,
        _HESITATION_PROB * 100,
        _HESITATION_EXTRA[0] * 1000, _HESITATION_EXTRA[1] * 1000,
    )
