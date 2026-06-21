"""
Run with:
    cd hotel_scraper
    python test_scraper.py
"""
import asyncio
from datetime import datetime, timedelta
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper_common" / "src"))
sys.path.insert(0, str(Path(__file__).parent / "src"))

from models import HotelSearchInput
from scraper import search_hotels

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

CHECKIN_DATE = (datetime.now() + timedelta(days=30)).date().isoformat()
CHECKOUT_DATE = (datetime.now() + timedelta(days=37)).date().isoformat()

REQUEST = HotelSearchInput(
    destination="Paris",
    check_in=CHECKIN_DATE,
    check_out=CHECKOUT_DATE,
    guests=2,
    rooms=1,
    min_stars=4,
    # site="https://www.hotels.com",  # uncomment to test a different site
    site="https://www.booking.com",  # uncomment to test a different site
)


async def main() -> None:
    print("=" * 60)
    print("Hotel search request:")
    print(REQUEST.model_dump_json(indent=2))
    print("=" * 60)

    result = await search_hotels(REQUEST)

    print("\nResult:")
    print(json.dumps(result.model_dump(), indent=2))
    print("=" * 60)
    print(f"Success:      {result.success}")
    print(f"Offers found: {result.result_count}")
    print(f"Source:       {result.source}")
    if result.error:
        print(f"Error:        {result.error}")
    print("=" * 60)

    if not result.success or result.result_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
