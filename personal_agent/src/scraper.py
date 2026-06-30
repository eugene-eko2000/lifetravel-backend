import json
import logging
from datetime import datetime, timezone

from scraper_common.cfg import Cfg
from scraper_common.scraper_base import run_browser_agent

from models import AgentOutput, TaskResponse

logger = logging.getLogger("personal_agent.scraper")


def _parse_agent_output(raw: str | None) -> AgentOutput:
    if not raw:
        return AgentOutput(
            success=False,
            error="Agent produced no output",
            source="unknown",
        )
    try:
        data = json.loads(raw)
        return AgentOutput.model_validate(data)
    except Exception as exc:
        logger.warning("Could not parse agent output as AgentOutput: %s — raw: %.200s", exc, raw)
        return AgentOutput(
            success=False,
            error=f"Output parse error: {exc}",
            source="unknown",
        )


async def perform_task(user_prompt: str, system_prompt: str) -> TaskResponse:
    cfg = Cfg.from_env(default_port=8082)
    completed_at = datetime.now(timezone.utc).isoformat()

    raw, stop_reason = await run_browser_agent(
        cfg=cfg,
        task_prompt=user_prompt,
        system_prompt_extension=system_prompt,
        output_model_schema=AgentOutput,
        logger_name="personal_agent.scraper",
        trace_file=cfg.trace_file or None,
    )

    if raw is None:
        output = AgentOutput(
            success=False,
            error=stop_reason or "Agent produced no output",
            source="unknown",
        )
    else:
        output = _parse_agent_output(raw)
        if stop_reason:
            if output.success:
                output = AgentOutput(
                    success=False,
                    error=stop_reason,
                    result=output.result,
                    source=output.source,
                )
            elif not output.error:
                output = AgentOutput(
                    success=output.success,
                    error=stop_reason,
                    result=output.result,
                    source=output.source,
                )

    return TaskResponse(
        success=output.success,
        error=output.error,
        result=output.result,
        source=output.source,
        completed_at=completed_at,
    )
