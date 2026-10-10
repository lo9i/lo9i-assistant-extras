"""TMDB, The Movie Database: finding movies and shows, and what it says about
them. https://developer.themoviedb.org/reference

The key is the API Key (32 hex characters, sent as api_key) or the API Read
Access Token (a long JWT, sent as a bearer token); TMDB's settings page shows
both.
"""

from dataclasses import dataclass
from datetime import date
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
        found = [_found(r, TMDB_KIND[kind] if kind else r.get("media_type")) for r in results]
        return [f for f in found if f][:10]

    def find_imdb(self, imdb_id: str) -> Found | None:
        """The movie or show with this IMDb id (tt…), from an IMDb link."""
        d = self._get(f"/find/{imdb_id}", external_source="imdb_id")
        found = [_found(r, "movie") for r in d.get("movie_results", [])]
        found += [_found(r, "tv") for r in d.get("tv_results", [])]
        return next((f for f in found if f), None)

    def details(self, kind: str, tmdb_id: int) -> tuple[str, dict]:
        """The title's name and what to store about it (Title in models.py)."""
        d = self._get(f"/{TMDB_KIND[kind]}/{tmdb_id}", append_to_response="external_ids")
        info = {
            "overview": d.get("overview") or "",
            "poster": _image(d.get("poster_path")),
            "released": d.get("release_date") or d.get("first_air_date") or None,
            "genres": [g["name"] for g in d.get("genres", [])],
            "tmdb_status": d.get("status") or "",
            "rating": d.get("vote_average") or None,
            "imdb_id": d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id") or None,
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
            "last_aired": _last_aired(d),
            "next_airing": _episode(d.get("next_episode_to_air")),
        }


    def season(self, tmdb_id: int, season: int) -> list[dict]:
        """A show's season: its episodes' numbers, names and air dates."""
        d = self._get(f"/tv/{tmdb_id}/season/{season}")
        return [
            {"episode": e["episode_number"], "name": e.get("name") or "", "air_date": e.get("air_date") or None}
            for e in d.get("episodes", [])
        ]


def _found(r: dict, tmdb_kind: str | None) -> Found | None:
    """A search or find result, or None when it's neither a movie nor a show (a person)."""
    if tmdb_kind not in ("movie", "tv"):
        return None
    released = r.get("release_date") or r.get("first_air_date")
    return Found(
        tmdb_id=r["id"],
        kind="movie" if tmdb_kind == "movie" else "show",
        name=r.get("title") or r.get("name") or "",
        year=released[:4] if released else None,
        overview=r.get("overview") or "",
        poster=_image(r.get("poster_path")),
    )


def _image(path: str | None) -> str | None:
    return f"{IMAGES}{path}" if path else None


def _last_aired(d: dict) -> dict | None:
    """The last regular episode aired. When TMDB's last one is a special
    (season 0, which isn't counted among the seasons), it's the end of the
    latest season that had started airing by the special's date."""
    last = _episode(d.get("last_episode_to_air"))
    if last is None or last["season"] != 0:
        return last
    by = last["air_date"] or date.today().isoformat()
    started = [
        s
        for s in d.get("seasons", [])
        if s.get("season_number") and s.get("episode_count") and s.get("air_date") and s["air_date"] <= by
    ]
    if not started:
        return None
    season = max(started, key=lambda s: s["season_number"])
    return {"season": season["season_number"], "episode": season["episode_count"], "air_date": None, "name": ""}


def _episode(e: dict | None) -> dict | None:
    if not e or e.get("season_number") is None or e.get("episode_number") is None:
        return None
    return {
        "season": e["season_number"],
        "episode": e["episode_number"],
        "air_date": e.get("air_date") or None,
        "name": e.get("name") or "",
    }
