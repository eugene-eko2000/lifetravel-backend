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
    # Seconds to wait between browser actions (kept small; organic timing comes
    # from the human-mouse/typing patches, not this artificial delay)
    wait_between_actions: float
    # Minimum seconds to wait after a page starts loading
    min_page_load_wait: float
    # Browser channel: "chrome" uses the real Chrome binary (better stealth);
    # empty string falls back to the bundled Chromium.
    browser_channel: str | None
    # reCAPTCHA solver: "audio" (Whisper), "token" (2captcha/anticaptcha), "none"
    captcha_solver: str
    openai_api_key: str           # OpenAI key for Whisper transcription
    captcha_solver_api_key: str   # 2captcha / anti-captcha API key
    captcha_solver_service: str   # "2captcha" or "anticaptcha"

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
            wait_between_actions=float(os.getenv("WAIT_BETWEEN_ACTIONS", "0.1")),
            min_page_load_wait=float(os.getenv("MIN_PAGE_LOAD_WAIT", "2.0")),
            browser_channel=channel_raw if channel_raw else None,
            captcha_solver=os.getenv("CAPTCHA_SOLVER", "none").lower().strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            captcha_solver_api_key=os.getenv("CAPTCHA_SOLVER_API_KEY", ""),
            captcha_solver_service=os.getenv("CAPTCHA_SOLVER_SERVICE", "2captcha").lower().strip(),
        )
