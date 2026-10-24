"""The Factory page. lo9i starts it (see server.yaml) and shows it in its web
app:

    FACTORY_DB=/path/factory.db factory-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has
no login: the daemon lets only the user's paired devices through. The daemon
serves it under a path (/factory/) and says which in X-Forwarded-Prefix;
pages link relative to it (<base> in base.html).

The board (SPEC §8) has a column per stage plus what waits for the user;
cards move only by approve and reject, never by dragging. A task's page
shows its progress across the stages and, in a sentence, what it's doing
now; the judge's decision at a checkpoint with the buttons to approve or
reject it; each stage's outcome in short, its debate as a chat behind a
toggle; the diff and the tests, and what it cost.
Settings has the models, limits, prompts and repositories.

Live: lo9i's daemon hands a page's answers on whole, so the page can't
stream (SSE). A task that's running has its live part read again every few
seconds instead (htmx polling); the forms the user types in sit outside it.
Changes go straight to the database, where the runners (this process's,
and the MCP server's) find them at once or within seconds.
"""

import argparse
import asyncio
import contextlib
import hashlib
import os
import re
import socket
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from .. import db, prices, providers, service, settings
from ..errors import ConflictError, FactoryError, NotFoundError, ValidationError
from ..models import AGENTS, COLUMNS, PROVIDERS, ROLES, RUN_MODES
from ..runner import Runner
from . import views

HERE = Path(__file__).parent
# What the daemon may send as X-Forwarded-Prefix: path segments, like /factory.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")
# How often a running task's page is read again, in seconds.
LIVE_SECONDS = 3
# Settings the form sends as numbers: whole, or not.
WHOLE = ("max_rounds", "max_fix_loops", "max_parallel")
DECIMAL = ("max_usd", "full_test_minutes")
STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "waiting": "Waiting for you",
    "paused": "Paused",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
