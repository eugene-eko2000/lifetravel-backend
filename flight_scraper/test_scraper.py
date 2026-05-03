"""
Run with:
    cd flight_scraper
    python test_scraper.py
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from models import FlightSearchInput
from scraper import search_flights

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

REQUEST = FlightSearchInput(
    origin="Zurich",
    destination="New York",
    departure_date="2026-06-15",
    return_date="2026-06-22",
    adults=1,
    children=0,
    cabin_class="economy",
    site="https://lufthansa.com",  # uncomment to test a different site
)


async def main() -> None:
    print("=" * 60)
    print("Flight search request:")
    print(REQUEST.model_dump_json(indent=2))
    print("=" * 60)

    result = await search_flights(REQUEST)

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
