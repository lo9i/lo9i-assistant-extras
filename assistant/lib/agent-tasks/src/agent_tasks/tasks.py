"""The tasks' records, one folder per task:

- task.json: what was asked. The MCP server writes it once, when the task starts.
- pid: the worker's process id. The worker writes it when it starts.
- progress.log: what the agent is doing, one line per step. The worker appends to it.
- result.json: how it ended. The worker writes it last.
- stopped: the user stopped it. The MCP server writes it.

Every file has one writer, so the server and the worker never race on one. A task's state comes from
which of them exist and whether its worker is still alive.
"""

import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Status = Literal["running", "done", "failed", "stopped"]

_TASK = "task.json"
_PID = "pid"
_PROGRESS = "progress.log"
_RESULT = "result.json"
_STOPPED = "stopped"
# How long a task may go without its worker's pid before it counts as a worker that never started.
_START_SECONDS = 60


class TaskNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    repository: str
    prompt: str
    # The agent's session: a follow-up continues its earlier task's session.
    session: str
    # The task this one follows up on; "" for a new session.
    follow_up_of: str
    started_at: str


@dataclass(frozen=True)
class Outcome:
    ok: bool
    # The agent's final answer, or what went wrong.
    text: str
    finished_at: str = ""
    cost_usd: float | None = None


@dataclass(frozen=True)
class TaskState:
    task: Task
    status: Status
    # Set once it's done or failed.
    outcome: Outcome | None
    # The last lines of progress.log.
    progress: list[str]


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, repository: str, prompt: str, session: str = "", follow_up_of: str = "") -> Task:
        task = Task(
            id=uuid.uuid4().hex[:8],
            repository=repository,
            prompt=prompt,
            session=session or str(uuid.uuid4()),
            follow_up_of=follow_up_of,
            started_at=_now(),
        )
        folder = self.folder(task.id)
        folder.mkdir(parents=True)
        _write_json(folder / _TASK, asdict(task))
        return task

    def folder(self, task_id: str) -> Path:
        return self.root / task_id

    def get(self, task_id: str) -> Task:
        path = self.folder(task_id) / _TASK
        if not task_id.isalnum() or not path.is_file():
            raise TaskNotFoundError(f"No task {task_id}.")
        return Task(**json.loads(path.read_text()))

    def recent(self, limit: int = 20) -> list[Task]:
        """Newest first, by when their record was written: `started_at` is only to the second."""
        records = sorted(self.root.glob(f"*/{_TASK}"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        return [self.get(record.parent.name) for record in records[:limit]]

    def state(self, task_id: str, progress_lines: int = 30) -> TaskState:
        task = self.get(task_id)
        folder = self.folder(task_id)
        outcome = _read_outcome(folder / _RESULT)
        progress = _tail(folder / _PROGRESS, progress_lines)
        return TaskState(task, self._status(task, outcome), outcome, progress)

    def pid(self, task_id: str) -> int | None:
        path = self.folder(task_id) / _PID
        return int(path.read_text()) if path.is_file() else None

    def mark_stopped(self, task_id: str) -> None:
        (self.folder(task_id) / _STOPPED).write_text(_now())

    def is_stopped(self, task_id: str) -> bool:
        return (self.folder(task_id) / _STOPPED).exists()

    def write_pid(self, task_id: str, pid: int) -> None:
        (self.folder(task_id) / _PID).write_text(str(pid))

    def append_progress(self, task_id: str, line: str) -> None:
        with (self.folder(task_id) / _PROGRESS).open("a", encoding="utf-8") as log:
            log.write(" ".join(line.split()) + "\n")

    def write_outcome(self, task_id: str, outcome: Outcome) -> None:
        _write_json(self.folder(task_id) / _RESULT, asdict(outcome))

    def _status(self, task: Task, outcome: Outcome | None) -> Status:
        if self.is_stopped(task.id):
            return "stopped"
        if outcome is not None:
            return "done" if outcome.ok else "failed"
        return "running" if self._alive(task) else "failed"

    def _alive(self, task: Task) -> bool:
        """Its worker is running: the process with its pid is the one started for this task's folder,
        not another that got the same pid after a restart. Without a pid yet, it's starting, for a while."""
        folder = self.folder(task.id)
        pid_file = folder / _PID
        if not pid_file.is_file():
            age = datetime.now(UTC) - datetime.fromisoformat(task.started_at)
            return age.total_seconds() < _START_SECONDS
        return str(folder) in _command_of(int(pid_file.read_text()))


def lost(state: TaskState) -> bool:
    """It failed without a result: its worker was killed (a restart, out of memory) before it finished."""
    return state.status == "failed" and state.outcome is None


def _command_of(pid: int) -> str:
    found = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return found.stdout


def _read_outcome(path: Path) -> Outcome | None:
    return Outcome(**json.loads(path.read_text())) if path.is_file() else None


def _tail(path: Path, lines: int) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]


def _write_json(path: Path, data: dict) -> None:
    """Written whole and then renamed, so a reader never sees half of it."""
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(data, indent=2))
    os.replace(partial, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
