"""
Shared browser-use agent runner used by all domain-specific scrapers.

Call run_browser_agent() with domain-specific prompts and output schema.
Returns (raw_output, stop_reason) where stop_reason is set when a cycle was
detected or an exception occurred.
"""
import logging
from collections import Counter
from typing import Any

from browser_use import Agent, Browser
from browser_use.llm import ChatAnthropic
from playwright_stealth import Stealth

from scraper_common.cfg import Cfg
from scraper_common.captcha_solver import CaptchaSolver
from scraper_common.human_typing import patch_watchdog_typing
from scraper_common.human_mouse import patch_mouse_movement

# Apply human-like keystroke timing and mouse movement before any Agent is instantiated.
patch_watchdog_typing()
patch_mouse_movement()

# Shared UA used both in the browser profile and in the stealth JS patches so
# navigator.userAgent, the HTTP header, and the sec-ch-ua hint all agree.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# One Stealth instance is enough — apply_stealth_async is stateless per call.
_STEALTH = Stealth(
    navigator_user_agent_override=_USER_AGENT,
    # Match the platform to our macOS UA so navigator.platform is consistent.
    navigator_platform_override="MacIntel",
    # Expose window.chrome so sites that probe it see a "real" Chrome object.
    chrome_runtime=True,
)


def _detect_action_cycle(sequence: list[str], window: int, threshold: int) -> bool:
    """Return True if the last `window` actions repeat `threshold` times in a row."""
    if len(sequence) < window * threshold:
        return False
    tail = sequence[-window:]
    for i in range(1, threshold):
        prev = sequence[-(window * (i + 1)):-(window * i)]
        if tail != prev:
            return False
    return True


async def run_browser_agent(
    cfg: Cfg,
    task_prompt: str,
    system_prompt_extension: str,
    output_model_schema: Any,
    logger_name: str = "scraper_common.scraper_base",
) -> tuple[str | None, str | None]:
    """
    Run a browser-use agent with stealth patches and cycle detection.

    Returns (raw_output, stop_reason):
      - raw_output: the agent's final result string, or None on hard failure
      - stop_reason: set when a loop/exception caused early termination
    """
    logger = logging.getLogger(logger_name)
    captcha_solver = CaptchaSolver.from_cfg(cfg)

    llm = ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        temperature=0,
    )

    # Attach to an externally-managed browser when BROWSER_CDP_URL is set;
    # otherwise launch a fresh instance ourselves. Launch-only kwargs
    # (channel, args, headless, user_agent) are skipped on the attach path
    # since the running browser already has them.
    if cfg.browser_cdp_url:
        logger.info("Attaching to running browser at %s", cfg.browser_cdp_url)
        browser = Browser(
            cdp_url=cfg.browser_cdp_url,
            keep_alive=True,
            wait_between_actions=cfg.wait_between_actions,
            minimum_wait_page_load_time=cfg.min_page_load_wait,
        )
    else:
        if cfg.user_data_dir:
            logger.info("Launching browser with user-data dir %s", cfg.user_data_dir)
        browser = Browser(
            headless=cfg.headless,
            channel=cfg.browser_channel,
            args=[
                "--disable-blink-features=AutomationControlled",
                # Cookies enabled — disable Chrome 136+ Tracking Protection
                # (which blocks third-party cookies by default) and storage
                # partitioning, so auth flows, embedded widgets and any
                # cross-origin cookie handshake work as they did pre-2025.
                "--disable-features=TrackingProtection3pcd,ThirdPartyStoragePartitioning",
            ],
            ignore_default_args=["--enable-automation"],
            user_agent=_USER_AGENT,
            # user_data_dir=cfg.user_data_dir or None,
            wait_between_actions=cfg.wait_between_actions,
            minimum_wait_page_load_time=cfg.min_page_load_wait,
        )

    # Per-run tracking state for cycle detection
    url_visit_counts: Counter[str] = Counter()
    action_sequence: list[str] = []
    stop_reason: list[str] = []  # mutable container so closures can write to it
    stealth_applied_pages: set[int] = set()

    # ── Pre-step hook ─────────────────────────────────────────────────────────

    async def apply_stealth(agent: Agent) -> None:
        # browser_use 0.12.x uses raw CDP instead of Playwright's API layer, so
        # the Page object has no add_init_script().  We call the underlying CDP
        # method directly: Page.addScriptToEvaluateOnNewDocument registers a
        # script that Chrome executes before any JS on every navigation — the
        # same mechanism Playwright's add_init_script() uses internally.
        try:
            page = await agent.browser_session.get_current_page()
            pid = id(page)
            if pid not in stealth_applied_pages and _STEALTH.script_payload:
                session_id = await page._ensure_session()
                await page._client.send.Page.addScriptToEvaluateOnNewDocument(
                    {"source": _STEALTH.script_payload},
                    session_id=session_id,
                )
                stealth_applied_pages.add(pid)
                logger.debug("Stealth patches applied to page id=%d via CDP", pid)
        except Exception:
            logger.warning("Could not apply stealth patches", exc_info=True)

    # ── Stop callback ────────────────────────────────────────────────────────
    async def should_stop() -> bool:
        if stop_reason:
            logger.warning("Custom stop triggered: %s", stop_reason[0])
            return True
        return False

    # ── Step callback ─────────────────────────────────────────────────────────
    async def on_new_step(browser_state, agent_output, step_number: int) -> None:
        # URL cycle detection
        url: str = getattr(browser_state, "url", "") or ""
        if url:
            url_visit_counts[url] += 1
            if url_visit_counts[url] > cfg.max_url_visits and not stop_reason:
                stop_reason.append(
                    f"URL visited {url_visit_counts[url]} times with no progress: {url!r}"
                )
                return

        # Action cycle detection
        if agent_output is not None:
            actions = getattr(agent_output, "action", None) or []
            if not isinstance(actions, list):
                actions = [actions]
            for action in actions:
                if action is not None:
                    action_sequence.append(type(action).__name__)

            if (
                not stop_reason
                and _detect_action_cycle(
                    action_sequence,
                    window=cfg.action_repeat_threshold,
                    threshold=2,
                )
            ):
                stop_reason.append(
                    f"Action cycle detected: {action_sequence[-cfg.action_repeat_threshold * 2:]}"
                )

        logger.debug(
            "Step %d | url=%s | actions=%s",
            step_number,
            url,
            [type(a).__name__ for a in (getattr(agent_output, "action", None) or [])],
        )

    try:
        agent = Agent(
            task=task_prompt,
            llm=llm,
            browser=browser,
            extend_system_message=system_prompt_extension,
            output_model_schema=output_model_schema,
            loop_detection_enabled=True,
            loop_detection_window=10,
            max_failures=3,
            register_should_stop_callback=should_stop,
            register_new_step_callback=on_new_step,
        )

        async def on_step_start(agent: Agent) -> None:
            await apply_stealth(agent)
            await captcha_solver.handle(agent)

        history = await agent.run(
            max_steps=cfg.max_steps,
            on_step_start=on_step_start,
        )
        raw = history.final_result()
        return raw, (stop_reason[0] if stop_reason else None)

    except Exception as exc:
        logger.exception("Browser agent raised an exception")
        return None, str(exc)
    finally:
        # Only stop browsers we launched ourselves — leave externally-attached
        # ones running for the next call / for the operator to inspect.
        if not cfg.browser_cdp_url:
            await browser.stop()
