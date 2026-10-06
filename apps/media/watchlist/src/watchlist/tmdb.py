"""TMDB, The Movie Database: finding movies and shows, and what it says about
them. https://developer.themoviedb.org/reference

The key is the API Key (32 hex characters, sent as api_key) or the API Read
Access Token (a long JWT, sent as a bearer token); TMDB's settings page shows
both.
"""

from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p/w342"
# Our kinds and TMDB's.
TMDB_KIND = {"movie": "movie", "show": "tv"}


class TmdbError(Exception):
    pass


@dataclass(frozen=True)
class Found:
    """A search result."""

    tmdb_id: int
    kind: str
    name: str
    year: str | None
    overview: str
    poster: str | None


class Tmdb:
    def __init__(self, key: str, http: httpx.Client) -> None:
        self.key = key.strip()
        self.http = http

    def _get(self, path: str, **params: Any) -> dict:
        if not self.key:
            raise TmdbError("TMDB_API_KEY isn't set: enter it in the watchlist plugin's details in Plugins")
        headers = {}
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        try:
            r = self.http.get(f"{API}{path}", params=params, headers=headers, timeout=15)
        except httpx.HTTPError as e:
            raise TmdbError(f"couldn't reach TMDB: {e}") from None
        if r.status_code == 401:
            raise TmdbError("TMDB refused the API key: check TMDB_API_KEY in the plugin's details")
        if r.status_code == 404:
            raise TmdbError(f"TMDB has nothing at {path}")
        if r.status_code != 200:
            raise TmdbError(f"TMDB answered {r.status_code} for {path}")
        return r.json()

    def search(self, query: str, kind: str | None = None) -> list[Found]:
        """Movies and shows matching `query`, best match first. `kind` limits
        it to movies or shows."""
        path = f"/search/{TMDB_KIND[kind]}" if kind else "/search/multi"
        results = self._get(path, query=query, include_adult="false")["results"]
        found = []
        for r in results:
            tmdb_kind = TMDB_KIND[kind] if kind else r.get("media_type")
            if tmdb_kind not in ("movie", "tv"):
                continue
            released = r.get("release_date") or r.get("first_air_date")
            found.append(
                Found(
                    tmdb_id=r["id"],
                    kind="movie" if tmdb_kind == "movie" else "show",
                    name=r.get("title") or r.get("name") or "",
                    year=released[:4] if released else None,
                    overview=r.get("overview") or "",
                    poster=_image(r.get("poster_path")),
                )
            )
        return found[:10]

    def details(self, kind: str, tmdb_id: int) -> tuple[str, dict]:
        """The title's name and what to store about it (Title in models.py)."""
        d = self._get(f"/{TMDB_KIND[kind]}/{tmdb_id}")
        info = {
            "overview": d.get("overview") or "",
            "poster": _image(d.get("poster_path")),
            "released": d.get("release_date") or d.get("first_air_date") or None,
            "genres": [g["name"] for g in d.get("genres", [])],
            "tmdb_status": d.get("status") or "",
        }
        if kind == "movie":
            return d.get("title") or "", {**info, "runtime": d.get("runtime") or None}
        seasons = {
            str(s["season_number"]): s["episode_count"]
            for s in d.get("seasons", [])
            if s.get("season_number") and s.get("episode_count")
        }
        runtimes = d.get("episode_run_time") or []
        return d.get("name") or "", {
            **info,
            "runtime": runtimes[0] if runtimes else None,
            "seasons": seasons,
            "last_aired": _episode(d.get("last_episode_to_air")),
            "next_airing": _episode(d.get("next_episode_to_air")),
        }


def _image(path: str | None) -> str | None:
    return f"{IMAGES}{path}" if path else None


def _episode(e: dict | None) -> dict | None:
    if not e or e.get("season_number") is None or e.get("episode_number") is None:
        return None
    return {
        "season": e["season_number"],
        "episode": e["episode_number"],
        "air_date": e.get("air_date") or None,
        "name": e.get("name") or "",
    }
