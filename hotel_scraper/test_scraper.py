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

from scraper import search_hotels

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

CHECKIN_DATE = (datetime.now() + timedelta(days=30)).date().isoformat()
CHECKOUT_DATE = (datetime.now() + timedelta(days=37)).date().isoformat()

SITE = "https://booking.com"

SEARCH_REQUEST = f"""
Find hotels in Barcelona from {CHECKIN_DATE} to {CHECKOUT_DATE} for 2 people.
A number of rooms: 1.

Apply the following criteria:

Accommodation type: hotels only, no apartment or other types;
Use review score 8.0 or higher;
Consider hotels with max. 150 euro per night, set currency = euro on the site;
Find only hotels with air conditioning.
"""


async def main() -> None:
    print("=" * 60)
    print("Hotel search request:")
    print(SEARCH_REQUEST)
    print("=" * 60)

    result = await search_hotels(SITE, SEARCH_REQUEST)

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
