"""Cleaning up after tasks (SPEC §10). History is kept until the user deletes
a task; what a task needed only while it ran goes once it's finished:

- its worktree: the branch factory/<id>-<slug> stays in the repository,
  never merged or pushed, and the diff is read from it after;
- the agent's home folders (local mode), and any container left over by a
  crash (Docker mode: they're removed on exit otherwise).

Deleting a task also removes its logs. The runner sweeps finished tasks
every few seconds, so it doesn't matter how they finished: approved,
cancelled or failed, from chat or the page.
"""

import asyncio
import logging
import shutil
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from . import db, git, repo
from .models import FINISHED, Task
from .sandbox import SandboxError, find

log = logging.getLogger(__name__)

# What a task cancelled a moment ago may still be stopping.
SETTLE = timedelta(seconds=30)


def homes(task_id: int) -> list:
    """The agent's home folders, one per build of the task (sandbox.py)."""
    folder = db.data_dir() / "homes"
    return sorted(folder.glob(f"lo9i-factory-{task_id}-*")) if folder.is_dir() else []


def logs(task_id: int):
    return db.data_dir() / "logs" / f"task-{task_id}"


async def sweep(conn: sqlite3.Connection, busy: set[int]) -> None:
    """Finished tasks lose what they needed only while they ran. `busy` are
    tasks this process still works on, and are left alone."""
    settled = (datetime.now(UTC) - SETTLE).isoformat(timespec="seconds")
    for task in repo.list_tasks(conn):
        if task.status not in FINISHED or task.id in busy or task.updated_at > settled:
            continue
        if task.worktree or homes(task.id):
            await stop_containers(task.id)
            remove_worktree(conn, task)
            for home in homes(task.id):
                shutil.rmtree(home, ignore_errors=True)


def remove_worktree(conn: sqlite3.Connection, task: Task) -> None:
    """The worktree goes; its branch stays."""
    if not task.worktree:
        return
    try:
        git.remove_worktree(task.repo, task.worktree)
    except git.GitError as e:
        # The repo moved or is gone: the folder is the factory's, so it goes anyway.
        log.warning("task %s: git couldn't remove its worktree: %s", task.id, e)
        shutil.rmtree(task.worktree, ignore_errors=True)
    with conn:
        repo.update_task(conn, task.id, {"worktree": None})


def forget(task: Task) -> None:
    """A deleted task's files: its worktree, the agent's homes, its logs. Its
    branch stays in the repository."""
    if task.worktree:
        try:
            git.remove_worktree(task.repo, task.worktree)
        except git.GitError:
            shutil.rmtree(task.worktree, ignore_errors=True)
    with suppress(db.MissingDatabaseError):  # No data folder: an in-memory database.
        for folder in [*homes(task.id), logs(task.id)]:
            shutil.rmtree(folder, ignore_errors=True)


async def stop_containers(task_id: int) -> None:
    """The task's containers, left running when the factory stopped under
    them. Nothing without Docker."""
    try:
        docker = find("docker")
    except SandboxError:
        return
    with suppress(OSError):
        listed = await asyncio.create_subprocess_exec(
            docker,
            "ps",
            "-aq",
            "--filter",
            f"name=^lo9i-factory-{task_id}-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await listed.communicate()
        ids = out.decode().split()
        if ids:
            removed = await asyncio.create_subprocess_exec(
                docker, "rm", "-f", *ids, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await removed.wait()
