"""MCP server over the service layer, over stdio. lo9i starts it (see
server.yaml) with FACTORY_DB set:

    FACTORY_DB=/path/factory.db factory-mcp

A runner (runner.py) works on the tasks in the background, in this process
while the server runs (the page's process has one too); the tools that give
it work wake it. The notifier (notify.py) tells the user through lo9i.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import asdict
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, providers, service, settings
from .errors import FactoryError
from .models import Task
from .notify import notifier
from .runner import Runner

INSTRUCTIONS = """\
A software factory: the user gives it a task in a local git repository, a
team of models (architect, implementer, reviewer, tester, security) debates
it, a judge picks the best option, the user approves at checkpoints (the
spec, the plan, the final diff), and a coding agent builds it on a local
branch factory/<id>-<slug>. It never merges or pushes.

- new_task records a task; list_tasks and task_status show where tasks are.
- A task whose status is waiting is at a checkpoint: approve it, or reject
  it with a comment saying what to change, which runs the stage again.
- A task whose status is paused reached a limit (its reason says which):
  after the user raises it with update_settings, resume it.
- comment adds what the user says to a task's debate: every member answers
  it before the debate goes on.
- cancel stops a task; delete_task removes it and its debate for good.
- settings shows the models, limits and agent, and which providers have a
  key; update_settings changes them. A model of a direct API needs a price
  (built in, or prices.<provider:model>) for its cost to count.

A new task debates its spec, then waits at the spec checkpoint; approved,
it debates the plan and waits at the plan checkpoint. task_status shows what
the judge decided there, to read to the user before they approve. With the
plan approved, the coding agent builds it in the task's worktree, its tests
run, the reviewer, security and tester review the diff, and fixes go back to
the agent; then the task waits at the diff checkpoint: task_status shows the
files changed, the tests, open review points and a link to open the
worktree in the editor. Approving it leaves the branch in the repository.

In local run mode the agent asks before each command or network access:
task_status lists them under approvals; read each to the user and answer
it with answer_approval. The agent waits meanwhile.

