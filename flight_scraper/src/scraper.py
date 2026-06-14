import json
import logging
from datetime import datetime, timezone

from scraper_common.cfg import Cfg
from scraper_common.scraper_base import run_browser_agent

from models import FlightOffer, FlightSearchInput, FlightSearchResponse, ScrapedFlights
from prompts import FLIGHT_SYSTEM_PROMPT_EXTENSION, build_task_prompt

logger = logging.getLogger("flight_scraper.scraper")


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
    cfg = Cfg.from_env(default_port=8081)
    scraped_at = datetime.now(timezone.utc).isoformat()

    raw, stop_reason = await run_browser_agent(
        cfg=cfg,
        task_prompt=build_task_prompt(search_input),
        system_prompt_extension=FLIGHT_SYSTEM_PROMPT_EXTENSION,
        output_model_schema=ScrapedFlights,
        logger_name="flight_scraper.scraper",
        trace_file=cfg.trace_file or None,
    )

    if raw is None:
        scraped = ScrapedFlights(
            success=False,
            error=stop_reason or "Agent produced no output",
            offers=[],
            source="unknown",
        )
    else:
        scraped = _parse_scraped_flights(raw)
        if stop_reason:
            if scraped.success:
                scraped = ScrapedFlights(
                    success=False,
                    error=stop_reason,
                    offers=scraped.offers,
                    source=scraped.source,
                )
            elif not scraped.error:
                scraped = ScrapedFlights(
                    success=scraped.success,
                    error=stop_reason,
                    offers=scraped.offers,
                    source=scraped.source,
                )

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
