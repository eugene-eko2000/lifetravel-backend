import json
import logging
from collections import Counter
from datetime import datetime, timezone

from browser_use import Agent, Browser
from browser_use.llm import ChatAnthropic
from playwright_stealth import Stealth

from cfg import Cfg
from captcha_solver import CaptchaSolver
from human_typing import patch_watchdog_typing
from human_mouse import patch_mouse_movement

# Apply human-like keystroke timing and mouse movement before any Agent is instantiated.
patch_watchdog_typing()
patch_mouse_movement()
from models import FlightOffer, FlightSearchInput, FlightSearchResponse, ScrapedFlights
from prompts import FLIGHT_SYSTEM_PROMPT_EXTENSION, build_task_prompt

logger = logging.getLogger("flight_scraper.scraper")

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


def _parse_scraped_flights(raw: str | None) -> ScrapedFlights:
    """Parse the agent's final string output into ScrapedFlights, with fallback."""
    if not raw:
        return ScrapedFlights(
            success=False,
            error="Agent produced no output",
            offers=[],
            source="unknown",
        )
    try:
        data = json.loads(raw)
        return ScrapedFlights.model_validate(data)
    except Exception as exc:
        logger.warning("Could not parse agent output as ScrapedFlights: %s — raw: %.200s", exc, raw)
        return ScrapedFlights(
            success=False,
            error=f"Output parse error: {exc}",
            offers=[],
            source="unknown",
        )


async def search_flights(search_input: FlightSearchInput) -> FlightSearchResponse:
    cfg = Cfg.from_env()
    captcha_solver = CaptchaSolver.from_cfg(cfg)
    scraped_at = datetime.now(timezone.utc).isoformat()

    llm = ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        temperature=0,
    )

    browser = Browser(
        headless=cfg.headless,
        channel=cfg.browser_channel,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
        user_agent=_USER_AGENT,
        wait_between_actions=cfg.wait_between_actions,
        minimum_wait_page_load_time=cfg.min_page_load_wait,
    )

    # Per-run tracking state for cycle detection
    url_visit_counts: Counter[str] = Counter()
    action_sequence: list[str] = []
    stop_reason: list[str] = []  # mutable container so closures can write to it
    stealth_applied_pages: set[int] = set()

    # ── Pre-step hook ─────────────────────────────────────────────────────────
    # on_step_start runs before each agent step.  We use it to (1) inject
    # stealth JS on any newly opened page and (2) resolve reCAPTCHA challenges
    # before the agent sees them.

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
            task=build_task_prompt(search_input),
            llm=llm,
            browser=browser,
            extend_system_message=FLIGHT_SYSTEM_PROMPT_EXTENSION,
            output_model_schema=ScrapedFlights,
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
        scraped = _parse_scraped_flights(history.final_result())

        if stop_reason and scraped.success:
            scraped = ScrapedFlights(
                success=False,
                error=stop_reason[0],
                offers=scraped.offers,
                source=scraped.source,
            )
        elif stop_reason and not scraped.error:
            scraped = ScrapedFlights(
                success=scraped.success,
                error=stop_reason[0],
                offers=scraped.offers,
                source=scraped.source,
            )

    except Exception as exc:
        logger.exception("Flight scraper agent raised an exception")
        scraped = ScrapedFlights(
            success=False,
            error=str(exc),
            offers=[],
            source="unknown",
        )
    finally:
        await browser.stop()

    offers: list[FlightOffer] = scraped.offers or []
    return FlightSearchResponse(
        success=scraped.success,
        error=scraped.error,
        search_params=search_input,
        offers=offers,
        result_count=len(offers),
        source=scraped.source,
        scraped_at=scraped_at,
    )
