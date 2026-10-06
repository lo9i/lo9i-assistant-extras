"""Movies and shows to watch, how far the user is in each show, and the
YouTube channels they follow.

Every rule lives here so the MCP server and the web page behave the same.
Errors name the valid choices, so a caller that guessed wrong can fix it
without a second lookup.

Update functions take UNSET for "leave as is". None, where allowed, clears
the field.
"""

import logging
import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from . import repo, youtube
from .models import Channel, Title, Video, episodes
from .tmdb import Found, Tmdb, TmdbError

log = logging.getLogger(__name__)

UNSET: Any = object()

KINDS = ("movie", "show")
STATUSES = ("to_watch", "watching", "watched", "dropped")
# A movie is watched in one go.
MOVIE_STATUSES = ("to_watch", "watched", "dropped")
# TMDB's status for a show that won't air more episodes.
ENDED = ("Ended", "Canceled")
# How old what TMDB said may get: shows still airing and movies not out yet
# change (next episode, release date); the rest rarely do.
FRESH_FOR = timedelta(hours=12)
FRESH_FOR_SETTLED = timedelta(days=30)


class WatchlistError(Exception):
    pass


class ValidationError(WatchlistError):
    pass


class NotFound(WatchlistError):
    pass


class Conflict(WatchlistError):
    pass


class RemoteError(WatchlistError):
    """TMDB or YouTube failed or said no."""


# --- Input checks ---


def _required(s: str, field: str) -> str:
    s = s.strip()
    if not s:
        raise ValidationError(f"{field} is required")
    return s


def _choice(value: Any, choices: tuple, field: str) -> Any:
    if value not in choices:
        raise ValidationError(f"{field} must be one of {list(choices)}, got {value!r}")
    return value


def _status(kind: str, status: str) -> str:
    return _choice(status, MOVIE_STATUSES if kind == "movie" else STATUSES, f"a {kind}'s status")


def _parallel(fn: Callable[[Any], Any], items: Iterable[Any]) -> list[tuple[Any, Any]]:
    """(item, result) for each item, fetched at the same time. A failure's
    result is its exception."""

    def run(item: Any) -> tuple[Any, Any]:
        try:
            return item, fn(item)
        except Exception as e:  # reported to the caller, item by item
            return item, e

    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return list(pool.map(run, items))


# --- Titles ---


def search(tmdb: Tmdb, query: str, kind: str | None = None) -> list[Found]:
    query = _required(query, "query")
    if kind is not None:
        _choice(kind, KINDS, "kind")
    try:
        return tmdb.search(query, kind)
    except TmdbError as e:
        raise RemoteError(str(e)) from None


def list_titles(conn: sqlite3.Connection, kind: str | None = None, status: str | None = None) -> list[Title]:
    if kind is not None:
        _choice(kind, KINDS, "kind")
    if status is not None:
        _choice(status, STATUSES, "status")
    return repo.list_titles(conn, kind, status)


def get_title(conn: sqlite3.Connection, id: int) -> Title:
    title = repo.get_title(conn, id)
    if title is None:
        known = [f"{t.id} ({t.name})" for t in repo.list_titles(conn)]
        raise NotFound(f"no title {id}, expected one of {known}")
    return title


def add_title(
    conn: sqlite3.Connection, tmdb: Tmdb, tmdb_id: int, kind: str, *, status: str = "to_watch", notes: str = ""
) -> Title:
    """Add a movie or show found with `search`."""
    _choice(kind, KINDS, "kind")
    _status(kind, status)
    if existing := repo.find_title(conn, kind, tmdb_id):
        raise Conflict(f"{existing.name} is already on the list, as {existing.id}")
    try:
        name, info = tmdb.details(kind, tmdb_id)
    except TmdbError as e:
        raise RemoteError(str(e)) from None
    with conn:
        id = repo.insert_title(
            conn,
            kind=kind,
            tmdb_id=tmdb_id,
            name=name or f"TMDB {tmdb_id}",
            status=status,
            notes=notes.strip(),
            info=info,
        )
    return get_title(conn, id)


