"""Waiting for a task to end: on any change in its folder (the worker writing its result or progress),
and again every few seconds for a worker that died without writing one."""

import asyncio
import logging

from watchfiles import awatch

from agent_tasks.tasks import TaskState, TaskStore

# How often a wait looks again without a file change: it misses no change made just before it began.
_RECHECK_MS = 2_000

# watchfiles logs every change it sees at INFO, which would fill lo9i's log while a task is followed.
logging.getLogger("watchfiles").setLevel(logging.WARNING)


async def until_ended(store: TaskStore, task_id: str) -> TaskState:
    """The task's state once it's no longer running."""
    if (state := store.state(task_id)).status != "running":
        return state
    async for _ in awatch(store.folder(task_id), yield_on_timeout=True, rust_timeout=_RECHECK_MS):
        if (state := store.state(task_id)).status != "running":
            return state
    return store.state(task_id)


async def at_most(store: TaskStore, task_id: str, seconds: int) -> None:
    """Until the task ends or `seconds` pass."""
    try:
        async with asyncio.timeout(seconds):
            await until_ended(store, task_id)
    except TimeoutError:
        return
