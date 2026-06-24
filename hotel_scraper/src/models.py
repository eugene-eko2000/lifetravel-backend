from typing import Optional
from pydantic import BaseModel, Field


class HotelOffer(BaseModel):
    hotel_name: str = Field(description="Full name of the hotel or property")
    address: Optional[str] = Field(
        default=None,
        description="Street address or neighbourhood description if visible",
    )
    star_rating: Optional[float] = Field(
        default=None,
        description="Official star rating of the property (e.g. 4.0 or 4.5)",
    )
    review_score: Optional[float] = Field(
        default=None,
        description="Guest review score shown on the site (e.g. 8.7 out of 10)",
    )
    review_count: Optional[int] = Field(
        default=None,
        description="Number of guest reviews the score is based on",
    )
    room_type: Optional[str] = Field(
        default=None,
        description="Room or rate type shown in the search result, e.g. 'Standard Double Room'",
    )
    price_per_night: float = Field(
        description="Price per night for one room in the listed currency"
    )
    total_price: float = Field(
        description="Total price for all nights and all rooms in the listed currency"
    )
    currency: str = Field(default="USD", description="ISO currency code, e.g. 'USD' or 'EUR'")
    breakfast_included: Optional[bool] = Field(
        default=None,
        description="True if breakfast is explicitly included in the rate, False if not, null if unknown",
    )
    cancellation_policy: Optional[str] = Field(
        default=None,
        description="Short description of the cancellation policy, e.g. 'Free cancellation before Jun 10'",
    )
    url: Optional[str] = Field(
        default=None,
        description="Direct URL to the hotel or room detail page, if available",
    )


class ScrapedHotels(BaseModel):
    """Structured output produced by the browser_use agent."""
    success: bool = Field(description="True if hotels were found, False if search failed or no results")
    error: Optional[str] = Field(
        default=None,
        description="Reason for failure if success is False, otherwise null",
    )
    offers: list[HotelOffer] = Field(
        default_factory=list,
        description="List of hotel offers found. Empty list if no results or failure.",
    )
    source: str = Field(
        default="booking_com",
        description="Website used to find results, e.g. 'booking_com' or 'hotels_com'",
    )


class HotelSearchResponse(BaseModel):
    """Full API response returned to the caller."""
    success: bool
    error: Optional[str] = None
    offers: list[HotelOffer]
    result_count: int
    source: str
    scraped_at: str
