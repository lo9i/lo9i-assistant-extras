"""The `converse` tool: lo9i calls it for a conversation the user handed to this agent (/claude,
the `agent:` section of server.yaml). Each message runs as a task that continues the conversation's last
one in that folder, so the agent remembers what was said, and the tool answers with the reply once it
ends. Stopping the message in lo9i cancels the call, which stops the task.

Which task a conversation is at is kept in a small file per conversation and folder, next to the tasks.
"""

import contextlib
import hashlib
import json
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agent_tasks import launch
from agent_tasks.tasks import Task, TaskNotFoundError, TaskState, TaskStore, lost
from agent_tasks.waiting import until_ended


class Conversations:
    """The last task of each conversation in each folder."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def last(self, conversation: str, folder: str, store: TaskStore) -> Task | None:
        path = self._path(conversation, folder)
        if not path.is_file():
            return None
        with contextlib.suppress(TaskNotFoundError):
            return store.get(json.loads(path.read_text())["task"])
        return None

    def record(self, conversation: str, folder: str, task: Task) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._path(conversation, folder).write_text(json.dumps({"task": task.id}))

    def _path(self, conversation: str, folder: str) -> Path:
        key = hashlib.sha256(f"{conversation}\n{folder}".encode()).hexdigest()[:24]
        return self._root / f"{key}.json"


def add_converse(server: MCPServer, worker: list[str], store: TaskStore, conversations: Conversations) -> None:
    @server.tool()
    async def converse(conversation: str, folder: str, message: str, ctx: Context) -> str:
        """For lo9i only, when the user hands it a conversation: answer `message` in `folder`, continuing
        what was said in this conversation there. Returns the reply once it's done; each step on the way
        (a file read, a command run) is sent as a progress notification, so lo9i shows it as it happens."""
        task = _start(conversation, folder, message, worker, store, conversations)
        try:
            state = await until_ended(store, task.id, _Steps(ctx))
        except BaseException:
            _stop(store, task)
            raise
        return _reply(state)


class _Steps:
    """Sends each progress line to the caller, numbered so the progress keeps going up."""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._sent = 0

    async def __call__(self, line: str) -> None:
        self._sent += 1
        await self._ctx.report_progress(self._sent, message=line)


def _start(
    conversation: str, folder: str, message: str, worker: list[str], store: TaskStore, conversations: Conversations
) -> Task:
    where = Path(folder).expanduser()
    if not where.is_absolute() or not where.is_dir():
        raise ToolError(f"{folder} isn't an existing folder.")
    previous = conversations.last(conversation, str(where), store)
    if previous is not None and store.state(previous.id, progress_lines=0).status == "running":
        raise ToolError("Still working on the previous message. Wait for it, or stop it first.")
    task = store.create(str(where), message, previous.session if previous else "", previous.id if previous else "")
    launch.start(worker, store.folder(task.id))
    conversations.record(conversation, str(where), task)
    return task


def _stop(store: TaskStore, task: Task) -> None:
    """The call was cancelled (the user stopped it in lo9i): the agent stops too."""
    store.mark_stopped(task.id)
    if (pid := store.pid(task.id)) is not None:
        launch.stop(pid)


def _reply(state: TaskState) -> str:
    if state.outcome is not None and state.outcome.ok:
        return state.outcome.text
    if lost(state):
        raise ToolError("The agent's worker ended without a reply: the computer restarted or it was killed.")
    raise ToolError(state.outcome.text if state.outcome else f"The task was {state.status}.")
