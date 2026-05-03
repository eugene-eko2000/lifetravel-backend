"""
reCAPTCHA v2 solver.

Integrates with the agent's on_step_start hook.  When the reCAPTCHA challenge
popup (bframe) is detected, the solver attempts to resolve it before the agent
takes its next step.

Strategy A — audio  (preferred)
  Switches to the audio challenge, downloads the MP3, transcribes it with
  OpenAI Whisper, and submits the text answer.
  Requires OPENAI_API_KEY.

Strategy B — token  (fallback / standalone)
  Extracts the site-key, submits it to a CAPTCHA-as-a-service API (2captcha
  or anti-captcha), and injects the returned token into the hidden response
  textarea + fires the registered callback.
  Requires CAPTCHA_SOLVER_API_KEY (and optionally CAPTCHA_SOLVER_SERVICE).

Strategy none (default)
  Disabled; the agent falls back to system-prompt guidance only.

When method is "audio" and Whisper fails, the solver automatically falls
back to the token service if CAPTCHA_SOLVER_API_KEY is present.

CDP interaction
---------------
Cross-origin reCAPTCHA iframes are out-of-process (OOPIF) in Chrome.  We
use Page.createIsolatedWorld to obtain an execution context inside the frame
and Runtime.evaluate to read/write the DOM from that context, which avoids
same-origin restrictions entirely.
"""

import asyncio
import json
import logging
import os
import random
import tempfile
import time
from typing import Any, Optional

logger = logging.getLogger("flight_scraper.captcha_solver")


# ── Low-level CDP helpers ─────────────────────────────────────────────────────

async def _page_session(page) -> str:
    return await page._ensure_session()


async def _find_frame_id(page, url_fragment: str) -> Optional[str]:
    """Walk the CDP frame tree; return the first frame ID whose URL contains url_fragment."""
    sid = await _page_session(page)
    tree = await page._client.send_raw(
        method="Page.getFrameTree", params={}, session_id=sid
    )

    def _walk(node) -> Optional[str]:
        f = node.get("frame", {})
        if url_fragment in f.get("url", ""):
            return f["id"]
        for child in node.get("childFrames", []):
            r = _walk(child)
            if r:
                return r
        return None

    return _walk(tree.get("frameTree", {}))


async def _isolated_context(page, frame_id: str) -> Optional[int]:
    """Create an isolated world in a frame and return its execution context ID."""
    sid = await _page_session(page)
    res = await page._client.send_raw(
        method="Page.createIsolatedWorld",
        params={"frameId": frame_id, "worldName": "captcha_solver"},
        session_id=sid,
    )
    return res.get("executionContextId")


async def _eval(page, expr: str, context_id: Optional[int] = None) -> Any:
    """Evaluate a JS expression (optionally in a specific frame context)."""
    sid = await _page_session(page)
    params: dict = {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": False,
    }
    if context_id is not None:
        params["contextId"] = context_id
    res = await page._client.send_raw(
        method="Runtime.evaluate", params=params, session_id=sid
    )
    return res.get("result", {}).get("value")


# ── Detection ─────────────────────────────────────────────────────────────────

async def challenge_is_open(page) -> bool:
    """Return True when the reCAPTCHA challenge popup (bframe) is present in the DOM."""
    val = await _eval(
        page,
        "!!(document.querySelector('iframe[src*=\"recaptcha\"][src*=\"bframe\"]'))",
    )
    return bool(val)


async def captcha_is_solved(page) -> bool:
    """Return True when the g-recaptcha-response textarea holds a token."""
    val = await _eval(page, """
        (function(){
            var t = document.getElementById('g-recaptcha-response');
            return !!(t && t.value && t.value.length > 20);
        })()
    """)
    return bool(val)


async def extract_sitekey(page) -> Optional[str]:
    """Extract the reCAPTCHA v2 site key from data-sitekey or the anchor iframe URL."""
    return await _eval(page, """
        (function(){
            var d = document.querySelector('[data-sitekey]');
            if (d) return d.dataset.sitekey;
            var f = document.querySelector('iframe[src*="recaptcha"]');
            if (f) { var m = f.src.match(/[?&]k=([^&]+)/); if (m) return m[1]; }
            return null;
        })()
    """)


# ── Token injection ───────────────────────────────────────────────────────────

