"""MCP server over the service layer, over stdio. lo9i starts it (see
server.yaml) with WATCHLIST_DB and TMDB_API_KEY set:

    WATCHLIST_DB=/path/watchlist.db TMDB_API_KEY=... watchlist-mcp
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from functools import cache
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, service
from .models import Title
from .tmdb import Tmdb

INSTRUCTIONS = """\
Tracks the movies and TV shows the user wants to watch or is watching, and
the YouTube channels they follow.

- Titles are movies and shows (kind movie or show), from TMDB. Find one with
  search, then add_title with its tmdb_id and kind.
- A title's status is to_watch, watching, watched or dropped (movies have no
  watching). For a show, season and episode are the last episode watched;
  unwatched counts the episodes aired since. watch_next marks the next one.
- Channels are YouTube channels, added from an @handle, a link, or a video of
  theirs. new_uploads reads their latest videos from YouTube.

Start with summary: shows with episodes to catch up on, and episodes and
movies coming out soon. Errors list the valid choices.
"""

mcp = MCPServer("watchlist", instructions=INSTRUCTIONS)

# Lets clients run these without approval. summary and list_titles refresh
# stale TMDB details first, which is upkeep, not a change the caller asked for.
READ_ONLY = ToolAnnotations(read_only_hint=True)


@cache
def _http() -> httpx.Client:
    return httpx.Client()


def _tmdb() -> Tmdb:
    return Tmdb(os.environ.get("TMDB_API_KEY", ""), _http())


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """A connection per call. Service errors reach the model as tool errors
    it can act on."""
    conn = db.connect()
    try:
        yield conn
    except service.WatchlistError as e:
        raise ToolError(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()


def _title(t: Title) -> dict:
    """A title for the model: episode codes instead of nested objects, and
    no per-season counts, which only the service needs."""
    out = asdict(t)
    del out["seasons"]
    for key in ("last_aired", "next_airing"):
        e = getattr(t, key)
        out[key] = {"episode": e.code, "air_date": e.air_date, "name": e.name} if e else None
    if t.kind == "show":
        nxt = service.next_episode(t)
        out["next_to_watch"] = f"S{nxt[0]:02d}E{nxt[1]:02d}" if nxt else None
        out["seasons"] = len(t.seasons)
    else:
        for key in ("season", "episode", "unwatched", "last_aired", "next_airing"):
            del out[key]
    return out


def _changes(clear: list[str] | None, clearable: set[str], **given: Any) -> dict[str, Any]:
    """Keyword arguments for an update: given values, plus None for fields
    named in `clear`. Absent fields are left out, so they stay as they are."""
    out = {k: v for k, v in given.items() if v is not None}
    for f in clear or []:
        if f not in clearable:
            raise ToolError(f"cannot clear {f!r}, clearable fields: {sorted(clearable)}")
        out[f] = None
    return out


# --- Overview ---


@mcp.tool(annotations=READ_ONLY)
def summary(
    days: Annotated[int, Field(description="How far ahead to look for releases and episodes")] = 30,
) -> dict:
    """Shows with aired episodes not watched yet (catch_up), episodes and
    movies coming out in the next `days` (coming), and counts by status."""
    with _db() as conn:
        service.refresh(conn, _tmdb())
        return service.summary(conn, days)


# --- Titles ---


@mcp.tool(annotations=READ_ONLY)
def search(
    query: str,
    kind: Annotated[str | None, Field(description="movie or show; absent searches both")] = None,
) -> list[dict]:
    """Find movies and shows on TMDB, best match first. Use the year to tell
    remakes apart."""
    with _db():
        return [asdict(f) for f in service.search(_tmdb(), query, kind)]


@mcp.tool(annotations=READ_ONLY)
def list_titles(
    kind: Annotated[str | None, Field(description="movie or show")] = None,
    status: Annotated[str | None, Field(description="to_watch, watching, watched or dropped")] = None,
) -> list[dict]:
    """Movies and shows on the list, by name, optionally filtered."""
    with _db() as conn:
        service.refresh(conn, _tmdb(), kind=kind)
        return [_title(t) for t in service.list_titles(conn, kind, status)]


@mcp.tool()
def add_title(
    tmdb_id: Annotated[int, Field(description="From search")],
    kind: Annotated[str, Field(description="movie or show, as search said")],
    status: Annotated[str, Field(description="to_watch, watching, watched or dropped")] = "to_watch",
    notes: str = "",
) -> dict:
    """Add a movie or show found with search."""
    with _db() as conn:
        return _title(service.add_title(conn, _tmdb(), tmdb_id, kind, status=status, notes=notes))


@mcp.tool()
def update_title(
    id: int,
    status: Annotated[str | None, Field(description="to_watch, watching, watched or dropped")] = None,
    season: Annotated[int | None, Field(description="Shows: the season of the last episode watched")] = None,
    episode: Annotated[int | None, Field(description="Shows: the last episode watched, in that season")] = None,
    notes: str | None = None,
    clear: Annotated[
        list[str] | None,
        Field(description="Fields to unset: season and episode together (not started)"),
    ] = None,
) -> dict:
    """Change a title. Omitted fields stay as they are. Watching an episode of
    a show still to watch makes it watching."""
    with _db() as conn:
        changes = _changes(clear, {"season", "episode"}, status=status, season=season, episode=episode, notes=notes)
        return _title(service.update_title(conn, id, **changes))


@mcp.tool()
def watch_next(id: int) -> dict:
    """Mark the next episode of a show as watched."""
    with _db() as conn:
        return _title(service.watch_next(conn, id))


@mcp.tool()
def remove_title(id: int) -> str:
    """Take a movie or show off the list."""
    with _db() as conn:
        title = service.remove_title(conn, id)
    return f"removed {title.name}"


# --- Channels ---


@mcp.tool(annotations=READ_ONLY)
def list_channels() -> list[dict]:
    """The YouTube channels followed, by name."""
    with _db() as conn:
        return [{**asdict(c), "url": c.url} for c in service.list_channels(conn)]


@mcp.tool()
def add_channel(
    channel: Annotated[str, Field(description="@handle, a link to the channel or to one of its videos, or its UC… id")],
) -> dict:
    """Follow a YouTube channel."""
    with _db() as conn:
        c = service.add_channel(conn, _http(), channel)
        return {**asdict(c), "url": c.url}


@mcp.tool()
def remove_channel(channel: Annotated[str, Field(description="Its id or @handle")]) -> str:
    """Stop following a YouTube channel."""
    with _db() as conn:
        c = service.remove_channel(conn, channel)
    return f"removed {c.name}"


@mcp.tool(annotations=READ_ONLY)
def new_uploads(
    days: Annotated[int, Field(description="How many days back")] = 7,
    channel: Annotated[str | None, Field(description="One channel's id or @handle; absent reads them all")] = None,
    shorts: Annotated[bool, Field(description="Include YouTube Shorts")] = False,
) -> dict:
    """The videos the followed channels uploaded lately, newest first, read
    from YouTube now. A channel's feed has its last 15 uploads."""
    with _db() as conn:
        videos, failed = service.new_uploads(conn, _http(), days=days, channel=channel, shorts=shorts)
    out: dict[str, Any] = {"videos": [asdict(v) for v in videos]}
    if failed:
        out["failed"] = failed
    return out


def main() -> None:
    # Fail now on a bad database path, not on the first tool call.
    db.connect().close()
    mcp.run()


if __name__ == "__main__":
    main()