def _progress(title: Title, season: int | None, episode: int | None) -> tuple[int, int] | tuple[None, None]:
    if title.kind != "show":
        raise ValidationError(f"{title.name} is a movie: only shows have episodes")
    if (season is None) != (episode is None):
        raise ValidationError("give both season and episode, or clear both to mark the show as not started")
    if season is None:
        return None, None
    if title.seasons and (season not in title.seasons or not 1 <= episode <= title.seasons[season]):
        counts = ", ".join(f"season {s}: {n}" for s, n in sorted(title.seasons.items()))
        raise ValidationError(f"{title.name} has no S{season:02d}E{episode:02d}; episodes per season are {counts}")
    if season < 1 or episode < 1:
        raise ValidationError("season and episode start at 1")
    return season, episode


def update_title(
    conn: sqlite3.Connection,
    id: int,
    *,
    status: str = UNSET,
    season: int | None = UNSET,
    episode: int | None = UNSET,
    notes: str = UNSET,
) -> Title:
    """`season` and `episode` are the last episode watched, given together.
    Watching an episode of a show still to watch starts it, unless `status`
    says otherwise."""
    title = get_title(conn, id)
    fields: dict[str, Any] = {}
    if season is not UNSET or episode is not UNSET:
        s, e = _progress(
            title, title.season if season is UNSET else season, title.episode if episode is UNSET else episode
        )
        fields |= {"season": s, "episode": e}
        if s is not None and title.status == "to_watch":
            fields["status"] = "watching"
    if status is not UNSET:
        fields["status"] = _status(title.kind, status)
    if notes is not UNSET:
        fields["notes"] = notes.strip()
    with conn:
        repo.update_title(conn, id, fields)
    return get_title(conn, id)


def next_episode(title: Title) -> tuple[int, int] | None:
    """The episode after the last one watched, aired or not."""
    after = (title.season, title.episode) if title.season is not None else (0, 0)
    return next((e for e in episodes(title.seasons) if e > after), None)


def watch_next(conn: sqlite3.Connection, id: int) -> Title:
    """Mark the next episode of a show as watched. The show is then being
    watched, or watched once it's caught up and has ended."""
    title = get_title(conn, id)
    if title.kind != "show":
        raise ValidationError(f"{title.name} is a movie: only shows have episodes")
    nxt = next_episode(title)
    code = f"S{nxt[0]:02d}E{nxt[1]:02d}" if nxt else ""
    if nxt is None:
        at = f" after {title.watched_up_to.code}" if title.watched_up_to else ""
        raise ValidationError(f"TMDB lists no episode of {title.name}{at}")
    last = title.last_aired
    if last is None or nxt > (last.season, last.episode):
        when = f": it airs {title.next_airing.air_date}" if title.next_airing and title.next_airing.air_date else ""
        raise ValidationError(f"{title.name} {code} hasn't aired yet{when}")
    caught_up = nxt == (last.season, last.episode)
    status = "watched" if caught_up and title.tmdb_status in ENDED else "watching"
    with conn:
        repo.update_title(conn, id, {"season": nxt[0], "episode": nxt[1], "status": status})
    return get_title(conn, id)


def remove_title(conn: sqlite3.Connection, id: int) -> Title:
    title = get_title(conn, id)
    with conn:
        repo.delete_title(conn, id)
    return title


def _settled(t: Title, today: date) -> bool:
    """Whether what TMDB says about it is unlikely to change soon."""
    if t.kind == "show":
        return t.status == "dropped" or t.tmdb_status in ENDED
    return t.released is not None and date.fromisoformat(t.released) < today - timedelta(days=30)


def refresh(conn: sqlite3.Connection, tmdb: Tmdb, *, kind: str | None = None) -> int:
    """Fetch again what TMDB says about titles whose copy got old (FRESH_FOR),
    so next episodes and release dates stay right. A title TMDB fails on keeps
    its old copy: stale beats blank. Returns how many were refreshed."""
    now, today = datetime.now(UTC), date.today()
    stale = [
        t
        for t in repo.list_titles(conn, kind)
        if now - datetime.fromisoformat(t.refreshed_at) > (FRESH_FOR_SETTLED if _settled(t, today) else FRESH_FOR)
    ]
    done = 0
    for t, result in _parallel(lambda t: tmdb.details(t.kind, t.tmdb_id), stale):
        if isinstance(result, Exception):
            log.warning("couldn't refresh %s from TMDB: %s", t.name, result)
            continue
        name, info = result
        with conn:
            repo.save_info(conn, t.id, name or t.name, info)
        done += 1
    return done


