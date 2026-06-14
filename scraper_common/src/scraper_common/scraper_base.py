"""
Shared browser-use agent runner used by all domain-specific scrapers.

Call run_browser_agent() with domain-specific prompts and output schema.
Returns (raw_output, stop_reason) where stop_reason is set when a cycle was
detected or an exception occurred.
"""
import contextlib
import logging
import platform
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from browser_use import Agent, Browser
from browser_use.llm import BaseChatModel
from playwright_stealth import Stealth

from scraper_common.cfg import Cfg
from scraper_common.captcha_solver import CaptchaSolver
from scraper_common.human_typing import patch_watchdog_typing
from scraper_common.human_mouse import patch_mouse_movement
from scraper_common.human_scrolling import patch_scroll_page
from scraper_common.model_tracer import ModelTracer

# Apply human-like interaction patches before any Agent is instantiated.
# mouse MUST be patched first — the typing patch calls dispatchMouseEvent for
# the pre-typing focus sequence, so the Bézier/hover/dwell logic must already
# be in place when patch_watchdog_typing() runs (§4 of design doc).
patch_mouse_movement()
patch_watchdog_typing()
patch_scroll_page()

def _system_browser_executable(channel: str | None) -> str | None:
    """Return the path to the system-installed browser for the given channel, or None."""
    system = platform.system()
    key = (channel or "chromium").lower()

    if system == "Darwin":
        candidates: dict[str, list[str]] = {
            "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
            "chrome-beta": ["/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"],
            "chrome-dev": ["/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev"],
            "chrome-canary": ["/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"],
            "msedge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
            "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
        }
    elif system == "Linux":
        candidates = {
            "chrome": ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"],
            "chrome-beta": ["/usr/bin/google-chrome-beta"],
            "chrome-dev": ["/usr/bin/google-chrome-dev"],
            "chrome-canary": [],
            "msedge": ["/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"],
            "chromium": ["/usr/bin/chromium-browser", "/usr/bin/chromium"],
        }
    elif system == "Windows":
        candidates = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ],
            "chrome-beta": [r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe"],
            "chrome-dev": [r"C:\Program Files\Google\Chrome Dev\Application\chrome.exe"],
            "chrome-canary": [str(Path.home() / r"AppData\Local\Google\Chrome SxS\Application\chrome.exe")],
            "msedge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
            "chromium": [r"C:\Program Files\Chromium\Application\chrome.exe"],
        }
    else:
        return None

    for path in candidates.get(key, []):
        if Path(path).exists():
            print(f"Using system browser for channel {channel}: {path}")
            return path

    # Fall back to PATH lookup
    which_map: dict[str, list[str]] = {
        "chrome": ["google-chrome-stable", "google-chrome"],
        "chromium": ["chromium-browser", "chromium"],
        "msedge": ["microsoft-edge-stable", "microsoft-edge"],
    }
    for name in which_map.get(key, []):
        found = shutil.which(name)
        if found:
            return found

    return None


# Shared UA used both in the browser profile and in the stealth JS patches so
# navigator.userAgent, the HTTP header, and the sec-ch-ua hint all agree.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# One Stealth instance is enough — apply_stealth_async is stateless per call.
_STEALTH = Stealth(
    navigator_user_agent_override=_USER_AGENT,
    # Match the platform to our macOS UA so navigator.platform is consistent.
    navigator_platform_override="MacIntel",
    # Expose window.chrome so sites that probe it see a "real" Chrome object.
    chrome_runtime=True,
)


def _build_llm(cfg: "Cfg") -> BaseChatModel:
    provider = cfg.llm_provider
    if provider == "openai":
        from browser_use.llm import ChatOpenAI
        if not cfg.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return ChatOpenAI(model=cfg.openai_model, api_key=cfg.openai_api_key, temperature=0)
    if provider == "google":
        from browser_use.llm import ChatGoogle
        if not cfg.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=google")
        return ChatGoogle(model=cfg.gemini_model, api_key=cfg.gemini_api_key, temperature=0)
    if provider == "deepseek":
        from browser_use.llm import ChatDeepSeek
        if not cfg.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek")
        return ChatDeepSeek(model=cfg.deepseek_model, api_key=cfg.deepseek_api_key, temperature=0)
    if provider == "browseruse":
        from browser_use.llm import ChatBrowserUse
        if not cfg.browseruse_api_key:
            raise ValueError("BROWSERUSE_API_KEY is required when LLM_PROVIDER=browseruse")
        kwargs = dict(api_key=cfg.browseruse_api_key, temperature=0)
        if cfg.browseruse_model:
            kwargs["model"] = cfg.browseruse_model
        return ChatBrowserUse(**kwargs)
    if provider != "anthropic":
        logging.getLogger(__name__).warning(
            "Unknown LLM_PROVIDER %r — falling back to anthropic", provider
        )
    from browser_use.llm import ChatAnthropic
    if not cfg.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
    return ChatAnthropic(model=cfg.anthropic_model, api_key=cfg.anthropic_api_key, temperature=0)


