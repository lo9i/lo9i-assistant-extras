"""Web UI over the service layer. lo9i starts it (see server.yaml) and shows
it in its web app:

    WATCHLIST_DB=/path/watchlist.db TMDB_API_KEY=... watchlist-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has
no login: the daemon lets only the user's paired devices through. The daemon
serves it under a path (/watchlist/) and says which in X-Forwarded-Prefix;
pages link relative to it (<base> in base.html).

Pages are server-rendered; htmx swaps a page's main section after each
action. A form that fails is re-rendered with its error and the values typed,
in place of the form (HX-Retarget), so nothing is lost. Handlers that reach
TMDB or YouTube are plain functions, which FastAPI runs in a thread pool, so
one slow request doesn't hold up the others.
"""

import argparse
import contextlib
import hashlib
import os
import re
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import db, service
from ..models import Title
from ..service import WatchlistError
from ..tmdb import Tmdb

HERE = Path(__file__).parent
# What the daemon may send as X-Forwarded-Prefix: path segments, like /watchlist.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")
STATUS_LABELS = {"to_watch": "To watch", "watching": "Watching", "watched": "Watched", "dropped": "Dropped"}
# The order a page lists them in. Watched and dropped start folded.
GROUPS = {"show": ("watching", "to_watch", "watched", "dropped"), "movie": ("to_watch", "watched", "dropped")}
FOLDED = ("watched", "dropped")
# How far back the channels page shows uploads.
UPLOAD_DAYS = 14


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it
    again after a deploy changes it instead of using its cached copy."""
    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


def next_up(t: Title) -> str | None:
    """The code of a show's next episode when it has aired, for its Watched button."""
    nxt = service.next_episode(t) if t.kind == "show" else None
    last = t.last_aired
    if nxt is None or last is None or nxt > (last.season, last.episode):
        return None
    return f"S{nxt[0]:02d}E{nxt[1]:02d}"


def ago(published: str) -> str:
    days = (datetime.now(UTC).date() - datetime.fromisoformat(published).date()).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def runtime(minutes: int | None) -> str:
    if not minutes:
        return ""
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes} min"


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["asset"] = asset_url
templates.env.globals["next_up"] = next_up
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["MOVIE_STATUSES"] = service.MOVIE_STATUSES
templates.env.globals["STATUSES"] = service.STATUSES
templates.env.filters["ago"] = ago
templates.env.filters["runtime"] = runtime


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _tmdb(request: Request) -> Tmdb:
    return request.app.state.tmdb


def _http(request: Request) -> httpx.Client:
    return request.app.state.http


# --- Form parsing ---


def _text(form: dict, key: str) -> str:
    return str(form.get(key) or "").strip()


def _int(form: dict, key: str) -> int | None:
    v = _text(form, key)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        raise service.ValidationError(f'{key} must be a whole number, got "{v}"') from None


def _render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def _form_error(request: Request, template: str, target: str, **ctx: Any) -> HTMLResponse:
    """Re-render a failed form, with its error, in place of the form."""
    resp = _render(request, template, **ctx)
    resp.headers["HX-Retarget"] = target
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


def _page(request: Request, page: str, partial: str, **ctx: Any) -> HTMLResponse:
    """The whole page, or only its main section for an htmx request."""
    return _render(request, partial if request.headers.get("HX-Request") else page, **ctx)


# --- Movies and shows ---


def _titles_ctx(conn: sqlite3.Connection, kind: str, **extra: Any) -> dict[str, Any]:
    titles = service.list_titles(conn, kind)
    groups = [
        {"status": s, "label": STATUS_LABELS[s], "folded": s in FOLDED, "titles": [t for t in titles if t.status == s]}
        for s in GROUPS[kind]
    ]
    return {"nav": kind, "kind": kind, "groups": groups, "empty": not titles, "today": date.today().isoformat(), **extra}


