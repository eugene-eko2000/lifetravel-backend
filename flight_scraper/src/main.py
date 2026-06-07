import logging

import uvicorn
from fastapi import FastAPI

from scraper_common.cfg import Cfg
from models import FlightSearchInput, FlightSearchResponse
from scraper import search_flights

logger = logging.getLogger("flight_scraper")

app = FastAPI(title="Flight Scraper API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/search", response_model=FlightSearchResponse)
async def search(request: FlightSearchInput) -> FlightSearchResponse:
    logger.info(
        "Flight search: %s → %s on %s (return: %s)",
        request.origin,
        request.destination,
        request.departure_date,
        request.return_date or "—",
    )
    result = await search_flights(request)
    logger.info(
        "Search done: success=%s offers=%d source=%s",
        result.success,
        result.result_count,
        result.source,
    )
    return result


if __name__ == "__main__":
    cfg = Cfg.from_env(default_port=8081)
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger.info("Starting Flight Scraper service on port %d", cfg.port)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)
