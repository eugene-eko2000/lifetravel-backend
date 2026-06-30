import logging

import uvicorn
from fastapi import FastAPI

from scraper_common.cfg import Cfg
from models import TaskInput, TaskResponse
from scraper import perform_task

logger = logging.getLogger("personal_agent")

app = FastAPI(title="Personal Agent API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/task", response_model=TaskResponse)
async def run_task(request: TaskInput) -> TaskResponse:
    logger.info("Task received: %.100s...", request.task)
    result = await perform_task(request.task)
    logger.info("Task done: success=%s source=%s", result.success, result.source)
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cfg = Cfg.from_env(default_port=8082)
    logger.info("Starting Personal Agent service on port %d", cfg.port)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)
