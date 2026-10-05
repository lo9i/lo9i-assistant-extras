"""A worker whose agent does what the task says: "finish", "fail", or "sleep" until it's stopped."""

import asyncio

from agent_tasks import Outcome, Progress, Task
from agent_tasks.worker import main


async def run(task: Task, progress: Progress) -> Outcome:
    progress(f"Read: {task.repository}/README.md")
    if task.prompt == "fail":
        raise RuntimeError("the agent gave up")
    if task.prompt == "sleep":
        await asyncio.sleep(60)
    return Outcome(ok=True, text=f"Did it in session {task.session}.", cost_usd=0.25)


if __name__ == "__main__":
    main(run)