async def inject_token(page, token: str) -> None:
    """
    Write the CAPTCHA token into every g-recaptcha-response textarea and fire
    the callbacks registered in ___grecaptcha_cfg as well as any data-callback
    attribute on the .g-recaptcha div.
    """
    tok_js = json.dumps(token)   # safe JS string literal, handles all escaping
    await _eval(page, f"""
        (function(tok){{
            // Populate all response textareas
            var ta = document.getElementById('g-recaptcha-response');
            if (ta) {{
                ta.style.display = '';
                ta.value = tok;
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
            document.querySelectorAll('textarea[name="g-recaptcha-response"]')
                .forEach(function(e){{
                    e.value = tok;
                    e.dispatchEvent(new Event('change', {{bubbles: true}}));
                }});

            // data-callback attribute on .g-recaptcha div
            var div = document.querySelector('.g-recaptcha[data-callback]');
            if (div && typeof window[div.dataset.callback] === 'function') {{
                try {{ window[div.dataset.callback](tok); }} catch(e) {{}}
            }}

            // Walk ___grecaptcha_cfg.clients callback tree
            try {{
                var clients = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
                if (!clients) return;
                function tryFire(o, depth) {{
                    if (!o || depth > 4) return;
                    if (typeof o.callback === 'function') {{
                        try {{ o.callback(tok); }} catch(e) {{}}
                    }}
                    if (typeof o === 'object') {{
                        Object.values(o).forEach(function(v) {{ tryFire(v, depth + 1); }});
                    }}
                }}
                Object.values(clients).forEach(function(c) {{ tryFire(c, 0); }});
            }} catch(e) {{}}
        }})({tok_js})
    """)


# ── 3rd-party token service (2captcha / anti-captcha) ────────────────────────

