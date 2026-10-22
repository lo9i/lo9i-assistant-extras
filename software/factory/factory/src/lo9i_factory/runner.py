"""The runner (SPEC §3): takes queued tasks and runs their stages until they
wait for the user, pause, or fail.

Two processes run one, both on the same database: the MCP server, and the
web page's process, which lo9i keeps running while lo9i runs. Tasks go on
even when lo9i has stopped the MCP server, or hasn't started it yet after a
restart. They share the queue:

- A runner claims a task before running it, atomically, so only one runs
  it, and only while fewer than max_parallel run, all runners counted.
- It works on several at once (SPEC §1 Concurrency), each in its own
  worktree.
- While it runs a task it says so every few seconds (heartbeat). A running
  task whose runner went quiet (it died; lo9i restarted) goes back in the
  queue, and goes on from its last saved step (SPEC §10).
- The page's runner is a fallback: it takes only tasks it has every
  provider for (it doesn't get lo9i's Copilot sign-in), and leaves the rest
  to the MCP server's, which runs them all and pauses one whose provider has
  no key, saying so.

It reads what to do from the database, so it doesn't matter where a change
came from: the tools and the page wake it, and it looks every few seconds.
It also cleans up after finished tasks (cleanup.py).
"""

import asyncio
import logging
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from . import agents, build, cleanup, db, repo, service, settings, stages
from .errors import FactoryError, NotFoundError
from .models import Task
from .providers import Providers

log = logging.getLogger(__name__)

POLL_SECONDS = 3.0
BEAT_SECONDS = 10.0
# A running task whose runner said nothing for this long is taken to be orphaned.
STALE = timedelta(seconds=60)


class Runner:
    def __init__(
        self,
        providers: Providers | None = None,
        poll: float = POLL_SECONDS,
        make_agent: build.MakeAgent = agents.make,
        *,
        fallback: bool = False,
        name: str | None = None,
    ) -> None:
        """`providers` and `make_agent` replace the real ones in tests. A
        `fallback` runner (the page's) takes only tasks it can fully run."""
        self.providers = providers or Providers()
        self.make_agent = make_agent
        self.poll = poll
        self.fallback = fallback
        self.name = name or f"{socket.gethostname()}:{os.getpid()}:{'page' if fallback else 'mcp'}"
        # The tasks this runner works on now.
        self.active: dict[int, asyncio.Task] = {}
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Something changed: look at the queue now."""
        self._wake.set()

    async def run(self) -> None:
        """Until cancelled; what it was working on goes on next time."""
        beating = asyncio.create_task(self._beat())
        try:
            while True:
                await self.tick()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.poll)
                self._wake.clear()
        finally:
            beating.cancel()
            for work in self.active.values():
                work.cancel()

    async def tick(self) -> None:
        """One look at the queue: requeue orphaned tasks, start what may
        start, clean up after finished ones."""
        conn = db.connect()
        try:
            stale = (datetime.now(UTC) - STALE).isoformat(timespec="seconds")
            with conn:
                orphans = repo.requeue_stale(conn, stale)
            for id in orphans:
                log.info("task %s: its runner went quiet; it goes on from its last saved step", id)
                await cleanup.stop_containers(id)
            while (task := self._claim(conn)) is not None:
                self.active[task.id] = asyncio.create_task(self._run(task.id))
            await cleanup.sweep(conn, busy=set(self.active))
        finally:
            conn.close()

    async def run_once(self) -> bool:
        """Claim the oldest task this runner can run and work on it until it
        stops; False when there's none. For tests, which go a step at a time."""
        conn = db.connect()
        try:
            task = self._claim(conn)
        finally:
            conn.close()
        if task is None:
            return False
        await self._run(task.id)
        return True

    def _claim(self, conn) -> Task | None:
        if not self._can_run(conn):
            return None
        most = settings.load(conn).max_parallel
        for task in reversed(repo.list_tasks(conn, "queued")):
            with conn:
                if repo.claim(conn, task.id, self.name, most):
                    return service.get_task(conn, task.id)
            return None  # Full: nothing more starts now.
        return None

    def _can_run(self, conn) -> bool:
        """The page's runner leaves tasks it hasn't every provider for."""
        if not self.fallback:
            return True
        return all(self.providers.has(model.partition(":")[0]) for model in settings.load(conn).models.values())

    async def _run(self, task_id: int) -> None:
        conn = db.connect()
        try:
            await stages.advance(conn, self.providers, task_id, self.make_agent)
        except FactoryError as e:
            # Stopped (cancelled, paused, deleted), out of money, a model
            # failing, a model without a price: the task is paused with the
            # reason, unless that's already been seen to.
            log.info("task %s stopped: %s", task_id, e)
            with suppress(NotFoundError):
                if service.get_task(conn, task_id).status in ("queued", "running"):
                    service.pause(conn, task_id, str(e))
        except asyncio.CancelledError:
            # This process is stopping: another runner, or this one next time, goes on with it.
            raise
        except Exception as e:
            # A bug: resuming would only run into it again.
            log.exception("task %s failed", task_id)
            with suppress(NotFoundError):
                service.fail(conn, task_id, f"the factory failed: {type(e).__name__}: {e}")
        finally:
            conn.close()
            self.active.pop(task_id, None)

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(BEAT_SECONDS)
            if not self.active:
                continue
            conn = db.connect()
            try:
                with conn:
                    repo.beat(conn, self.name, list(self.active))
            except Exception:
                log.exception("couldn't say the tasks still run")
            finally:
                conn.close()
