"""Web UI over the service layer. lo9i starts it (see server.yaml) and shows
it in its web app:

    WATCHLIST_DB=/path/watchlist.db TMDB_API_KEY=... watchlist-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has
no login: the daemon lets only the user's paired devices through. The daemon
serves it under a path (/watchlist/) and says which in X-Forwarded-Prefix;
pages link relative to it (<base> in base.html).

Pages are server-rendered and look like the assistant's earlier app: a home
page of poster and video rows, tables for shows, movies and channels, and a
page per title. Actions are htmx requests; one that changes what several
parts of a page show answers HX-Refresh, so the page is read again whole.
Handlers that reach TMDB or YouTube are plain functions, which FastAPI runs
in a thread pool, so one slow request doesn't hold up the others.
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
# The order the list pages' tabs and rows go in.
ORDER = {"show": ("watching", "to_watch", "watched", "dropped"), "movie": ("to_watch", "watched", "dropped")}
# How far back the home page shows uploads.
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


def days_until(day: str | None) -> int | None:
    """Days from today to a future YYYY-MM-DD; None for today, the past, or no date."""
    if not day:
        return None
    n = (date.fromisoformat(day) - date.today()).days
    return n if n > 0 else None


def ago(published: str) -> str:
    days = (datetime.now(UTC).date() - datetime.fromisoformat(published).date()).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals |= {
    "asset": asset_url,
    "next_up": next_up,
    "days_until": days_until,
    "plural": plural,
    "STATUS_LABELS": STATUS_LABELS,
    "MOVIE_STATUSES": service.MOVIE_STATUSES,
    "STATUSES": service.STATUSES,
}
templates.env.filters["ago"] = ago


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


def _refresh() -> Response:
    """Read the page again: the change shows in more than one place on it."""
    return Response(headers={"HX-Refresh": "true"})


def _go(request: Request, path: str) -> Response:
    """Open another page of this app."""
    return Response(headers={"HX-Redirect": f"{request.scope.get('root_path', '')}/{path}"})


# --- Home ---


def _home_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, conn: Conn) -> HTMLResponse:
        service.refresh(conn, _tmdb(request))
        titles = service.list_titles(conn)
        up_next = [t for t in titles if t.kind == "show" and t.status in ("watching", "watched") and next_up(t)]
        return _render(
            request,
            "home.html",
            nav="home",
            up_next=up_next,
            movies=[t for t in titles if t.kind == "movie" and t.status == "to_watch"],
            shows=[t for t in titles if t.kind == "show" and t.status in ("watching", "to_watch")],
            has_channels=bool(service.list_channels(conn)),
            days=UPLOAD_DAYS,
        )

    @app.get("/uploads", response_class=HTMLResponse)
    def uploads(request: Request, conn: Conn) -> HTMLResponse:
        """The home page's videos row, read from the channels' feeds."""
        videos, failed = service.new_uploads(conn, _http(request), days=UPLOAD_DAYS)
        return _render(request, "_uploads.html", videos=videos, failed=failed, days=UPLOAD_DAYS)


# --- Movies and shows ---


def _list_routes(app: FastAPI) -> None:
    def list_page(request: Request, conn: sqlite3.Connection, kind: str, status: str) -> HTMLResponse:
        service.refresh(conn, _tmdb(request), kind=kind)
        titles = service.list_titles(conn, kind)
        order = ORDER[kind]
        titles.sort(key=lambda t: (order.index(t.status), t.name.lower()))
        counts = {s: sum(1 for t in titles if t.status == s) for s in order}
        shown = [t for t in titles if t.status == status] if status in order else titles
        ctx = {"nav": kind, "kind": kind, "titles": shown, "total": len(titles), "counts": counts, "tab": status}
        return _render(request, "titles.html", **ctx)

    @app.get("/shows", response_class=HTMLResponse)
    def shows(request: Request, conn: Conn, status: str = "") -> HTMLResponse:
        return list_page(request, conn, "show", status)

    @app.get("/movies", response_class=HTMLResponse)
    def movies(request: Request, conn: Conn, status: str = "") -> HTMLResponse:
        return list_page(request, conn, "movie", status)

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, conn: Conn, q: str = "", kind: str = "") -> HTMLResponse:
        """Results in the Add dialog, as the user types."""
        if not q.strip():
            return HTMLResponse("")
        try:
            found = service.search(_tmdb(request), q, kind or None)
        except WatchlistError as e:
            return _render(request, "_results.html", found=[], error=str(e), q=q)
        added = {(t.kind, t.tmdb_id): t.id for t in service.list_titles(conn)}
        return _render(request, "_results.html", found=found, added=added, q=q)

    @app.post("/titles")
    async def add_title(request: Request, conn: Conn) -> Response:
        """Add a search result and open its page."""
        form = dict(await request.form())
        tmdb_id = _int(form, "tmdb_id")
        if tmdb_id is None:
            raise service.ValidationError("tmdb_id is required")
        title = await run_in_threadpool(service.add_title, conn, _tmdb(request), tmdb_id, _text(form, "kind"))
        return _go(request, f"titles/{title.id}")


# --- A title's page ---


def _title_routes(app: FastAPI) -> None:
    @app.get("/titles/{id}", response_class=HTMLResponse)
    def title_page(request: Request, id: int, conn: Conn) -> HTMLResponse:
        t = service.get_title(conn, id)
        return _render(request, "title.html", nav=t.kind, t=t)

    @app.get("/titles/{id}/seasons/{season}", response_class=HTMLResponse)
    def season(request: Request, id: int, season: int, conn: Conn) -> HTMLResponse:
        """A season's episodes, read from TMDB when its row is opened."""
        t = service.get_title(conn, id)
        try:
            episodes = service.season_episodes(_tmdb(request), t, season)
        except WatchlistError as e:
            return _render(request, "_season.html", t=t, episodes=[], error=str(e))
        return _render(request, "_season.html", t=t, episodes=episodes, today=date.today().isoformat())

    @app.post("/titles/{id}/status")
    async def set_status(request: Request, id: int, conn: Conn) -> Response:
        form = dict(await request.form())
        service.update_title(conn, id, status=_text(form, "status"))
        return _refresh() if _text(form, "refresh") else Response()

    @app.post("/titles/{id}/next")
    def watch_next(id: int, conn: Conn) -> Response:
        service.watch_next(conn, id)
        return _refresh()

    @app.post("/titles/{id}/progress")
    async def set_progress(request: Request, id: int, conn: Conn) -> Response:
        """The last episode watched: one picked in a season's list, or none."""
        form = dict(await request.form())
        service.update_title(conn, id, season=_int(form, "season"), episode=_int(form, "episode"))
        return _refresh()

    @app.post("/titles/{id}/notes", response_class=HTMLResponse)
    async def save_notes(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        t = service.update_title(conn, id, notes=_text(form, "notes"))
        return _render(request, "_notes.html", t=t, saved=True)

    @app.post("/titles/{id}/refresh")
    def refresh_title(request: Request, id: int, conn: Conn) -> Response:
        service.refresh_title(conn, _tmdb(request), id)
        return _refresh()

    @app.post("/titles/{id}/delete")
    async def delete_title(request: Request, id: int, conn: Conn) -> Response:
        """From a list, its row goes (hx-swap delete); from its page, back to the list."""
        form = dict(await request.form())
        t = service.remove_title(conn, id)
        return _go(request, "shows" if t.kind == "show" else "movies") if _text(form, "back") else Response()


# --- Channels ---


def _channel_routes(app: FastAPI) -> None:
    @app.get("/channels", response_class=HTMLResponse)
    def channels(request: Request, conn: Conn) -> HTMLResponse:
        return _render(request, "channels.html", nav="channels", channels=service.list_channels(conn))

    @app.post("/channels", response_class=HTMLResponse)
    async def add_channel(request: Request, conn: Conn) -> Response:
        form = dict(await request.form())
        typed = _text(form, "channel")
        try:
            await run_in_threadpool(service.add_channel, conn, _http(request), typed)
        except WatchlistError as e:
            return _form_error(request, "_add_channel.html", "#add-channel", channel=typed, error=str(e))
        return _refresh()

    @app.get("/channels/{id}", response_class=HTMLResponse)
    def channel_page(request: Request, id: str, conn: Conn) -> HTMLResponse:
        c = service.get_channel(conn, id)
        try:
            videos, error = service.channel_uploads(_http(request), c, shorts=True), None
        except WatchlistError as e:
            videos, error = [], str(e)
        return _render(request, "channel.html", nav="channels", c=c, videos=videos, error=error)

    @app.post("/channels/{id}/delete")
    async def delete_channel(request: Request, id: str, conn: Conn) -> Response:
        form = dict(await request.form())
        service.remove_channel(conn, id)
        return _go(request, "channels") if _text(form, "back") else Response()


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

    # Errors outside a form (a title deleted in another tab, TMDB down): the
    # page shows the message (see base.html).
    @app.exception_handler(WatchlistError)
    async def watchlist_error(request: Request, exc: WatchlistError) -> Response:
        status = 404 if isinstance(exc, service.NotFound) else 400
        return Response(str(exc), status_code=status, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _home_routes(app)
    _list_routes(app)
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