def summary(conn: sqlite3.Connection, days: int = 30) -> dict:
    """What to watch next and what's coming out in the next `days`."""
    today = date.today()
    until = (today + timedelta(days=days)).isoformat()
    titles = repo.list_titles(conn)
    catch_up = []
    for t in titles:
        if t.kind == "show" and t.status in ("watching", "watched") and t.unwatched:
            s, e = next_episode(t)
            catch_up.append(
                {
                    "id": t.id,
                    "name": t.name,
                    "unwatched": t.unwatched,
                    "watched_up_to": t.watched_up_to.code if t.watched_up_to else None,
                    "next_to_watch": f"S{s:02d}E{e:02d}",
                }
            )
    coming = []
    for t in titles:
        if t.status == "dropped" or (t.kind == "movie" and t.status == "watched"):
            continue
        if t.kind == "show" and t.next_airing and t.next_airing.air_date:
            n = t.next_airing
            if today.isoformat() <= n.air_date <= until:
                show = {"id": t.id, "name": t.name, "kind": "show"}
                coming.append({"date": n.air_date, **show, "episode": n.code, "episode_name": n.name})
        if t.kind == "movie" and t.released and today.isoformat() <= t.released <= until:
            coming.append({"date": t.released, "id": t.id, "name": t.name, "kind": "movie"})
    counts: dict[str, dict[str, int]] = {k: {} for k in KINDS}
    for t in titles:
        counts[t.kind][t.status] = counts[t.kind].get(t.status, 0) + 1
    return {
        "today": today.isoformat(),
        "catch_up": catch_up,
        "coming": sorted(coming, key=lambda c: (c["date"], c["name"])),
        "counts": counts,
        "channels": len(repo.list_channels(conn)),
    }


# --- Channels ---


def list_channels(conn: sqlite3.Connection) -> list[Channel]:
    return repo.list_channels(conn)


def get_channel(conn: sqlite3.Connection, key: str) -> Channel:
    """By id or @handle."""
    k = key.strip().removeprefix("@").lower()
    channels = repo.list_channels(conn)
    for c in channels:
        if k in (c.id.lower(), (c.handle or "").removeprefix("@").lower()):
            return c
    known = [f"{c.id} ({c.name})" for c in channels]
    raise NotFound(f'no channel "{key}" is followed, expected one of {known}')


def add_channel(conn: sqlite3.Connection, http: httpx.Client, channel: str) -> Channel:
    """Follow the channel `channel` names: its @handle, a link to it or to one
    of its videos, or its UC… id."""
    try:
        found = youtube.resolve(channel, http)
    except youtube.YoutubeError as e:
        raise RemoteError(str(e)) from None
    with conn:
        if not repo.insert_channel(conn, found.id, found.name, found.handle, found.thumbnail):
            raise Conflict(f"{found.name} is already followed")
    return get_channel(conn, found.id)


def remove_channel(conn: sqlite3.Connection, key: str) -> Channel:
    channel = get_channel(conn, key)
    with conn:
        repo.delete_channel(conn, channel.id)
    return channel


def channel_uploads(http: httpx.Client, channel: Channel, *, shorts: bool = False) -> list[Video]:
    try:
        videos = youtube.uploads(channel.id, http)
    except youtube.YoutubeError as e:
        raise RemoteError(str(e)) from None
    return [v for v in videos if shorts or not v.short]


def new_uploads(
    conn: sqlite3.Connection, http: httpx.Client, *, days: int = 7, channel: str | None = None, shorts: bool = False
) -> tuple[list[Video], dict[str, str]]:
    """Uploads of the last `days` from every channel followed (or one),
    newest first, and the channels whose feed failed, with why."""
    if days < 1:
        raise ValidationError(f"days must be at least 1, got {days}")
    channels = [get_channel(conn, channel)] if channel else repo.list_channels(conn)
    since = datetime.now(UTC) - timedelta(days=days)
    videos, failed = [], {}
    for c, result in _parallel(lambda c: channel_uploads(http, c, shorts=shorts), channels):
        if isinstance(result, Exception):
            failed[c.name] = str(result)
            continue
        videos += [v for v in result if datetime.fromisoformat(v.published) >= since]
    return sorted(videos, key=lambda v: v.published, reverse=True), failed
