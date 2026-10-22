"""Telling the user through lo9i (SPEC §1 Notifications, §3, §8): at each
checkpoint, when the agent asks to do something (local mode), and when a
task pauses, fails or ends. lo9i shows it in its notifications and as a
toast, and sends it to the chat the task was driven from (Telegram, say),
where the user answers: approve, or reject with a comment.

lo9i's background work (its docs/background-work.md) carries it. Each
unfinished task is one piece of work, task-<id>, started (work_started) as
long as the task isn't finished, so lo9i doesn't stop this server, and
with it the runner, as idle while a task waits for the user. A notice
finishes that work (work_finished, with the text and the conversation the
task was last driven from) and starts it again if the task goes on.
follow_up has the assistant bring it up at once, to ask the user.

The notifier watches the database every few seconds, so it doesn't matter
what changed a task: the runner, the page, or chat. What it told is kept
(the notices table), so a restart doesn't tell it again. It can only talk
to lo9i once a call came in on the connection (the session comes with it);
what it would tell before waits for that.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from . import db, repo, service
from .models import Task

log = logging.getLogger(__name__)

STARTED = "notifications/lo9i/work_started"
FINISHED_WORK = "notifications/lo9i/work_finished"
WATCH_SECONDS = 2.0
# lo9i shows the user at most 300 characters of a notice.
_SHORT = 280
CHECKPOINTS = {"spec": "the spec", "plan": "the plan", "diff": "the final diff"}
UNFINISHED = ("queued", "running", "waiting", "paused")


class _Notification(BaseModel):
    """A notification of lo9i's own. The session sends any model with a
    method and params (it dumps it), though its type names MCP's."""

    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Notice:
    # Told once per key, like checkpoint:3.
    key: str
    text: str
    ok: bool = True
    # The assistant brings it up unasked: a decision for the user.
    follow_up: bool = False


def work(task_id: int) -> str:
    return f"task-{task_id}"


def short(text: str, limit: int = _SHORT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def due(conn, task: Task) -> list[Notice]:
    """What the user should hear of a task now; those already told are
    dropped by the caller."""
    name = f"Task {task.id}, {short(task.title, 60)}"
    out = []
    if task.status == "waiting" and (c := repo.pending_checkpoint(conn, task.id)):
        decided = service.decision(conn, c)
        why = f" The judge: {short(decided.rationale, 120)}" if decided else ""
        extra = _diff(conn, task) if c.kind == "diff" else ""
        out.append(
            Notice(
                f"checkpoint:{c.id}",
                f"{name}: {CHECKPOINTS[c.kind]} waits for you.{extra}{why} Approve it, or reject it saying what to"
                " change.",
                follow_up=True,
            )
        )
    for a in repo.pending_approvals(conn, task.id):
        out.append(
            Notice(
                f"approval:{a.id}",
                f"{name}: the agent asks to use {a.tool}: {short(a.detail, 120)} Allow it or deny it?",
                follow_up=True,
            )
        )
    if task.status == "paused":
        out.append(Notice(f"paused:{task.paused_at}", f"{name} is paused: {short(task.reason, 160)}", ok=False))
    elif task.status == "failed":
        out.append(Notice("failed", f"{name} failed: {short(task.reason, 160)}", ok=False))
    elif task.status == "done":
        out.append(Notice("done", f"{name} is done: its branch {task.branch} is in {task.repo}."))
    elif task.status == "cancelled":
        out.append(Notice("cancelled", f"{name} was cancelled."))
    return out


def _diff(conn, task: Task) -> str:
    changed = service.changes(conn, task.id)
    added = sum(c.added or 0 for c in changed)
    deleted = sum(c.deleted or 0 for c in changed)
    latest = {r.kind: r for r in service.test_runs(conn, task.id)}.values()
    tests = "" if not latest else " Tests pass." if all(r.passed for r in latest) else " Some tests fail."
    return f" {len(changed)} files changed, +{added} -{deleted}.{tests}"


class Notifier:
    def __init__(self) -> None:
        self.session: Any = None
        # The tasks whose work is started, with where to tell their end.
        self._open: dict[int, dict | None] = {}

    def attach(self, session: Any) -> None:
        """The connection a call came in on, to talk to lo9i over. A new one
        (lo9i reconnected) knows of no work yet: it's all started again."""
        if session is not self.session:
            self.session = session
            self._open.clear()

    async def watch(self) -> None:
        """Until cancelled."""
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("couldn't tell the user about the tasks")
            await asyncio.sleep(WATCH_SECONDS)

    async def sync(self) -> None:
        """Tell what's due, and hold work for the unfinished tasks."""
        if self.session is None:
            return
        conn = db.connect()
        try:
            tasks = {t.id: t for t in repo.list_tasks(conn)}
            for id in sorted(tasks):
                await self._task(conn, tasks[id])
            for id in [i for i in self._open if i not in tasks]:
                # Deleted: its work ends, quietly.
                origin = self._open.pop(id)
                if origin:
                    await self._finished(id, origin, f"Task {id} was deleted.", ok=True, follow_up=False)
        finally:
            conn.close()

    async def _task(self, conn, task: Task) -> None:
        told = repo.told(conn, task.id)
        origin = task.origin or repo.latest_origin(conn)
        for notice in due(conn, task):
            if notice.key in told:
                continue
            if origin is None:
                # Nowhere to tell it: it shows on the page only.
                log.info("task %s: nowhere to tell %s", task.id, notice.key)
            else:
                # Telling finishes the task's work; it's held again below if the task goes on.
                await self._finished(task.id, origin, notice.text, notice.ok, notice.follow_up)
                self._open.pop(task.id, None)
            with conn:
                repo.tell(conn, task.id, notice.key)
        if task.status in UNFINISHED and origin is not None:
            # Held only where its end can be told, so it can't keep the server up for good.
            if task.id not in self._open:
                await self._send(STARTED, {"work": work(task.id)})
            self._open[task.id] = origin
        else:
            self._open.pop(task.id, None)

    async def _finished(self, task_id: int, origin: dict, text: str, ok: bool, follow_up: bool) -> None:
        params = {"work": work(task_id), "origin": origin, "ok": ok, "text": text, "follow_up": follow_up}
        await self._send(FINISHED_WORK, params)

    async def _send(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self.session.send_notification(_Notification(method=method, params=params))
        except Exception:
            # The connection went: what's left waits for the next one.
            log.warning("lo9i's connection is gone; telling waits for the next call")
            self.session = None
            self._open.clear()
            raise


notifier = Notifier()