async def _request_token(
    service: str,
    api_key: str,
    sitekey: str,
    page_url: str,
    timeout_s: int = 120,
) -> Optional[str]:
    """Submit a site-key to a CAPTCHA-as-a-service API; poll until the token arrives."""
    try:
        import httpx
    except ImportError:
        logger.error("httpx is required for the token solver — add it to pyproject.toml")
        return None

    async with httpx.AsyncClient(timeout=30) as http:
        if service == "2captcha":
            r = await http.post(
                "http://2captcha.com/in.php",
                data={
                    "key": api_key,
                    "method": "userrecaptcha",
                    "googlekey": sitekey,
                    "pageurl": page_url,
                    "json": "1",
                },
            )
            data = r.json()
            if data.get("status") != 1:
                logger.warning("2captcha submission error: %s", data)
                return None
            task_id = data["request"]

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                poll = await http.get(
                    "http://2captcha.com/res.php",
                    params={
                        "key": api_key, "action": "get",
                        "id": task_id, "json": "1",
                    },
                )
                pd = poll.json()
                if pd.get("status") == 1:
                    return pd["request"]
                if pd.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                    logger.warning("2captcha poll error: %s", pd)
                    return None

        elif service == "anticaptcha":
            r = await http.post(
                "https://api.anti-captcha.com/createTask",
                json={
                    "clientKey": api_key,
                    "task": {
                        "type": "NoCaptchaTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                },
            )
            data = r.json()
            if data.get("errorId", 1) != 0:
                logger.warning("anti-captcha submission error: %s", data)
                return None
            task_id = data["taskId"]

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                poll = await http.post(
                    "https://api.anti-captcha.com/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
                pd = poll.json()
                if pd.get("status") == "ready":
                    return pd.get("solution", {}).get("gRecaptchaResponse")
                if pd.get("errorId", 0) != 0:
                    logger.warning("anti-captcha poll error: %s", pd)
                    return None

    logger.warning("Token service timed out after %ds", timeout_s)
    return None


# ── Audio challenge solver ────────────────────────────────────────────────────

async def _solve_audio(page, openai_api_key: str) -> bool:
    """
    Switch to the audio challenge, download the MP3, transcribe with Whisper,
    fill in the answer, and submit.  Returns True if the CAPTCHA is solved.

    The audio button, source URL, response field, and verify button all live
    in the cross-origin bframe.  We use an isolated CDP execution context so
    we can read and write the iframe's DOM without same-origin restrictions.
    """
    try:
        import httpx
        from openai import AsyncOpenAI
    except ImportError:
        logger.error("openai and httpx packages are required for the audio solver")
        return False

    bframe_id = await _find_frame_id(page, "bframe")
    if not bframe_id:
        logger.debug("bframe not found — challenge popup not yet open")
        return False

    ctx = await _isolated_context(page, bframe_id)
    if not ctx:
        logger.warning("Could not create isolated world for bframe")
        return False

    # Click the audio-challenge button (may be invisible if already on audio tab)
    await _eval(page, """
        (function(){
            var btn = document.getElementById('recaptcha-audio-button');
            if (btn) btn.click();
        })()
    """, context_id=ctx)
    await asyncio.sleep(random.uniform(1.2, 1.8))

    # Read the audio MP3 URL from the <source id="audio-source"> element
    audio_url: Optional[str] = await _eval(page, """
        (function(){
            var s = document.getElementById('audio-source');
            return s ? s.src : null;
        })()
    """, context_id=ctx)

    if not audio_url:
        logger.warning("Audio source element not found in bframe")
        return False

    logger.debug("reCAPTCHA audio URL acquired")

    # Download the MP3
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            audio_bytes = (await http.get(audio_url)).content
    except Exception as exc:
        logger.warning("Audio download failed: %s", exc)
        return False

    # Transcribe with Whisper
    tmp_path: Optional[str] = None
    try:
        oai = AsyncOpenAI(api_key=openai_api_key)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as audio_file:
            transcription = await oai.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
            )
        answer = transcription.strip().lower()
        logger.debug("Whisper transcription: %r", answer)
    except Exception as exc:
        logger.warning("Whisper transcription failed: %s", exc)
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Fill in the answer
    answer_js = json.dumps(answer)
    await _eval(page, f"""
        (function(ans){{
            var inp = document.getElementById('audio-response');
            if (!inp) return;
            inp.focus();
            inp.value = ans;
            ['input', 'change', 'blur'].forEach(function(ev){{
                inp.dispatchEvent(new Event(ev, {{bubbles: true}}));
            }});
        }})({answer_js})
    """, context_id=ctx)

    await asyncio.sleep(random.uniform(0.4, 0.8))

    # Submit
    await _eval(page, """
        (function(){
            var btn = document.getElementById('recaptcha-verify-button');
            if (btn) btn.click();
        })()
    """, context_id=ctx)

    await asyncio.sleep(2.5)
    return await captcha_is_solved(page)


# ── Public API ────────────────────────────────────────────────────────────────

class CaptchaSolver:
    """
    Checks for an open reCAPTCHA challenge before each agent step and tries
    to resolve it.  Meant to be called from on_step_start.

    Resolution order:
      1. audio + Whisper  (if method == "audio" and openai_api_key is set)
      2. token service    (if solver_api_key is set — acts as fallback for audio
                           or as the primary method when method == "token")
    """

    def __init__(
        self,
        method: str = "none",
        openai_api_key: str = "",
        solver_api_key: str = "",
        solver_service: str = "2captcha",
    ) -> None:
        self.method = method
        self.openai_api_key = openai_api_key
        self.solver_api_key = solver_api_key
        self.solver_service = solver_service

    @classmethod
    def from_cfg(cls, cfg) -> "CaptchaSolver":
        return cls(
            method=cfg.captcha_solver,
            openai_api_key=cfg.openai_api_key,
            solver_api_key=cfg.captcha_solver_api_key,
            solver_service=cfg.captcha_solver_service,
        )

    async def handle(self, agent) -> None:
        """Detect and solve any open reCAPTCHA challenge. No-op when method is 'none'."""
        if self.method == "none":
            return

        try:
            page = await agent.browser_session.get_current_page()
        except Exception:
            return

        try:
            open_challenge = await challenge_is_open(page)
        except Exception:
            return

        if not open_challenge:
            return

        if await captcha_is_solved(page):
            return

        logger.info("reCAPTCHA challenge detected — attempting solve (method=%s)", self.method)

        try:
            # ── Strategy A: audio + Whisper ───────────────────────────────────
            if self.method == "audio" and self.openai_api_key:
                solved = await _solve_audio(page, self.openai_api_key)
                if solved:
                    logger.info("reCAPTCHA solved via audio + Whisper")
                    return
                logger.info("Audio challenge failed — falling back to token service")

            # ── Strategy B: 3rd-party token injection ─────────────────────────
            if not self.solver_api_key:
                logger.warning(
                    "No CAPTCHA_SOLVER_API_KEY configured — cannot solve via token service"
                )
                return

            sitekey = await extract_sitekey(page)
            page_url = await _eval(page, "window.location.href")
            if not sitekey or not page_url:
                logger.warning("Could not extract site-key or page URL")
                return

            token = await _request_token(
                service=self.solver_service,
                api_key=self.solver_api_key,
                sitekey=sitekey,
                page_url=page_url,
            )
            if not token:
                logger.warning("Token service returned no result")
                return

            await inject_token(page, token)
            await asyncio.sleep(1.0)

            if await captcha_is_solved(page):
                logger.info(
                    "reCAPTCHA solved via token injection (%s)", self.solver_service
                )
            else:
                logger.warning(
                    "Token injected but g-recaptcha-response still empty"
                )

        except Exception:
            logger.warning("CaptchaSolver.handle raised an exception", exc_info=True)
