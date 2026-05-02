import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Cfg:
    port: int
    anthropic_api_key: str
    anthropic_model: str
    headless: bool
    max_steps: int
    # Number of times the same URL may appear before we consider it a cycle
    max_url_visits: int
    # Consecutive identical actions before we consider it a cycle
    action_repeat_threshold: int
    # Seconds to wait between every browser action (human-like pacing)
    wait_between_actions: float
    # Minimum seconds to wait after a page starts loading
    min_page_load_wait: float
    # Browser channel: "chrome" uses the real Chrome binary (better stealth);
    # empty string falls back to the bundled Chromium.
    browser_channel: str | None

    @classmethod
    def from_env(cls) -> "Cfg":
        channel_raw = os.getenv("BROWSER_CHANNEL", "chrome").strip()
        return cls(
            port=int(os.getenv("PORT", "8081")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            headless=os.getenv("HEADLESS", "true").lower() != "false",
            max_steps=int(os.getenv("MAX_STEPS", "30")),
            max_url_visits=int(os.getenv("MAX_URL_VISITS", "5")),
            action_repeat_threshold=int(os.getenv("ACTION_REPEAT_THRESHOLD", "3")),
            wait_between_actions=float(os.getenv("WAIT_BETWEEN_ACTIONS", "1.5")),
            min_page_load_wait=float(os.getenv("MIN_PAGE_LOAD_WAIT", "2.0")),
            browser_channel=channel_raw if channel_raw else None,
        )
