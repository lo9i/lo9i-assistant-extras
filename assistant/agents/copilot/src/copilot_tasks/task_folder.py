"""The task folder lo9i gives this worker (lo9i's docs/agent-plugins.md). lo9i writes task.json and
starts the worker with the folder as its last argument; the worker writes its pid, appends each step to
progress.log as a JSON line, and writes result.json last. Stopping the task signals the worker's process
group, so the worker leads its own and everything it starts goes with it."""

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Kind = Literal["note", "text", "thinking", "tool", "result"]


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
class Step:
    """One thing the agent does: a note about the task, its text, its thinking, a tool call (`tool` its
    name, `detail` what it's about) or a tool call's result (`tool` the call it ends, `text` its output).
    `id` is a tool call's id, `within` the id of the call whose subagent took the step."""

    kind: Kind
    text: str = ""
    tool: str = ""
    detail: str = ""
    id: str = ""
    within: str = ""

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v}, ensure_ascii=False)


def note(text: str) -> Step:
    return Step("note", text)


# Records one step of what the agent is doing.
Progress = Callable[[Step], None]
# Runs the task with the agent and says how it ended. Errors it raises end the task as failed.
Runner = Callable[[Task, Progress], Awaitable[Outcome]]


def main(runner: Runner) -> None:
    folder = Path(sys.argv[-1])
    if os.getpgrp() != os.getpid():
        os.setsid()
    (folder / "pid").write_text(str(os.getpid()))
    if (folder / "stopped").exists():
        return  # stopped before it had a pid to be signalled with
    task = Task(**json.loads((folder / "task.json").read_text()))
    _write_outcome(folder, asyncio.run(run(folder, task, runner)))


async def run(folder: Path, task: Task, runner: Runner) -> Outcome:
    def progress(step: Step) -> None:
        with (folder / "progress.log").open("a", encoding="utf-8") as log:
            log.write(step.to_json() + "\n")

    try:
        outcome = await runner(task, progress)
    except Exception as e:
        outcome = Outcome(ok=False, text=f"{type(e).__name__}: {e}")
    return Outcome(outcome.ok, outcome.text, datetime.now(UTC).isoformat(timespec="seconds"), outcome.cost_usd)


def _write_outcome(folder: Path, outcome: Outcome) -> None:
    """Written whole and then renamed, so lo9i never reads half of it."""
    partial = folder / "result.partial"
    partial.write_text(json.dumps(asdict(outcome), indent=2))
    os.replace(partial, folder / "result.json")
