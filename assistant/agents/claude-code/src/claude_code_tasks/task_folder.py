"""The task folder lo9i gives this worker (lo9i's docs/agent-plugins.md). lo9i writes task.json and
starts the worker with the folder as its last argument; the worker writes its pid, appends each step to
progress.log as a JSON line, and writes result.json last. Stopping the task signals the worker's process
group, so the worker leads its own and everything it starts goes with it. While it works, what the user
sends it comes through inbox/ (Inbox)."""

import asyncio
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Kind = Literal["note", "text", "thinking", "tool", "result", "text_delta", "thinking_delta", "user"]


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
    `text_delta` and `thinking_delta` are the words of a text or thinking as they're written, before the
    whole of it comes as a `text` or `thinking` step. `user` is a message the user sent while it worked,
    when the agent took it (`id` the message's, from the inbox). `id` is a tool call's id, `within` the id
    of the call whose subagent took the step."""

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


@dataclass(frozen=True)
class Message:
    """What the user sent while the task worked: a message (`text`), or `interrupt` to stop the step in
    progress and wait for their next message. `id` names it in the inbox."""

    id: str
    kind: Literal["message", "interrupt"]
    text: str = ""


class Inbox:
    """lo9i writes each message as a JSON file in inbox/, named so they sort in the order they were sent,
    and signals the worker with SIGUSR1. The worker makes the folder once it listens for the signal, which
    tells lo9i it takes messages: before that, the signal would end it."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder / "inbox"
        self._read: set[str] = set()
        self._arrived = asyncio.Event()

    def open(self) -> None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, self._arrived.set)
        self._folder.mkdir(exist_ok=True)

    def close(self) -> None:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR1)

    def take(self) -> list[Message]:
        """The messages not taken yet, oldest first."""
        names = sorted(p.name for p in self._folder.glob("*.json") if p.name not in self._read)
        self._read.update(names)
        return [_message(self._folder / name) for name in names]

    async def next(self) -> list[Message]:
        """Waits for messages and takes them."""
        while True:
            self._arrived.clear()
            if messages := self.take():
                return messages
            await self._arrived.wait()


def _message(path: Path) -> Message:
    data = json.loads(path.read_text())
    kind = "interrupt" if data.get("kind") == "interrupt" else "message"
    return Message(path.stem, kind, str(data.get("text", "")))


# Records one step of what the agent is doing.
Progress = Callable[[Step], None]
# Runs the task with the agent and says how it ended, taking what the user sends meanwhile from the inbox.
# Errors it raises end the task as failed.
Runner = Callable[[Task, Progress, Inbox], Awaitable[Outcome]]


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

    inbox = Inbox(folder)
    inbox.open()
    try:
        outcome = await runner(task, progress, inbox)
    except Exception as e:
        outcome = Outcome(ok=False, text=f"{type(e).__name__}: {e}")
    finally:
        inbox.close()
    return Outcome(outcome.ok, outcome.text, datetime.now(UTC).isoformat(timespec="seconds"), outcome.cost_usd)


def _write_outcome(folder: Path, outcome: Outcome) -> None:
    """Written whole and then renamed, so lo9i never reads half of it."""
    partial = folder / "result.partial"
    partial.write_text(json.dumps(asdict(outcome), indent=2))
    os.replace(partial, folder / "result.json")