def _detect_action_cycle(sequence: list[str], window: int, threshold: int) -> bool:
    """Return True if the last `window` actions repeat `threshold` times in a row."""
    if len(sequence) < window * threshold:
        return False
    tail = sequence[-window:]
    for i in range(1, threshold):
        prev = sequence[-(window * (i + 1)):-(window * i)]
        if tail != prev:
            return False
    return True


async def run_browser_agent(
    cfg: Cfg,
    task_prompt: str,
    system_prompt_extension: str,
    output_model_schema: Any,
    logger_name: str = "scraper_common.scraper_base",
    trace_file: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Run a browser-use agent with stealth patches and cycle detection.

    Returns (raw_output, stop_reason):
      - raw_output: the agent's final result string, or None on hard failure
      - stop_reason: set when a loop/exception caused early termination
    """
    logger = logging.getLogger(logger_name)
    captcha_solver = CaptchaSolver.from_cfg(cfg)

    llm = _build_llm(cfg)

    effective_trace_file = trace_file or cfg.trace_file or None
    tracer = ModelTracer(effective_trace_file) if effective_trace_file else None

    # Per-run tracking state for cycle detection
    url_visit_counts: Counter[str] = Counter()
    action_sequence: list[str] = []
    stop_reason: list[str] = []  # mutable container so closures can write to it
    stealth_applied_pages: set[int] = set()

    # ── Pre-step hook ─────────────────────────────────────────────────────────

    async def apply_stealth(agent: Agent) -> None:
        # browser_use 0.12.x uses raw CDP instead of Playwright's API layer, so
        # the Page object has no add_init_script().  We call the underlying CDP
        # method directly: Page.addScriptToEvaluateOnNewDocument registers a
        # script that Chrome executes before any JS on every navigation — the
        # same mechanism Playwright's add_init_script() uses internally.
        try:
            page = await agent.browser_session.get_current_page()
            pid = id(page)
            if pid not in stealth_applied_pages and _STEALTH.script_payload:
                session_id = await page._ensure_session()
                await page._client.send.Page.addScriptToEvaluateOnNewDocument(
                    {"source": _STEALTH.script_payload},
                    session_id=session_id,
                )
                stealth_applied_pages.add(pid)
                logger.info("Stealth patches applied to page id=%d via CDP", pid)
        except Exception:
            logger.warning("Could not apply stealth patches", exc_info=True)

    # ── Stop callback ────────────────────────────────────────────────────────
    async def should_stop() -> bool:
        if stop_reason:
            logger.warning("Custom stop triggered: %s", stop_reason[0])
            return True
        return False

    # ── Step callback ─────────────────────────────────────────────────────────
    async def on_new_step(browser_state, agent_output, step_number: int) -> None:
        # URL cycle detection
        url: str = getattr(browser_state, "url", "") or ""
        if url:
            url_visit_counts[url] += 1
            if url_visit_counts[url] > cfg.max_url_visits and not stop_reason:
                stop_reason.append(
                    f"URL visited {url_visit_counts[url]} times with no progress: {url!r}"
                )
                return

        # Action cycle detection
        if agent_output is not None:
            actions = getattr(agent_output, "action", None) or []
            if not isinstance(actions, list):
                actions = [actions]
            for action in actions:
                if action is not None:
                    action_sequence.append(type(action).__name__)

            if (
                not stop_reason
                and _detect_action_cycle(
                    action_sequence,
                    window=cfg.action_repeat_threshold,
                    threshold=2,
                )
            ):
                stop_reason.append(
                    f"Action cycle detected: {action_sequence[-cfg.action_repeat_threshold * 2:]}"
                )

        if tracer is not None and agent_output is not None:
            tracer.record(browser_state, agent_output, step_number)

        logger.debug(
            "Step %d | url=%s | actions=%s",
            step_number,
            url,
            [type(a).__name__ for a in (getattr(agent_output, "action", None) or [])],
        )

    async with contextlib.AsyncExitStack() as stack:
        # ── Browser setup ─────────────────────────────────────────────────────
        # Three paths:
        #   1. camoufox  — Firefox-based stealth; launched via AsyncCamoufox,
        #                  connected to browser_use via its WS endpoint.
        #   2. CDP attach — connect to an already-running browser (any engine).
        #   3. Chrome     — launch a fresh Chromium/Chrome instance ourselves.
        if cfg.browser_type == "camoufox":
            import asyncio
            import base64
            import re
            from functools import partial
            from pathlib import Path
            import orjson
            from camoufox.utils import launch_options as camoufox_launch_options
            from camoufox.server import get_nodejs, to_camel_case_dict
            from camoufox.pkgman import LOCAL_DATA

            logger.info("Launching Camoufox server (headless=%s)", cfg.headless)

            # Build fingerprinted Firefox launch options (synchronous)
            opts = await asyncio.get_event_loop().run_in_executor(
                None, partial(camoufox_launch_options, headless=cfg.headless)
            )

            # camoufox's launchServer.js wraps Playwright's internal Firefox
            # launchServer() and prints the WebSocket endpoint to stdout.
            nodejs = get_nodejs()
            launch_script = str(LOCAL_DATA / "launchServer.js")
            # Strip None values — Playwright's launchServer rejects null for
            # optional fields like proxy that expect an object or no key at all.
            clean_opts = {k: v for k, v in opts.items() if v is not None}
            encoded_opts = base64.b64encode(
                orjson.dumps(to_camel_case_dict(clean_opts))
            ).decode()

            proc = await asyncio.create_subprocess_exec(
                nodejs,
                launch_script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                cwd=str(Path(nodejs).parent / "package"),
            )
            stack.callback(proc.terminate)

            proc.stdin.write(encoded_opts.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            # Read stdout until we find the WebSocket endpoint line.
            ws_url: str | None = None
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
                if not line:
                    break
                # Strip ANSI colour codes; line format:
                #   "Websocket endpoint:\x1b[93m ws://127.0.0.1:PORT/path \x1b[0m"
                text = re.sub(r"\x1b\[[0-9;]*m", "", line.decode()).strip()
                if "Websocket endpoint:" in text:
                    ws_url = text.split("Websocket endpoint:", 1)[-1].strip()
                    break

            if not ws_url:
                raise RuntimeError("Camoufox server did not report a WebSocket endpoint")

            logger.info("Camoufox ready at %s", ws_url)

            # Pass the ws:// URL directly — browser_use skips /json/version
            # when the cdp_url already starts with "ws".
            browser = Browser(
                cdp_url=ws_url,
                keep_alive=True,
                wait_between_actions=cfg.wait_between_actions,
                minimum_wait_page_load_time=cfg.min_page_load_wait,
            )
            owned_browser = False  # camoufox process lifecycle managed by the stack
        elif cfg.browser_cdp_url:
            logger.info("Attaching to running browser at %s", cfg.browser_cdp_url)
            browser = Browser(
                cdp_url=cfg.browser_cdp_url,
                keep_alive=True,
                wait_between_actions=cfg.wait_between_actions,
                minimum_wait_page_load_time=cfg.min_page_load_wait,
            )
            owned_browser = False
        else:
            if cfg.user_data_dir:
                logger.info("Launching browser with user-data dir %s", cfg.user_data_dir)
            browser = Browser(
                headless=cfg.headless,
                channel=cfg.browser_channel,
                executable_path=_system_browser_executable(cfg.browser_channel),
                # args=[
                #     "--disable-blink-features=AutomationControlled",
                #     # Cookies enabled — disable Chrome 136+ Tracking Protection
                #     # (which blocks third-party cookies by default) and storage
                #     # partitioning, so auth flows, embedded widgets and any
                #     # cross-origin cookie handshake work as they did pre-2025.
                #     "--disable-features=TrackingProtection3pcd,ThirdPartyStoragePartitioning",
                # ],
                ignore_default_args=["--enable-automation"],
                user_agent=_USER_AGENT,
                # user_data_dir=cfg.user_data_dir or None,
                wait_between_actions=cfg.wait_between_actions,
                minimum_wait_page_load_time=cfg.min_page_load_wait,
            )
            owned_browser = True

        try:
            agent = Agent(
                task=task_prompt,
                llm=llm,
                browser=browser,
                extend_system_message=system_prompt_extension,
                output_model_schema=output_model_schema,
                loop_detection_enabled=True,
                loop_detection_window=10,
                max_failures=3,
                register_should_stop_callback=should_stop,
                register_new_step_callback=on_new_step,
            )

            async def on_step_start(agent: Agent) -> None:
                await apply_stealth(agent)
                await captcha_solver.handle(agent)

            history = await agent.run(
                max_steps=cfg.max_steps,
                on_step_start=on_step_start,
            )
            raw = history.final_result()
            return raw, (stop_reason[0] if stop_reason else None)

        except Exception as exc:
            logger.exception("Browser agent raised an exception")
            return None, str(exc)
        finally:
            if tracer is not None:
                tracer.close()
            # Only stop browsers we launched ourselves — leave externally-attached
            # ones running for the next call / for the operator to inspect.
            if owned_browser:
                await browser.stop()
