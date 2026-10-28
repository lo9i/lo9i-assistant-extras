"""Reading Hacker News: the front-page lists from its Firebase API, search and mentions from Algolia's."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

FIREBASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA = "https://hn.algolia.com/api/v1"
LISTS = {"top": "topstories", "new": "newstories", "best": "beststories", "show": "showstories", "ask": "askstories"}


class HNError(Exception):
    """HN couldn't be read. The text says why."""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, headers={"User-Agent": "lo9i-hackernews"})


def _discussion(item_id: Any) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


def _story(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title", ""),
        "url": item.get("url") or _discussion(item["id"]),
        "discussion": _discussion(item["id"]),
        "points": item.get("score", 0),
        "comments": item.get("descendants", 0),
        "by": item.get("by", ""),
        "time": datetime.fromtimestamp(item.get("time", 0), UTC).isoformat(),
    }


def _hit(hit: dict[str, Any]) -> dict[str, Any]:
    is_comment = "comment" in hit.get("_tags", [])
    story_id = hit.get("story_id") or hit["objectID"]
    found = {
        "kind": "comment" if is_comment else "story",
        "title": hit.get("title") or hit.get("story_title") or "",
        "discussion": _discussion(story_id),
        "link": _discussion(hit["objectID"]),
        "by": hit.get("author", ""),
        "time": hit.get("created_at", ""),
    }
    if is_comment:
        found["text"] = (hit.get("comment_text") or "")[:500]
    else:
        found |= {
            "url": hit.get("url") or _discussion(hit["objectID"]),
            "points": hit.get("points") or 0,
            "comments": hit.get("num_comments") or 0,
        }
    return found


async def _get(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    try:
        response = await client.get(url, params=params or None)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as e:
        raise HNError(f"Couldn't read Hacker News: {e}") from None


async def stories(kind: str = "top", limit: int = 10) -> list[dict[str, Any]]:
    if kind not in LISTS:
        raise HNError(f"Unknown list {kind!r}; use one of {', '.join(LISTS)}.")
    limit = max(1, min(limit, 30))
    async with _client() as client:
        ids = (await _get(client, f"{FIREBASE}/{LISTS[kind]}.json"))[:limit]
        items = await asyncio.gather(*(_get(client, f"{FIREBASE}/item/{i}.json") for i in ids))
    return [_story(item) for item in items if item and not item.get("deleted") and not item.get("dead")]


async def search(
    query: str, days: int | None = None, sort: str = "relevance", limit: int = 10, tags: str = "story"
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "tags": tags, "hitsPerPage": max(1, min(limit, 50))}
    if days:
        since = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
        params["numericFilters"] = f"created_at_i>{since}"
    endpoint = "search_by_date" if sort == "date" else "search"
    async with _client() as client:
        found = await _get(client, f"{ALGOLIA}/{endpoint}", **params)
    return [_hit(hit) for hit in found.get("hits", [])]


async def mentions(word: str, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    """Stories and comments with `word`, newest first."""
    return await search(word, days=days, sort="date", limit=limit, tags="(story,comment)")