def _title_routes(app: FastAPI) -> None:
    def titles_page(request: Request, conn: sqlite3.Connection, kind: str) -> HTMLResponse:
        service.refresh(conn, _tmdb(request), kind=kind)
        return _page(request, "titles.html", "_titles.html", **_titles_ctx(conn, kind))

    @app.get("/", response_class=HTMLResponse)
    def shows(request: Request, conn: Conn) -> HTMLResponse:
        return titles_page(request, conn, "show")

    @app.get("/movies", response_class=HTMLResponse)
    def movies(request: Request, conn: Conn) -> HTMLResponse:
        return titles_page(request, conn, "movie")

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, conn: Conn, kind: str, q: str = "") -> HTMLResponse:
        try:
            found = service.search(_tmdb(request), q, kind)
        except WatchlistError as e:
            return _render(request, "_results.html", found=[], error=str(e))
        added = {t.tmdb_id for t in service.list_titles(conn, kind)}
        return _render(request, "_results.html", found=found, added=added)

    @app.post("/titles", response_class=HTMLResponse)
    async def add_title(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        kind = _text(form, "kind")
        try:
            tmdb_id = _int(form, "tmdb_id")
            if tmdb_id is None:
                raise service.ValidationError("tmdb_id is required")
            await run_in_threadpool(service.add_title, conn, _tmdb(request), tmdb_id, kind)
        except WatchlistError as e:
            return _render(request, "_titles.html", **_titles_ctx(conn, kind, banner=str(e)))
        return _render(request, "_titles.html", **_titles_ctx(conn, kind))

    @app.get("/titles/{id}/edit", response_class=HTMLResponse)
    def edit_title(request: Request, id: int, conn: Conn) -> HTMLResponse:
        return _render(request, "_edit_title.html", t=service.get_title(conn, id), form=None)

    @app.post("/titles/{id}", response_class=HTMLResponse)
    async def save_title(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        title = service.get_title(conn, id)
        try:
            changes: dict[str, Any] = {"status": _text(form, "status"), "notes": _text(form, "notes")}
            if title.kind == "show":
                changes |= {"season": _int(form, "season"), "episode": _int(form, "episode")}
            service.update_title(conn, id, **changes)
        except WatchlistError as e:
            return _form_error(request, "_edit_title.html", f"#title-{id}", t=title, form=form, error=str(e))
        return _render(request, "_titles.html", **_titles_ctx(conn, title.kind))

    @app.post("/titles/{id}/status", response_class=HTMLResponse)
    async def set_status(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        title = service.update_title(conn, id, status=_text(form, "status"))
        return _render(request, "_titles.html", **_titles_ctx(conn, title.kind))

    @app.post("/titles/{id}/next", response_class=HTMLResponse)
    def watch_next(request: Request, id: int, conn: Conn) -> HTMLResponse:
        title = service.watch_next(conn, id)
        return _render(request, "_titles.html", **_titles_ctx(conn, title.kind))

    @app.post("/titles/{id}/delete", response_class=HTMLResponse)
    def delete_title(request: Request, id: int, conn: Conn) -> HTMLResponse:
        title = service.remove_title(conn, id)
        return _render(request, "_titles.html", **_titles_ctx(conn, title.kind))


# --- Channels ---


def _channel_routes(app: FastAPI) -> None:
    def ctx(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
        return {"nav": "channels", "channels": service.list_channels(conn), "days": UPLOAD_DAYS, **extra}

    @app.get("/channels", response_class=HTMLResponse)
    def channels(request: Request, conn: Conn) -> HTMLResponse:
        return _page(request, "channels.html", "_channels.html", **ctx(conn))

    @app.post("/channels", response_class=HTMLResponse)
    async def add_channel(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        try:
            await run_in_threadpool(service.add_channel, conn, _http(request), _text(form, "channel"))
        except WatchlistError as e:
            return _form_error(request, "_add_channel.html", "#add-channel", channel=_text(form, "channel"), error=str(e))
        return _render(request, "_channels.html", **ctx(conn))

    @app.post("/channels/{id}/delete", response_class=HTMLResponse)
    def delete_channel(request: Request, id: str, conn: Conn) -> HTMLResponse:
        service.remove_channel(conn, id)
        return _render(request, "_channels.html", **ctx(conn))

    @app.get("/uploads", response_class=HTMLResponse)
    def uploads(request: Request, conn: Conn) -> HTMLResponse:
        videos, failed = service.new_uploads(conn, _http(request), days=UPLOAD_DAYS)
        return _render(request, "_uploads.html", videos=videos, failed=failed, days=UPLOAD_DAYS)


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


def create_app(http: httpx.Client | None = None) -> FastAPI:
    """`http` reaches TMDB and YouTube; tests pass one that answers for them."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.http = http or httpx.Client()
    app.state.tmdb = Tmdb(os.environ.get("TMDB_API_KEY", ""), app.state.http)

    # Errors outside a form (a title deleted in another tab): the page shows
    # the message (see base.html).
    @app.exception_handler(WatchlistError)
    async def watchlist_error(request: Request, exc: WatchlistError) -> Response:
        status = 404 if isinstance(exc, service.NotFound) else 400
        return Response(str(exc), status_code=status, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _title_routes(app)
    _channel_routes(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="watchlist-web")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()
    db.connect().close()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(), fd=sock.fileno())


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
