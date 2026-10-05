"""The worker: the process that runs one task, started detached from the MCP server (launch.py) with the
task's folder as its last argument. A plugin's worker module calls `main` with its runner."""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from agent_tasks.tasks import Outcome, Task, TaskStore

# Records one step of what the agent is doing.
Progress = Callable[[str], None]
# Runs the task with the agent and says how it ended. Errors it raises end the task as failed.
Runner = Callable[[Task, Progress], Awaitable[Outcome]]


def main(runner: Runner) -> None:
    folder = Path(sys.argv[-1])
    store = TaskStore(folder.parent)
    _lead_own_group()
    store.write_pid(folder.name, os.getpid())
    if store.is_stopped(folder.name):
        return  # stopped before it had a pid to be signalled with
    store.write_outcome(folder.name, asyncio.run(run(store, store.get(folder.name), runner)))


async def run(store: TaskStore, task: Task, runner: Runner) -> Outcome:
    def progress(line: str) -> None:
        store.append_progress(task.id, line)

    try:
        outcome = await runner(task, progress)
    except Exception as e:
        outcome = Outcome(ok=False, text=f"{type(e).__name__}: {e}")
    return Outcome(outcome.ok, outcome.text, datetime.now(UTC).isoformat(timespec="seconds"), outcome.cost_usd)


def _lead_own_group() -> None:
    """Stopping the task signals this process group, which takes the agent and the commands it started
    with it (launch.stop)."""
    if os.getpgrp() != os.getpid():
        os.setsid()
