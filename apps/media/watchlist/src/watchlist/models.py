import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Episode:
    season: int
    episode: int
    # YYYY-MM-DD, when known.
    air_date: str | None = None
    name: str = ""

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    @classmethod
    def from_dict(cls, d: dict | None) -> "Episode | None":
        return cls(d["season"], d["episode"], d.get("air_date"), d.get("name", "")) if d else None


@dataclass(frozen=True)
class Title:
    """A movie or a TV show, with what TMDB says about it."""

    id: int
    # "movie" or "show".
    kind: str
    tmdb_id: int
    name: str
    # "to_watch", "watching", "watched" or "dropped".
    status: str
    # Shows: the last episode watched. Unset before the first.
    season: int | None
    episode: int | None
    notes: str
    # From TMDB, as of refreshed_at.
    overview: str
    poster: str | None
    # Movies: the release date. Shows: the first air date. YYYY-MM-DD.
    released: str | None
    genres: list[str]
    # TMDB's status: "Released" or "In Production" for a movie; "Returning
    # Series", "Ended" or "Canceled" for a show.
    tmdb_status: str
    runtime: int | None
    # Shows: episodes in each season, specials (season 0) left out.
    seasons: dict[int, int]
    last_aired: Episode | None
    next_airing: Episode | None
    # Shows: episodes aired after the last one watched.
    unwatched: int | None
    refreshed_at: str
    added_at: str
    updated_at: str

    @property
    def year(self) -> str | None:
        return self.released[:4] if self.released else None

    @property
    def watched_up_to(self) -> Episode | None:
        return Episode(self.season, self.episode) if self.season is not None else None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Title":
        values = dict(row)
        info = json.loads(values.pop("info"))
        seasons = {int(s): n for s, n in info.get("seasons", {}).items()}
        last_aired = Episode.from_dict(info.get("last_aired"))
        unwatched = None
        if values["kind"] == "show":
            after = (values["season"], values["episode"]) if values["season"] is not None else (0, 0)
            unwatched = sum(1 for e in aired(seasons, last_aired) if e > after)
        return cls(
            **values,
            overview=info.get("overview", ""),
            poster=info.get("poster"),
            released=info.get("released"),
            genres=info.get("genres", []),
            tmdb_status=info.get("tmdb_status", ""),
            runtime=info.get("runtime"),
            seasons=seasons,
            last_aired=last_aired,
            next_airing=Episode.from_dict(info.get("next_airing")),
            unwatched=unwatched,
        )


def episodes(seasons: dict[int, int]) -> list[tuple[int, int]]:
    """Every (season, episode) of a show, in order."""
    return [(s, e) for s in sorted(seasons) for e in range(1, seasons[s] + 1)]


def aired(seasons: dict[int, int], last_aired: Episode | None) -> list[tuple[int, int]]:
    """The episodes aired so far: up to the last one aired."""
    if last_aired is None:
        return []
    last = (last_aired.season, last_aired.episode)
    return [e for e in episodes(seasons) if e <= last]


@dataclass(frozen=True)
class Channel:
    """A YouTube channel the user follows."""

    # YouTube's channel id, UC...
    id: str
    name: str
    # "@handle", when known.
    handle: str | None
    thumbnail: str | None
    added_at: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/{self.handle}" if self.handle else f"https://www.youtube.com/channel/{self.id}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Channel":
        return cls(**dict(row))


@dataclass(frozen=True)
class Video:
    """An upload, read from a channel's feed."""

    id: str
    channel_id: str
    channel: str
    title: str
    url: str
    # ISO 8601, with its time zone.
    published: str
    thumbnail: str | None
    # A YouTube Short.
    short: bool
