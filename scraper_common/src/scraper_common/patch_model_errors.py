"""
Monkey-patch browser_use Agent to log full stack traces and model inputs on
ModelProviderError / ModelRateLimitError.

Call patch_model_provider_error_logging() once at startup (before any Agent
is instantiated). The patch is applied at the class level so it covers all
Agent instances.
"""
import traceback
from typing import Any


def _format_messages(messages: list[Any]) -> str:
    parts: list[str] = []
    for i, msg in enumerate(messages):
        role = type(msg).__name__
        content = msg.content if hasattr(msg, "content") else repr(msg)
        if isinstance(content, str):
            parts.append(f"[{i}] {role}: {content}")
        elif isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                t = getattr(part, "type", None)
                if t == "text":
                    text_parts.append(getattr(part, "text", ""))
                elif t == "image_url":
                    url = getattr(getattr(part, "image_url", None), "url", "")
                    if url.startswith("data:"):
                        media = url.split(";")[0].split(":")[1] if ";" in url else "image"
                        text_parts.append(f"<base64 {media} {len(url)} bytes>")
                    else:
                        text_parts.append(f"<image_url {url}>")
                else:
                    text_parts.append(repr(part))
            parts.append(f"[{i}] {role}: {''.join(text_parts)}")
        else:
            parts.append(f"[{i}] {role}: {repr(content)}")
    return "\n".join(parts)


def patch_model_provider_error_logging() -> None:
    from browser_use.agent.service import Agent
    from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError

    _original_get_model_output = Agent.get_model_output
    _original_try_switch = Agent._try_switch_to_fallback_llm

    async def _patched_get_model_output(self, input_messages):
        self._debug_last_input_messages = input_messages
        return await _original_get_model_output(self, input_messages)

    def _patched_try_switch(self, error: ModelRateLimitError | ModelProviderError) -> bool:
        tb = traceback.format_exc()
        input_messages = getattr(self, "_debug_last_input_messages", None)
        msg_dump = _format_messages(input_messages) if input_messages is not None else "<unavailable>"
        status = getattr(error, "status_code", "N/A")
        model = getattr(error, "model", "unknown")
        print(
            f"\n{'='*80}\n"
            f"ModelProviderError: {type(error).__name__} | status={status} | model={model}\n"
            f"{error}\n"
            f"\n--- Stack trace ---\n{tb}"
            f"--- Model input ({len(input_messages) if input_messages else 0} messages) ---\n"
            f"{msg_dump}\n"
            f"{'='*80}\n",
            flush=True,
        )
        return _original_try_switch(self, error)

    Agent.get_model_output = _patched_get_model_output
    Agent._try_switch_to_fallback_llm = _patched_try_switch
