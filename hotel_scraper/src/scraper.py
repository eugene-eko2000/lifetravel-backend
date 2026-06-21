import json
import logging
from datetime import datetime, timezone

from scraper_common.cfg import Cfg
from scraper_common.scraper_base import run_browser_agent

from models import HotelOffer, HotelSearchResponse, ScrapedHotels
from prompts import HOTEL_SYSTEM_PROMPT_EXTENSION, build_task_prompt

logger = logging.getLogger("hotel_scraper.scraper")


def _parse_scraped_hotels(raw: str | None) -> ScrapedHotels:
    """Parse the agent's final string output into ScrapedHotels, with fallback."""
    if not raw:
        return ScrapedHotels(
            success=False,
            error="Agent produced no output",
            offers=[],
            source="unknown",
        )
    try:
        data = json.loads(raw)
        return ScrapedHotels.model_validate(data)
    except Exception as exc:
        logger.warning("Could not parse agent output as ScrapedHotels: %s — raw: %.200s", exc, raw)
        return ScrapedHotels(
            success=False,
            error=f"Output parse error: {exc}",
            offers=[],
            source="unknown",
        )


async def search_hotels(site: str, search_input: str) -> HotelSearchResponse:
    cfg = Cfg.from_env(default_port=8082)
    scraped_at = datetime.now(timezone.utc).isoformat()

    raw, stop_reason = await run_browser_agent(
        cfg=cfg,
        task_prompt=build_task_prompt(site, search_input),
        system_prompt_extension=HOTEL_SYSTEM_PROMPT_EXTENSION,
        output_model_schema=ScrapedHotels,
        logger_name="hotel_scraper.scraper",
        trace_file=cfg.trace_file or None,
    )

    if raw is None:
        scraped = ScrapedHotels(
            success=False,
            error=stop_reason or "Agent produced no output",
            offers=[],
            source="unknown",
        )
    else:
        scraped = _parse_scraped_hotels(raw)
        if stop_reason:
            if scraped.success:
                scraped = ScrapedHotels(
                    success=False,
                    error=stop_reason,
                    offers=scraped.offers,
                    source=scraped.source,
                )
            elif not scraped.error:
                scraped = ScrapedHotels(
                    success=scraped.success,
                    error=stop_reason,
                    offers=scraped.offers,
                    source=scraped.source,
                )

    offers: list[HotelOffer] = scraped.offers or []
    return HotelSearchResponse(
        success=scraped.success,
        error=scraped.error,
        search_params=search_input,
        offers=offers,
        result_count=len(offers),
        source=scraped.source,
        scraped_at=scraped_at,
    )
