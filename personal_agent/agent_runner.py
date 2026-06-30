"""
Run with:
    cd personal_agent
    python agent_runner.py --task "Your task here including the target URL" --system-prompt "Your system prompt"
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper_common" / "src"))
sys.path.insert(0, str(Path(__file__).parent / "src"))

from scraper import perform_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the personal agent with a task")
    parser.add_argument(
        "--task",
        required=True,
        help="Task for the agent to perform (include the target URL in the description)",
    )
    parser.add_argument(
        "--system-prompt",
        required=True,
        help="System prompt extension appended to the browser agent's default prompt",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Task:")
    print(args.task)
    print("=" * 60)

    result = await perform_task(args.task, args.system_prompt)

    print("\nResult:")
    print(json.dumps(result.model_dump(), indent=2))
    print("=" * 60)
    print(f"Success:    {result.success}")
    print(f"Source:     {result.source}")
    if result.error:
        print(f"Error:      {result.error}")
    print("=" * 60)

    if not result.success:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
