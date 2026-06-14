"""
Writes a JSONL trace of every LLM response to a file.

Each line is a JSON object with:
  ts        - ISO-8601 UTC timestamp
  step      - step number (int)
  url       - browser URL at that step
  title     - page title at that step
  thinking  - model's internal monologue (str | null)
  eval      - evaluation of the previous goal (str | null)
  memory    - model's running memory note (str | null)
  next_goal - next goal the model set for itself (str | null)
  actions   - list of {type: str, params: object} dicts

Enable by setting the TRACE_FILE env var to a file path, or by passing
trace_file= to run_browser_agent().
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("scraper_common.model_tracer")


class ModelTracer:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")
        logger.info("Model tracer writing to %s", self._path)

    def record(self, browser_state_summary, agent_output, step: int) -> None:
        try:
            url = getattr(browser_state_summary, "url", "") or ""
            title = getattr(browser_state_summary, "title", "") or ""
            cs = agent_output.current_state

            actions: list[dict] = []
            for action in (agent_output.action or []):
                dumped = action.model_dump(exclude_none=True, exclude_unset=True)
                # Each action model has exactly one non-None field: the action type.
                for action_type, params in dumped.items():
                    actions.append({"type": action_type, "params": params})
                    break  # only one action per slot
                else:
                    # Fallback for truly empty action objects
                    actions.append({"type": type(action).__name__, "params": {}})

            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "step": step,
                "url": url,
                "title": title,
                "thinking": cs.thinking if cs.thinking else None,
                "eval": cs.evaluation_previous_goal if cs.evaluation_previous_goal else None,
                "memory": cs.memory if cs.memory else None,
                "next_goal": cs.next_goal if cs.next_goal else None,
                "actions": actions,
            }
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        except Exception:
            logger.warning("ModelTracer failed to record step %d", step, exc_info=True)

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass
