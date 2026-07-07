"""
Monkey-patch browser_use Agent to trace every LLM input/output to a text file.

Call patch_llm_io_trace() once at startup.  To enable tracing for a specific
Agent instance, set  agent._llm_io_tracer = LlmIoTracer(path)  before calling
agent.run().  The patch is a no-op when _llm_io_tracer is absent or None.

Output format per LLM call:
  ================================================================
  Step N
  ================================================================
  --- INPUT ---
  [0] SystemMessage:
  ...

  --- OUTPUT ---
  Thinking: ...
  ...
  ================================================================
"""
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("scraper_common.patch_llm_io_trace")

_DIVIDER = "=" * 64


def _format_messages(messages: list[Any]) -> str:
    parts: list[str] = []
    for i, msg in enumerate(messages):
        role = type(msg).__name__
        content = msg.content if hasattr(msg, "content") else repr(msg)
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                t = getattr(part, "type", None)
                if t == "text":
                    chunks.append(getattr(part, "text", ""))
                elif t == "image_url":
                    url = getattr(getattr(part, "image_url", None), "url", "")
                    if url.startswith("data:"):
                        media = url.split(";")[0].split(":")[1] if ";" in url else "image"
                        chunks.append(f"<base64 {media} {len(url)} bytes>")
                    else:
                        chunks.append(f"<image_url {url}>")
                else:
                    chunks.append(repr(part))
            text = "".join(chunks)
        else:
            text = repr(content)
        parts.append(f"[{i}] {role}:\n{text}")
    return "\n\n".join(parts)


def _format_output(agent_output: Any) -> str:
    parts: list[str] = []
    cs = getattr(agent_output, "current_state", None)
    if cs is not None:
        for label, attr in (
            ("Thinking", "thinking"),
            ("Evaluation", "evaluation_previous_goal"),
            ("Memory", "memory"),
            ("Next Goal", "next_goal"),
        ):
            val = getattr(cs, attr, None)
            if val:
                parts.append(f"{label}:\n{val}")

    actions = getattr(agent_output, "action", None) or []
    if actions:
        action_lines: list[str] = []
        for i, action in enumerate(actions):
            try:
                dumped = action.model_dump(exclude_none=True, exclude_unset=True)
            except Exception:
                dumped = repr(action)
            action_lines.append(f"  [{i}] {dumped}")
        parts.append("Actions:\n" + "\n".join(action_lines))

    return "\n\n".join(parts) if parts else "<empty>"


class LlmIoTracer:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")
        self._step = 0
        self._file.write(f"\n{_DIVIDER}\nRUN START\n{_DIVIDER}\n")
        self._file.flush()
        logger.info("LLM IO tracer writing to %s", self._path)

    def record(self, input_messages: list[Any], agent_output: Any) -> None:
        self._step += 1
        try:
            input_text = _format_messages(input_messages)
            output_text = _format_output(agent_output)
            self._file.write(
                f"\n{_DIVIDER}\n"
                f"Step {self._step}\n"
                f"{_DIVIDER}\n\n"
                f"--- INPUT ---\n\n"
                f"{input_text}\n\n"
                f"--- OUTPUT ---\n\n"
                f"{output_text}\n\n"
            )
            self._file.flush()
        except Exception:
            logger.warning("LlmIoTracer failed to record step %d", self._step, exc_info=True)

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass


def patch_llm_io_trace() -> None:
    """
    Install the LLM IO trace class-level patch.  Call once at startup.

    Activate per-run by setting agent._llm_io_tracer = LlmIoTracer(path)
    immediately after constructing the Agent and before calling agent.run().
    """
    from browser_use.agent.service import Agent

    _original = Agent.get_model_output

    async def _patched(self, input_messages):
        result = await _original(self, input_messages)
        tracer: LlmIoTracer | None = getattr(self, "_llm_io_tracer", None)
        if tracer is not None:
            tracer.record(input_messages, result)
        return result

    Agent.get_model_output = _patched
