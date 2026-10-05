"""The MCP tools a coding agent plugin offers: start a task, follow it, list them, stop one. Starting and
stopping change the user's code and spend their plan, so they aren't read-only: lo9i asks the user first
unless they trust the plugin."""

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from watchfiles import awatch

from agent_tasks import launch
from agent_tasks.tasks import TaskNotFoundError, TaskState, TaskStore, lost

READ_ONLY = ToolAnnotations(read_only_hint=True)
# The longest a status call waits for a task to finish.
MAX_WAIT_SECONDS = 600
# Results longer than this are cut in tool output; the whole text stays in the task's result.json.
_MAX_RESULT = 20_000
# How often a wait looks again without a file change: it misses no change made just before it began.
_RECHECK_MS = 2_000

# watchfiles logs every change it sees at INFO, which would fill lo9i's log while a task is followed.
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def build(name: str, agent: str, instructions: str, worker: list[str], store: TaskStore) -> MCPServer:
    """`worker`: the command that runs one task, given its folder (worker.py)."""
    server = MCPServer(name, instructions=instructions)
    _add_start(server, agent, worker, store)
    _add_following(server, store)
    _add_stop(server, store)
    return server


def _add_start(server: MCPServer, agent: str, worker: list[str], store: TaskStore) -> None:
    @server.tool(description=_start_description(agent))
    def start_task(
        repository: Annotated[str, Field(description="Absolute path of the repository or folder to work in")],
        task: Annotated[str, Field(description="The whole brief: goal, constraints, how to check it, what to report")],
        follow_up_of: Annotated[
            str, Field(description="An earlier task's id, to continue its session with what it already knows")
        ] = "",
    ) -> str:
        folder = Path(repository).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ToolError(f"{repository} isn't an existing folder. Give an absolute path.")
        session = _session_to_continue(store, follow_up_of) if follow_up_of else ""
        created = store.create(str(folder), task, session, follow_up_of)
        launch.start(worker, store.folder(created.id))
        return f"Started task {created.id} in {folder}. Follow it with task_status (wait_seconds to wait for it)."


def _add_following(server: MCPServer, store: TaskStore) -> None:
    @server.tool(annotations=READ_ONLY)
    async def task_status(
        task_id: str,
        wait_seconds: Annotated[
            int, Field(ge=0, le=MAX_WAIT_SECONDS, description="Wait up to this long for it to finish; 0 answers now")
        ] = 0,
    ) -> str:
        """A task's state: running, done, failed or stopped, its latest steps, and its result once it ends."""
        if wait_seconds:
            await _wait(store, task_id, wait_seconds)
        return render(_state(store, task_id))

    @server.tool(annotations=READ_ONLY)
    def list_tasks() -> str:
        """The latest tasks, newest first, with their state."""
        tasks = store.recent()
        lines = [_summary(store.state(t.id, progress_lines=0)) for t in tasks]
        return "\n".join(lines) or "No tasks yet."


def _add_stop(server: MCPServer, store: TaskStore) -> None:
    @server.tool()
    def stop_task(task_id: str) -> str:
        """Stop a running task, and every command it started. What it already changed stays."""
        state = _state(store, task_id)
        if state.status != "running":
            return f"Task {task_id} isn't running: it's {state.status}."
        store.mark_stopped(task_id)
        if (pid := store.pid(task_id)) is not None:
            launch.stop(pid)
        return f"Stopped task {task_id}. Check the repository for what it changed before it stopped."


def render(state: TaskState) -> str:
    task = state.task
    lines = [_summary(state), f"Repository: {task.repository}", f"Asked: {_short(task.prompt, 500)}"]
    if task.follow_up_of:
        lines.append(f"Follows up on: {task.follow_up_of}")
    if state.progress:
        lines += ["", "Latest steps:", *(f"- {step}" for step in state.progress)]
    lines += ["", *_ending(state)]
    return "\n".join(lines).rstrip()


def _ending(state: TaskState) -> list[str]:
    outcome = state.outcome
    if lost(state):
        return ["Its worker ended without a result: the computer restarted or the process was killed."]
    if outcome is None:
        return [] if state.status == "stopped" else ["Still working."]
    cost = [f"Cost: ${outcome.cost_usd:.2f}"] if outcome.cost_usd is not None else []
    follow = f'To continue this work, call start_task with follow_up_of="{state.task.id}".'
    return ["Result:" if outcome.ok else "Failed:", _short(outcome.text, _MAX_RESULT), *cost, "", follow]


def _summary(state: TaskState) -> str:
    task = state.task
    return f"{task.id}: {state.status}, started {task.started_at}, in {task.repository}: {_short(task.prompt, 80)}"


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " […]"


def _state(store: TaskStore, task_id: str) -> TaskState:
    try:
        return store.state(task_id)
    except TaskNotFoundError as e:
        raise ToolError(f"{e} list_tasks shows the latest ones.") from None


def _session_to_continue(store: TaskStore, task_id: str) -> str:
    state = _state(store, task_id)
    if state.status == "running":
        raise ToolError(f"Task {task_id} is still running. Wait for it or stop it before following up.")
    return state.task.session


async def _wait(store: TaskStore, task_id: str, seconds: int) -> None:
    """Until the task ends or `seconds` pass. Wakes on any change in its folder (the worker writing its
    result, or its progress), and looks again every few seconds for a worker that died without one."""
    if _state(store, task_id).status != "running":
        return
    folder = store.folder(task_id)
    try:
        async with asyncio.timeout(seconds):
            async for _ in awatch(folder, yield_on_timeout=True, rust_timeout=_RECHECK_MS):
                if store.state(task_id, progress_lines=0).status != "running":
                    return
    except TimeoutError:
        return


def _start_description(agent: str) -> str:
    return (
        f"Hand a coding task to {agent}, which works on it on its own in that repository: it reads code, "
        "edits files and runs commands without asking. It runs in the background and survives restarts; "
        "this returns its id at once. It starts with nothing of this conversation, so the task must say "
        "everything: the goal, the files or area, constraints, how to check the work, and what to report."
    )
