import logging

import uvicorn
from fastapi import FastAPI

from scraper_common.cfg import Cfg
from models import HotelSearchInput, HotelSearchResponse
from scraper import search_hotels

logger = logging.getLogger("hotel_scraper")

app = FastAPI(title="Hotel Scraper API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/search", response_model=HotelSearchResponse)
async def search(request: HotelSearchInput) -> HotelSearchResponse:
    logger.info(
        "Hotel search: %s | %s → %s | %d guest(s) %d room(s)",
        request.destination,
        request.check_in,
        request.check_out,
        request.guests,
        request.rooms,
    )
    result = await search_hotels(request)
    logger.info(
        "Search done: success=%s offers=%d source=%s",
        result.success,
        result.result_count,
        result.source,
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cfg = Cfg.from_env(default_port=8082)
    logger.info("Starting Hotel Scraper service on port %d", cfg.port)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)
