from typing import Optional
from pydantic import BaseModel, Field


class FlightSearchInput(BaseModel):
    origin: str = Field(description="Origin city or IATA airport code, e.g. 'New York' or 'JFK'")
    destination: str = Field(description="Destination city or IATA airport code, e.g. 'London' or 'LHR'")
    departure_date: str = Field(description="Departure date in YYYY-MM-DD format")
    return_date: Optional[str] = Field(
        default=None,
        description="Return date in YYYY-MM-DD format. Omit or null for one-way trip.",
    )
    adults: int = Field(default=1, ge=1, le=9, description="Number of adult passengers")
    children: int = Field(default=0, ge=0, le=8, description="Number of child passengers (ages 2-11)")
    cabin_class: str = Field(
        default="economy",
        description="Cabin class: economy, premium_economy, business, or first",
    )
    site: Optional[str] = Field(
        default=None,
        description=(
            "URL of the flight search site to use, e.g. 'https://www.google.com/travel/flights'. "
            "Defaults to Google Flights when omitted."
        ),
    )


class FlightSegment(BaseModel):
    airline: str = Field(description="Airline name, e.g. 'Delta' or 'Lufthansa'")
    flight_number: Optional[str] = Field(default=None, description="Flight number if visible, e.g. 'DL 123'")
    departure_airport: str = Field(description="Departure airport code or name")
    arrival_airport: str = Field(description="Arrival airport code or name")
    departure_time: str = Field(description="Departure time, e.g. '09:00 AM' or '14:30'")
    arrival_time: str = Field(description="Arrival time, e.g. '12:30 PM' or '18:45'")
    duration: str = Field(description="Flight duration, e.g. '3h 30m'")
    stops: int = Field(description="Number of stops: 0 = nonstop, 1 = one stop, etc.")
    stop_airports: Optional[list[str]] = Field(
        default=None,
        description="List of intermediate stop airport codes or names, if any",
    )


class FlightOffer(BaseModel):
    price: float = Field(description="Total price for all passengers combined")
    currency: str = Field(default="USD", description="ISO currency code, e.g. 'USD' or 'EUR'")
    outbound: FlightSegment = Field(description="Outbound (departure) flight details")
    inbound: Optional[FlightSegment] = Field(
        default=None,
        description="Return (inbound) flight details for round trips, null for one-way",
    )


class ScrapedFlights(BaseModel):
    """Structured output produced by the browser_use agent."""
    success: bool = Field(description="True if flights were found, False if search failed or no results")
    error: Optional[str] = Field(
        default=None,
        description="Reason for failure if success is False, otherwise null",
    )
    offers: list[FlightOffer] = Field(
        default_factory=list,
        description="List of flight offers found. Empty list if no results or failure.",
    )
    source: str = Field(
        default="google_flights",
        description="Website used to find results, e.g. 'google_flights' or 'kayak'",
    )


class FlightSearchResponse(BaseModel):
    """Full API response returned to the caller."""
    success: bool
    error: Optional[str] = None
    search_params: FlightSearchInput
    offers: list[FlightOffer]
    result_count: int
    source: str
    scraped_at: str
