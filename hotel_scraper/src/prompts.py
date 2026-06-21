from models import HotelSearchInput

# ─── System prompt extension ──────────────────────────────────────────────────
# Appended to browser_use's default system prompt via extend_system_message.

HOTEL_SYSTEM_PROMPT_EXTENSION = """
## Your Role
You are a professional hotel search agent. Your sole task is to find hotel options on travel
websites and return structured results. Focus exclusively on this task and nothing else.

## Target Site
The URL to use is provided in the task. Navigate there first.
If the site is blocked, unavailable, or showing a CAPTCHA, report failure immediately —
do not silently switch to another site.

## How to Fill the Search Form

### Step 1

Detect and close any modal boxes, popups, cookie banners, or other elements thatcan obscure
the view. Examples:
- login modal;
- cookie consent banner;
- cookie consent modal;
- any other banner or modal that covers the page and can prevent you from finding and
  interacting with the search form. You should close such boxes and banners before starting
  searching for the hotel search form. Note that there can be multiple banners and modals.

### Step 2 - Simulate the page exploring.
- perform several scrolls down and up and mouse movements to explore the page before
  interacting with the search form.

### Step 3 – Enter destination
- Click the destination / search field
- Clear any pre-filled value
- Type the destination from the task
- Wait for the autocomplete dropdown to appear
- Press Enter or click the FIRST suggested option that matches the destination

### Step 4 – Set dates
- Click the check-in date field and select the correct date from the calendar
- Then select the check-out date in the same or adjacent calendar widget
- Confirm the selection if the site requires it (e.g. click "Done" or "Apply")

### Step 5 – Set occupancy
- Click the guests / rooms selector
- Set the number of adults to match the task
- Set the number of rooms to match the task
- Confirm or close the selector

### Step 6 – Submit
- Click the Search button ONCE
- Wait for the results page to fully load (hotel cards with prices must be visible)
- Do NOT click Search again while results are loading

### Step 7 – Apply star filter (only if min_stars is set in the task)
- Look for a star rating filter on the results page
- Select the minimum star rating specified in the task
- Wait for the results to refresh before extracting data

## Data Extraction
From each visible hotel card, collect:
- Hotel name
- Address or location description (if visible)
- Star rating (official classification, e.g. 4 stars)
- Review score and number of reviews (if shown)
- Room type listed in the result (e.g. "Standard Double Room", "Deluxe King")
- Price per night (for one room)
- Total price for the full stay (all nights, all rooms)
- Currency
- Whether breakfast is included in the rate
- Cancellation policy summary (e.g. "Free cancellation", "Non-refundable")
- URL to the hotel detail page (if available in the card)

Collect the first 5–15 results. Scroll down to reveal more if fewer than 5 are visible.
If a "Load more" or pagination control is present, use it once to get additional results.

## STOP IMMEDIATELY and return success=false if ANY of the following occur:
1. The search form cannot be found after 3 location attempts
2. The same URL appears for more than 4 consecutive steps with no progress
3. The same action (click, type, etc.) is repeated 3 times in a row with no visible change
4. No hotel results appear after the search completes and all loading indicators are gone
5. You have re-submitted the search form more than twice

In every failure case set success=false and populate the error field with a clear reason.

## Completing the Task
Once you have extracted the hotel results, immediately use the done action and provide the
complete JSON result. Do not continue browsing after extraction is complete.
"""


# ─── Task prompt builder ──────────────────────────────────────────────────────

_DEFAULT_SITE = "https://www.booking.com"


def build_task_prompt(search: HotelSearchInput) -> str:
    nights = _count_nights(search.check_in, search.check_out)
    nights_label = f"{nights} night(s)" if nights else "unknown nights"

    lines = [
        f"Navigate to: {search.site or _DEFAULT_SITE}",
        "",
        "Search for hotels with these exact parameters:",
        "",
        f"  Destination:  {search.destination}",
        f"  Check-in:     {search.check_in}",
        f"  Check-out:    {search.check_out}",
        f"  Stay:         {nights_label}",
        f"  Guests:       {search.guests} adult(s)",
        f"  Rooms:        {search.rooms}",
    ]
    if search.min_stars is not None:
        lines.append(f"  Min stars:    {search.min_stars}+")

    lines += [
        "",
        "Return ALL available hotel offers you find, including hotel name, address,",
        "star rating, review score, room type, price per night, total price, currency,",
        "breakfast inclusion, and cancellation policy.",
        "The result must be valid JSON matching the required output schema.",
    ]
    return "\n".join(lines)


def _count_nights(check_in: str, check_out: str) -> int | None:
    try:
        from datetime import date
        ci = date.fromisoformat(check_in)
        co = date.fromisoformat(check_out)
        return (co - ci).days
    except Exception:
        return None
