import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Cfg:
    port: int
    # LLM provider: "anthropic" | "openai" | "google" | "deepseek" | "browseruse"
    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_model: str
    gemini_api_key: str
    gemini_model: str
    deepseek_api_key: str
    deepseek_model: str
    browseruse_api_key: str
    browseruse_model: str
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
    # Browser engine: "chrome" (Chromium via Playwright) or "camoufox" (Firefox-based stealth)
    browser_type: str
    # Browser channel: "chrome" uses the real Chrome binary (better stealth);
    # empty string falls back to the bundled Chromium.
    browser_channel: str | None
    # CDP endpoint of an already-running browser, e.g. "http://localhost:9222".
    # When set, the agent attaches to that browser instead of launching a new
    # one, and we don't shut it down on exit. Empty string → launch fresh.
    browser_cdp_url: str
    # Path to a Chrome user-data dir. When set on the launch path, the new
    # browser process loads cookies, history, extensions and prefs from that
    # directory and writes back to it on exit. Empty string → ephemeral
    # profile. Ignored when attaching via browser_cdp_url.
    user_data_dir: str
    # reCAPTCHA solver: "audio" (Whisper), "token" (2captcha/anticaptcha), "none"
    # Note: audio mode reuses openai_api_key above for Whisper transcription.
    captcha_solver: str
    captcha_solver_api_key: str   # 2captcha / anti-captcha API key
    captcha_solver_service: str   # "2captcha" or "anticaptcha"
    # Logging level: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    log_level: str
    # Path to a JSONL file for tracing every LLM response. Empty = disabled.
    trace_file: str

    @classmethod
    def from_env(cls, default_port: int = 8080) -> "Cfg":
        channel_raw = os.getenv("BROWSER_CHANNEL", "chrome").strip()
        return cls(
            port=int(os.getenv("PORT", str(default_port))),
            llm_provider=os.getenv("LLM_PROVIDER", "anthropic").lower().strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            browseruse_api_key=os.getenv("BROWSERUSE_API_KEY", ""),
            browseruse_model=os.getenv("BROWSERUSE_MODEL", ""),
            headless=os.getenv("HEADLESS", "true").lower() != "false",
            max_steps=int(os.getenv("MAX_STEPS", "30")),
            max_url_visits=int(os.getenv("MAX_URL_VISITS", "5")),
            action_repeat_threshold=int(os.getenv("ACTION_REPEAT_THRESHOLD", "3")),
            wait_between_actions=float(os.getenv("WAIT_BETWEEN_ACTIONS", "0.1")),
            min_page_load_wait=float(os.getenv("MIN_PAGE_LOAD_WAIT", "2.0")),
            browser_type=os.getenv("BROWSER_TYPE", "chrome").lower().strip(),
            browser_channel=channel_raw if channel_raw else None,
            browser_cdp_url=os.getenv("BROWSER_CDP_URL", "").strip(),
            user_data_dir=os.getenv("USER_DATA_DIR", "").strip(),
            captcha_solver=os.getenv("CAPTCHA_SOLVER", "none").lower().strip(),
            captcha_solver_api_key=os.getenv("CAPTCHA_SOLVER_API_KEY", ""),
            captcha_solver_service=os.getenv("CAPTCHA_SOLVER_SERVICE", "2captcha").lower().strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip(),
            trace_file=os.getenv("TRACE_FILE", "").strip(),
        )