The factory tells you, in its background work notices, when a task waits
at a checkpoint, when its agent asks to do something, and when a task
pauses, fails or is done. At a checkpoint: read task_status, give the user
the judge's decision in a few lines (for the diff: the files changed, the
tests, open review points), and ask ✅ approve or ❌ reject. On ❌, ask what
to change and pass it to reject as the comment. For an agent's request,
say what it wants to do and ask allow or deny. Paused: say why, and resume
once the user has fixed it.
"""

# Started by main() only: clients in this process (the tests) run the tools
# without one.
_run_tasks = False
_runner: Runner | None = None


@asynccontextmanager
async def _lifespan(server: MCPServer) -> AsyncIterator[None]:
    global _runner
    if not _run_tasks:
        yield
        return
    _runner = Runner()
    workers = [asyncio.create_task(_runner.run()), asyncio.create_task(notifier.watch())]
    try:
        yield
    finally:
        # A debate cut short here goes on from its last saved step next time.
        for worker in workers:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        _runner = None


def _wake() -> None:
    """A task has work for the runner."""
    if _runner is not None:
        _runner.wake()


mcp = MCPServer("factory", instructions=INSTRUCTIONS, lifespan=_lifespan)

READ_ONLY = ToolAnnotations(read_only_hint=True)
# Where each call says which conversation in lo9i it was made in.
ORIGIN = "lo9i/origin"


@contextmanager
def _db(ctx: Context) -> Iterator[sqlite3.Connection]:
    """A connection per call. Service errors reach the model as tool errors
    it can act on. The call's connection to lo9i is where notices go."""
    notifier.attach(ctx.session)
    conn = db.connect()
    try:
        yield conn
    except FactoryError as e:
        raise ToolError(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()


def _origin(ctx: Context) -> dict | None:
    """The conversation in lo9i the call was made in (lo9i/origin)."""
    meta = ctx.request_context.meta or {}
    origin = meta.get(ORIGIN) if isinstance(meta, dict) else getattr(meta, ORIGIN, None)
    return origin if isinstance(origin, dict) else None


def _task(t: Task) -> dict:
    return {**asdict(t), "column": t.column}


def _status(conn: sqlite3.Connection, id: int) -> dict:
    """A task with its stages, the checkpoint it waits at, and what it spent."""
    checkpoint = service.pending_checkpoint(conn, id)
    task = service.get_task(conn, id)
    out = {
        **_task(task),
        "stages": [
            {k: v for k, v in asdict(s).items() if k not in ("task_id", "created_at")} for s in service.stages(conn, id)
        ],
        "checkpoint": _checkpoint(conn, checkpoint) if checkpoint else None,
        # What the agent waits for the user to allow (local mode).
        "approvals": [{"id": a.id, "tool": a.tool, "detail": a.detail} for a in service.approvals(conn, id)],
        "costs": service.costs(conn, id),
    }
    if task.base_commit:
        out["changes"] = [asdict(c) for c in service.changes(conn, id)]
        out["tests"] = [
            {k: v for k, v in asdict(r).items() if k not in ("task_id", "created_at", "output")}
            | ({"output": r.output[-1500:]} if not r.passed else {})
            for r in service.test_runs(conn, id)
        ]
        out["review_points"] = service.review_points(conn, id)
    if task.worktree:
        out["open_in_editor"] = settings.load(conn).editor_url(task.worktree)
    return out


def _checkpoint(conn: sqlite3.Connection, checkpoint) -> dict:
    """A checkpoint with the judge's decision the user approves or rejects."""
    d = service.decision(conn, checkpoint)
    decided = {"rationale": d.rationale, **d.body} if d else None
    return {**asdict(checkpoint), "decision": decided}


# --- Tasks ---


@mcp.tool()
def new_task(
    ctx: Context,
    repo: Annotated[
        str,
        Field(
            description="The local git repository, an absolute path (~ allowed). For a new project, a folder that"
            " doesn't exist yet or is empty: the factory makes it a repository"
        ),
    ],
    description: Annotated[str, Field(description="What to do, as the user put it")],
    title: Annotated[str | None, Field(description="Short; absent takes the description's first line")] = None,
    include_context: Annotated[
        bool, Field(description="Give the team the repo's README, file tree and CLAUDE.md/AGENTS.md")
    ] = False,
    run_mode: Annotated[str | None, Field(description="docker or local for this task; absent follows Settings")] = None,
) -> dict:
    """Start a task in a local repository, at its spec stage."""
    with _db(ctx) as conn:
        t = service.create_task(
            conn, repo, description, title=title, include_context=include_context, run_mode=run_mode
        )
        service.driven_from(conn, t.id, _origin(ctx))
    _wake()
    return _task(t)


@mcp.tool(annotations=READ_ONLY)
def list_tasks(
    ctx: Context,
    status: Annotated[
        str | None, Field(description="queued, running, waiting, paused, done, failed or cancelled")
    ] = None,
) -> list[dict]:
    """The tasks, newest first, optionally filtered by status. waiting ones
    are at a checkpoint for the user."""
    with _db(ctx) as conn:
        return [_task(t) for t in service.list_tasks(conn, status)]


@mcp.tool(annotations=READ_ONLY)
def task_status(ctx: Context, id: int) -> dict:
    """A task in full: its stages, the checkpoint it waits at, and its costs
    (USD for the direct APIs, requests for Copilot)."""
    with _db(ctx) as conn:
        return _status(conn, id)


@mcp.tool()
def approve(
    ctx: Context,
    id: int,
    comment: Annotated[str, Field(description="Optional, kept with the approval")] = "",
) -> dict:
    """Approve the checkpoint a waiting task is at: it goes on to the next
    stage, or is done after the final diff."""
    with _db(ctx) as conn:
        service.driven_from(conn, id, _origin(ctx))
        service.approve(conn, id, comment)
        _wake()
        return _status(conn, id)


@mcp.tool()
def reject(
    ctx: Context,
    id: int,
    comment: Annotated[str, Field(description="What to change; the team reads it when the stage runs again")],
) -> dict:
    """Reject the checkpoint a waiting task is at: the stage runs again with
    the comment. A rejected final diff goes back to the build."""
    with _db(ctx) as conn:
        service.driven_from(conn, id, _origin(ctx))
        service.reject(conn, id, comment)
        _wake()
        return _status(conn, id)


@mcp.tool()
def comment(
    ctx: Context,
    id: int,
    text: Annotated[str, Field(description="What the user says to the team, as they put it")],
) -> dict:
    """Step into a task's debate: the comment goes in its current stage's
    thread, and every member answers it before the debate goes on."""
    with _db(ctx) as conn:
        service.driven_from(conn, id, _origin(ctx))
        message = service.comment(conn, id, text)
    _wake()
    return asdict(message)


@mcp.tool()
def resume(ctx: Context, id: int) -> dict:
    """Put a paused task back in the queue, once it's within its limit again
    (raise max_usd first when that's what it reached)."""
    with _db(ctx) as conn:
        service.driven_from(conn, id, _origin(ctx))
        t = service.resume(conn, id)
    _wake()
    return _task(t)


@mcp.tool()
def answer_approval(
    ctx: Context,
    id: Annotated[int, Field(description="The approval's id, from task_status's approvals")],
    allow: bool,
    reason: Annotated[str, Field(description="Optional; the agent reads it when denied")] = "",
) -> dict:
    """Allow or deny a command, network access or file outside its worktree
    that a task's agent asks for (local run mode). The agent waits for it."""
    with _db(ctx) as conn:
        approval = service.answer_approval(conn, id, allow, reason)
        service.driven_from(conn, approval.task_id, _origin(ctx))
        return asdict(approval)


@mcp.tool()
def cancel(ctx: Context, id: int) -> dict:
    """Stop a task. Its history stays until it's deleted."""
    with _db(ctx) as conn:
        service.driven_from(conn, id, _origin(ctx))
        return _task(service.cancel(conn, id))


@mcp.tool()
def delete_task(ctx: Context, id: int) -> str:
    """Delete a task and its whole debate, for good. Its branch stays in the repository."""
    with _db(ctx) as conn:
        t = service.delete_task(conn, id)
    return f"deleted task {t.id}, {t.title}"


# --- Settings ---


# Named settings for the model; settings_ here, next to the settings module.
@mcp.tool(name="settings", annotations=READ_ONLY)
def settings_(
    ctx: Context,
    prompts: Annotated[bool, Field(description="Include each role's prompt, which is long")] = False,
) -> dict:
    """The models per role, limits, agent, run mode, editor and repositories.
    changed lists what differs from the defaults."""
    with _db(ctx) as conn:
        out = asdict(settings.load(conn))
    if not prompts:
        del out["prompts"]
    # Which providers have a key (or, for Copilot, lo9i's sign-in).
    return {**out, "providers": providers.available()}


@mcp.tool()
def update_settings(
    ctx: Context,
    changes: Annotated[
        dict[str, Any],
        Field(
            description="Setting key to new value; null resets it to its default (or removes a repo's)."
            " Keys: models.<role> (provider:model; providers anthropic, openai, copilot), prompts.<role>,"
            " max_rounds, max_fix_loops, max_usd, max_parallel, agent (claude-code or codex),"
            " run_mode (docker or local), editor (a URL with {path}), full_test_minutes,"
            " repos.<absolute path> ({test_command, base_branch}), prices.<provider:model> (USD per"
            " million tokens: {input, output, cache_read, cache_write}, the cache's optional)."
            " Roles: architect, implementer, reviewer, tester, security, judge."
        ),
    ],
) -> dict:
    """Change settings. All or nothing: one bad value changes none."""
    with _db(ctx) as conn:
        out = asdict(settings.update(conn, changes))
    del out["prompts"]
    return out


def main() -> None:
    global _run_tasks
    # Fail now on a bad database path, not on the first tool call.
    db.connect().close()
    _run_tasks = True
    mcp.run()


if __name__ == "__main__":
    main()
