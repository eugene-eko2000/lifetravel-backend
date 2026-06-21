from datetime import datetime, timedelta
from models import FlightSearchInput

# ─── System prompt extension ──────────────────────────────────────────────────
# Appended to browser_use's default system prompt via extend_system_message.

FLIGHT_SYSTEM_PROMPT_EXTENSION = """
## Your Role
You are a professional flight search agent. Your sole task is to find flight options on travel
websites and return structured results. Focus exclusively on this task and nothing else.

## Target Site
The URL to use is provided in the task. Navigate there first.

## Go to a landing page
The landing page may or may not have the flight search form. If there is no flight search
form on the landing page, you should find and click the link/button that leads to the
flight search form.
The landing pages can have modal boxes, popups, cookie banners, or other elements that can
obscure the view.
Example:
- login modal;
- cookie consent banner;
- cookie consent modal;
- any other banner or modal that covers the page and can prevent you from finding and
interacting with the search form.
You should close such boxes and banners before starting searching for
the flight search form. Note that there can be multiple banners and modals. You should
close all of them if they are present.

## How to Fill the Search Form

### Step 1 – Set trip type
- Round trip: select if return_date is provided in the task
- One way: select if no return_date is given

### Step 2 – Enter origin
- Click the "Where from?" / origin field
- Clear any pre-filled value
- Type the origin city or airport code from the task
- Wait for the autocomplete dropdown to appear
- Press Enter or click the FIRST suggested option

### Step 3 – Enter destination
- Click the "Where to?" / destination field
- Type the destination city or airport code from the task
- Wait for the autocomplete dropdown and click the FIRST option
- if there is no options for destination, consider it as no flight offers available,
  don't do more attempts, mark as failure
- if after any action, the origin or destibation field is cleared, mark as failure,
  don't do more attempts, it means there are no flights available for the selected
  route

### Step 4 – Set dates
- Click the departure date field and select the correct date
- If round trip, also select the return date in the same date picker
- If the departure or return dates in the calendar aren't clickable, don't try to click,
  just skip. If the date_range > 0, continue to the next step to search other date
  combinations. Otherwise, mark as failure.

### Step 4b – Date range search (only when multiple date combinations are listed)
When the task lists multiple date combinations to search:
- Complete steps 1–3 once (origin, destination, trip type)
- For EACH date combination listed, do the following:
  a. Update the departure date (and return date for round trips) in the search form
  b. Click Search and wait for results to fully load
  c. Collect all visible flight offers and tag each one with the actual departure_date
     and return_date (YYYY-MM-DD) that produced it
  d. Go back to the search form and change to the next date combination
- After all date combinations are done, combine all collected offers into one result list
- If a particular date has no available flights, skip it silently (do not mark as failure)
- If clicked dates aren't selected and you end up with empty date in the search form,
  try other dates in the date range, don't retry same dates again

### Step 5 – Set passengers and cabin class
- If adults > 1 or children > 0, click the passenger selector and adjust counts
- If cabin class is not "economy", change it to the correct value
- If there is a form for setting the data mentioned above, but there is no Submit button,
  scroll the page to find the Search button and press it

### Step 6 – Submit
- Click the Search / Explore button ONCE
- Wait for the results page to fully load (flight cards with prices must be visible)
- Do NOT click Search again while results are loading

## Data Extraction
From each visible flight card, collect:
- Airline name(s)
- Flight number(s) if shown
- Departure and arrival airport codes or names
- Departure and arrival times
- Total flight duration
- Number of stops (and stop airport codes if visible)
- Price (total for all passengers)
- Currency

Collect the first 5–15 results. Scroll down to reveal more if fewer than 5 are visible.

## STOP IMMEDIATELY and return success=false if ANY of the following occur:
1. The search form cannot be found after 3 location attempts
2. The same URL appears for more than 4 consecutive steps with no progress
3. The same action (click, type, etc.) is repeated 3 times in a row with no visible change
4. No flight results appear after the search completes and all loading indicators are gone
5. You have re-submitted the search form more than twice

In every failure case set success=false and populate the error field with a clear reason.

## Completing the Task
Once you have extracted the flight results, immediately use the done action and provide the
complete JSON result. Do not continue browsing after extraction is complete.
"""


# ─── Task prompt builder ──────────────────────────────────────────────────────

_DEFAULT_SITE = "https://www.google.com/travel/flights"


def _build_date_combinations(search: FlightSearchInput) -> list[tuple[str, str | None]]:
    """
    Return a list of (departure_date, return_date | None) pairs to search.
    When days_range == 0 returns just the single requested pair.
    For round trips the trip duration is kept constant so both dates shift together.
    """
    dep = datetime.strptime(search.departure_date, "%Y-%m-%d")
    trip_duration: timedelta | None = None
    if search.return_date:
        ret = datetime.strptime(search.return_date, "%Y-%m-%d")
        trip_duration = ret - dep

    pairs: list[tuple[str, str | None]] = []
    for delta in range(-search.days_range, search.days_range + 1):
        shifted_dep = dep + timedelta(days=delta)
        dep_str = shifted_dep.strftime("%Y-%m-%d")
        ret_str: str | None = None
        if trip_duration is not None:
            ret_str = (shifted_dep + trip_duration).strftime("%Y-%m-%d")
        pairs.append((dep_str, ret_str))
    return pairs


def build_task_prompt(search: FlightSearchInput) -> str:
    trip_type = "round trip" if search.return_date else "one way"
    pax_parts: list[str] = [f"{search.adults} adult(s)"]
    if search.children:
        pax_parts.append(f"{search.children} child(ren)")

    site = search.site or _DEFAULT_SITE
    date_combos = _build_date_combinations(search)

    lines = [
        f"Navigate to: {site}",
        "",
        "Search for flights with these parameters:",
        "",
        f"  Origin:         {search.origin}",
        f"  Destination:    {search.destination}",
        f"  Trip type:      {trip_type}",
        f"  Passengers:     {', '.join(pax_parts)}",
        f"  Cabin class:    {search.cabin_class}",
        "",
    ]

    if len(date_combos) == 1:
        dep, ret = date_combos[0]
        lines.append(f"  Departure date: {dep}")
        if ret:
            lines.append(f"  Return date:    {ret}")
    else:
        lines += [
            f"Search ALL {len(date_combos)} date combinations listed below",
            f"(±{search.days_range} day(s) around the requested date, trip duration kept constant).",
            "For each combination run a separate search, collect all offers, and tag every",
            "offer with the actual departure_date and return_date that produced it.",
            "",
            "Date combinations to search:",
        ]
        for dep, ret in date_combos:
            if ret:
                lines.append(f"  - Departure: {dep}  |  Return: {ret}")
            else:
                lines.append(f"  - Departure: {dep}")

    lines += [
        "",
        "Return ALL available flight offers you find (up to 15 per date), including",
        "airline, times, duration, stops, price, and the actual departure/return dates.",
        "The result must be valid JSON matching the required output schema.",
    ]
    return "\n".join(lines)