STAGE_STATUS_LABELS = {
    "pending": "Not started",
    "running": "Running",
    "waiting": "Waiting for you",
    "approved": "Approved",
    "rejected": "Rejected",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
# Under each step of a task's progress (views.steps).
STEP_NOTES = {
    "done": "Done",
    "running": "Working on it",
    "queued": "Starting",
    "waiting": "Your call",
    "paused": "Paused",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "upcoming": "",
}
# Beside each point of a debate (views.points).
POINT_LABELS = {"open": "open", "answered": "answered", "revised": "revised since"}

# Models write Markdown. Raw HTML in it is shown as text, never run.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it
    again after a deploy changes it instead of using its cached copy."""
    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


def markdown(text: str | None) -> Markup:
    return Markup(_markdown.render(text or ""))


def ago(stamp: str | None) -> str:
    if not stamp:
        return ""
    seconds = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{int(seconds // size)} {unit} ago"
    return "just now"


def usd(amount: float) -> str:
    return f"${amount:.2f}" if amount >= 0.01 or amount == 0 else "<$0.01"


def repo_name(path: str) -> str:
    return Path(path).name


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals |= {
    "asset": asset_url,
    "COLUMNS": COLUMNS,
    "ROLES": ROLES,
    "AGENTS": AGENTS,
    "RUN_MODES": RUN_MODES,
    "STATUS_LABELS": STATUS_LABELS,
    "STAGE_STATUS_LABELS": STAGE_STATUS_LABELS,
    "CHECKPOINT_LABELS": views.CHECKPOINT_LABELS,
    "STEP_NOTES": STEP_NOTES,
    "POINT_LABELS": POINT_LABELS,
    "LIVE_SECONDS": LIVE_SECONDS,
}
templates.env.filters |= {
    "markdown": markdown,
    "ago": ago,
    "usd": usd,
    "repo_name": repo_name,
    "one_line": views.one_line,
}


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _text(form: dict, key: str) -> str:
    return str(form.get(key) or "").strip()


def _render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def _form_error(request: Request, template: str, target: str, **ctx: Any) -> HTMLResponse:
    """Re-render a failed form, with its error, in place of the form."""
    resp = _render(request, template, **ctx)
    resp.headers["HX-Retarget"] = target
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


def _refresh() -> Response:
    """Read the page again: the change shows in more than one place on it."""
    return Response(headers={"HX-Refresh": "true"})


def _go(request: Request, path: str) -> Response:
    """Open another page of this app."""
    return Response(headers={"HX-Redirect": f"{request.scope.get('root_path', '')}/{path}"})


# --- The board ---


def _board_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def board(request: Request, conn: Conn) -> HTMLResponse:
        s = settings.load(conn)
        return _render(
            request, "board.html", nav="board", columns=views.board(conn), run_mode=s.run_mode, repos=list(s.repos)
        )

    @app.post("/tasks")
    async def new_task(request: Request, conn: Conn) -> Response:
        form = dict(await request.form())
        try:
            t = service.create_task(
                conn,
                _text(form, "repo"),
                _text(form, "description"),
                title=_text(form, "title") or None,
                include_context=bool(form.get("include_context")),
                run_mode=_text(form, "run_mode") or None,
            )
        except FactoryError as e:
            return _form_error(request, "_new_task.html", "#new-task-form", form=form, error=str(e))
        return _go(request, f"tasks/{t.id}")


# --- A task ---


def _task_page(request: Request, conn: sqlite3.Connection, id: int, template: str) -> HTMLResponse:
    task = service.get_task(conn, id)
    s = settings.load(conn)
    stages = views.stages(conn, task)
    checkpoint = service.pending_checkpoint(conn, id)
    info = views.overview(conn, task, s.editor_url(task.worktree) if task.worktree else None)
    open_points = len([p for p in info.points if p["status"] == "open"])
    return _render(
        request,
        template,
        nav="board",
        task=task,
        stages=stages,
        checkpoint=checkpoint,
        info=info,
        steps=views.steps(task, stages, checkpoint, len(info.approvals)),
        now=views.now(task, stages, checkpoint, info.approvals, len(info.changes), open_points),
        live=task.status in ("queued", "running"),
    )


def _task_routes(app: FastAPI) -> None:
    @app.get("/tasks/{id}", response_class=HTMLResponse)
    def task_page(request: Request, id: int, conn: Conn) -> HTMLResponse:
        return _task_page(request, conn, id, "task.html")

    @app.get("/tasks/{id}/live", response_class=HTMLResponse)
    def live(request: Request, id: int, conn: Conn) -> Response:
        """The part of a running task's page that changes. Once it stops,
        the whole page is read again: its buttons change with it."""
        if service.get_task(conn, id).status not in ("queued", "running"):
            return _refresh()
        return _task_page(request, conn, id, "_live.html")

    @app.post("/tasks/{id}/approve")
    async def approve(request: Request, id: int, conn: Conn) -> Response:
        form = dict(await request.form())
        service.approve(conn, id, _text(form, "comment"))
        return _refresh()

    @app.post("/tasks/{id}/reject")
    async def reject(request: Request, id: int, conn: Conn) -> Response:
        form = dict(await request.form())
        service.reject(conn, id, _text(form, "comment"))
        return _refresh()

    @app.post("/tasks/{id}/comment")
    async def comment(request: Request, id: int, conn: Conn) -> Response:
        form = dict(await request.form())
        service.comment(conn, id, _text(form, "text"))
        return _refresh()

    @app.post("/tasks/{id}/approvals/{approval}")
    async def answer(request: Request, id: int, approval: int, conn: Conn) -> Response:
        form = dict(await request.form())
        service.answer_approval(conn, approval, _text(form, "allow") == "yes", _text(form, "reason"))
        return _refresh()

    @app.post("/tasks/{id}/resume")
    def resume(id: int, conn: Conn) -> Response:
        service.resume(conn, id)
        return _refresh()

    @app.post("/tasks/{id}/cancel")
    def cancel(id: int, conn: Conn) -> Response:
        service.cancel(conn, id)
        return _refresh()

    @app.post("/tasks/{id}/delete")
    def delete(request: Request, id: int, conn: Conn) -> Response:
        service.delete_task(conn, id)
        return _go(request, "")


# --- Settings ---


def _settings_page(request: Request, conn: sqlite3.Connection) -> HTMLResponse:
    s = settings.load(conn)
    return _render(
        request,
        "settings.html",
        nav="settings",
        s=s,
        defaults=settings.defaults(),
        # Suggested in the model fields: the priced ones and those in use.
        known_models=sorted({*prices.PRICES, *s.prices, *s.models.values()}),
        providers=providers.available(),
        provider_names=PROVIDERS,
    )


def _settings_routes(app: FastAPI) -> None:
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn: Conn) -> HTMLResponse:
        return _settings_page(request, conn)

    @app.post("/settings")
    async def save(request: Request, conn: Conn) -> Response:
        """The models, agent, limits, run mode and editor, as one form."""
        form = dict(await request.form())
        changes: dict[str, Any] = {f"models.{role}": _text(form, f"models.{role}") for role in ROLES}
        changes |= {key: _text(form, key) for key in ("agent", "run_mode", "editor")}
        changes |= {key: _number(form, key, int) for key in WHOLE}
        changes |= {key: _number(form, key, float) for key in DECIMAL}
        settings.update(conn, changes)
        return _refresh()

    @app.post("/settings/prompts/{role}")
    async def prompt(request: Request, role: str, conn: Conn) -> Response:
        """Save a role's prompt, or (reset) go back to the default."""
        form = dict(await request.form())
        text = None if form.get("reset") else _text(form, "prompt")
        settings.update(conn, {f"prompts.{role}": text})
        return _refresh()

    @app.post("/settings/repos")
    async def save_repo(request: Request, conn: Conn) -> Response:
        form = dict(await request.form())
        key = f"repos.{_text(form, 'path')}"
        if form.get("remove"):
            settings.update(conn, {key: None})
        else:
            settings.update(
                conn, {key: {"test_command": _text(form, "test_command"), "base_branch": _text(form, "base_branch")}}
            )
        return _refresh()


def _number(form: dict, key: str, kind: type) -> int | float:
    value = _text(form, key)
    try:
        return kind(value)
    except ValueError:
        raise ValidationError(f"{key} must be a {'whole ' if kind is int else ''}number, got {value!r}") from None


# --- Serving under lo9i ---


class ForwardedPrefix:
    """Takes the path the daemon serves this under from X-Forwarded-Prefix
    (the daemon strips it from the request path), so pages can link to it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1")
            if prefix and PREFIX.match(prefix):
                # ASGI's path includes root_path; static files (and url_for) count on it.
                scope = {
                    **scope,
                    "root_path": prefix,
                    "path": prefix + scope["path"],
                    "raw_path": prefix.encode() + scope.get("raw_path", scope["path"].encode()),
                }
        await self.app(scope, receive, send)


def create_app(run_tasks: bool = False) -> FastAPI:
    """With `run_tasks` (the page lo9i runs), a runner works on the tasks in
    this process too, which lo9i keeps running: tasks go on while lo9i has
    the MCP server stopped (runner.py)."""
    runner = Runner(fallback=True) if run_tasks else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runner is None:
            yield
            return
        working = asyncio.create_task(runner.run())
        try:
            yield
        finally:
            working.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await working

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def wake(request: Request, call_next: Any) -> Response:
        """A change from the page: the runner looks at the queue now."""
        response = await call_next(request)
        if runner is not None and request.method == "POST" and response.status_code < 400:
            runner.wake()
        return response

    # Errors outside a form (a task deleted in another tab, a checkpoint
    # approved from chat meanwhile): the page shows the message (base.html).
    @app.exception_handler(FactoryError)
    async def factory_error(request: Request, exc: FactoryError) -> Response:
        status = 404 if isinstance(exc, NotFoundError) else 409 if isinstance(exc, ConflictError) else 400
        return Response(str(exc), status_code=status, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _board_routes(app)
    _task_routes(app)
    _settings_routes(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="factory-web")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()
    db.connect().close()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(run_tasks=True), fd=sock.fileno())


def listen(path: str) -> socket.socket:
    """A Unix socket only this user can connect to. The page has no login,
    so other users on the computer must not reach it (uvicorn's own `uds`
    makes the socket world-writable)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(128)
    return sock
