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

    @classmethod
    def from_env(cls) -> "Cfg":
        return cls(
            port=int(os.getenv("PORT", "8081")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            headless=os.getenv("HEADLESS", "true").lower() != "false",
            max_steps=int(os.getenv("MAX_STEPS", "30")),
            max_url_visits=int(os.getenv("MAX_URL_VISITS", "5")),
            action_repeat_threshold=int(os.getenv("ACTION_REPEAT_THRESHOLD", "3")),
        )
