import json
import logging
from collections import Counter
from datetime import datetime, timezone

from browser_use import Agent, Browser, BrowserConfig
from langchain_anthropic import ChatAnthropic

from cfg import Cfg
from models import FlightOffer, FlightSearchInput, FlightSearchResponse, ScrapedFlights
from prompts import FLIGHT_SYSTEM_PROMPT_EXTENSION, build_task_prompt

logger = logging.getLogger("flight_scraper.scraper")


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
    scraped_at = datetime.now(timezone.utc).isoformat()

    llm = ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        temperature=0,
    )

    browser = Browser(config=BrowserConfig(headless=cfg.headless))

    # Per-run tracking state for cycle detection
    url_visit_counts: Counter[str] = Counter()
    action_sequence: list[str] = []
    stop_reason: list[str] = []  # mutable container so closures can write to it

    # ── Stop callback ────────────────────────────────────────────────────────
    # register_should_stop_callback: async () -> bool
    # Called at the start of every step; returning True stops the agent cleanly.

    async def should_stop() -> bool:
        if stop_reason:
            logger.warning("Custom stop triggered: %s", stop_reason[0])
            return True
        return False

    # ── Step callback ─────────────────────────────────────────────────────────
    # register_new_step_callback: (BrowserStateSummary, AgentOutput, int) -> None
    # Called after each step completes; used to update tracking state.

    async def on_new_step(browser_state, agent_output, step_number: int) -> None:
        # ── URL cycle detection ──────────────────────────────────────────────
        url: str = getattr(browser_state, "url", "") or ""
        if url:
            url_visit_counts[url] += 1
            if url_visit_counts[url] > cfg.max_url_visits and not stop_reason:
                stop_reason.append(
                    f"URL visited {url_visit_counts[url]} times with no progress: {url!r}"
                )
                return

        # ── Action cycle detection ───────────────────────────────────────────
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
            # Built-in loop detection (detects repeated action patterns)
            loop_detection_enabled=True,
            loop_detection_window=10,
            # Hard upper bound on LLM failures before abort
            max_failures=3,
            register_should_stop_callback=should_stop,
            register_new_step_callback=on_new_step,
        )

        history = await agent.run(max_steps=cfg.max_steps)
        scraped = _parse_scraped_flights(history.final_result())

        # If our custom stop was the reason, annotate the error
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
        await browser.close()

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
