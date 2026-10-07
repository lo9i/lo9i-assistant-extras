import asyncio
import json

import pytest
from conftest import CHANNEL_ID
from mcp import Client

from watchlist import mcp_server
from watchlist.mcp_server import mcp


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch, http):
    monkeypatch.setenv("WATCHLIST_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("TMDB_API_KEY", "k")
    monkeypatch.setattr(mcp_server, "_http", lambda: http)


def call(*calls: tuple[str, dict]) -> list:
    """Run tool calls in order over one in-process session."""

    async def run():
        async with Client(mcp) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_a_show_from_search_to_watching():
    found, added, watched, progress, listed = call(
        ("search", {"query": "severance", "kind": "show"}),
        ("add_title", {"tmdb_id": 95396, "kind": "show"}),
        ("watch_next", {"id": 1}),
        ("update_title", {"id": 1, "season": 2, "episode": 3}),
        ("list_titles", {"status": "watching"}),
    )
    assert payload(found)["tmdb_id"] == 95396
    show = payload(added)
    assert (show["next_to_watch"], show["unwatched"], show["seasons"]) == ("S01E01", 19, 2)
    assert show["last_aired"] == {"episode": "S02E10", "air_date": "2025-03-20", "name": "Cold Harbor"}
    assert payload(watched)["status"] == "watching"
    assert payload(progress)["next_to_watch"] == "S02E04"
    assert listed.structured_content["result"][0]["unwatched"] == 7


def test_a_movie_has_no_episodes():
    (movie,) = call(("add_title", {"tmdb_id": 1170608, "kind": "movie"}))
    assert {"season", "episode", "unwatched", "next_to_watch"}.isdisjoint(payload(movie))
    assert payload(movie)["runtime"] == 150


def test_a_title_is_added_from_a_tmdb_or_imdb_link():
    by_imdb, missing = call(
        ("add_title", {"imdb_id": "tt11280740", "status": "watching"}),
        ("add_title", {"tmdb_id": 1170608}),
    )
    assert (payload(by_imdb)["name"], payload(by_imdb)["status"]) == ("Severance", "watching")
    assert missing.is_error and "or imdb_id" in missing.content[0].text


def test_service_errors_reach_the_model():
    _, again, bad_status = call(
        ("add_title", {"tmdb_id": 95396, "kind": "show"}),
        ("add_title", {"tmdb_id": 95396, "kind": "show"}),
        ("update_title", {"id": 1, "status": "seen"}),
    )
    assert again.is_error and "Conflict: Severance is already on the list" in again.content[0].text
    assert bad_status.is_error and "'to_watch', 'watching', 'watched', 'dropped'" in bad_status.content[0].text


def test_clear_unsets_progress():
    _, _, cleared, bad = call(
        ("add_title", {"tmdb_id": 95396, "kind": "show"}),
        ("update_title", {"id": 1, "season": 1, "episode": 2}),
        ("update_title", {"id": 1, "clear": ["season", "episode"]}),
        ("update_title", {"id": 1, "clear": ["notes"]}),
    )
    assert payload(cleared)["season"] is None
    assert bad.is_error and "clearable fields" in bad.content[0].text


def test_channels_and_their_uploads():
    added, uploads, removed = call(
        ("add_channel", {"channel": "@mkbhd"}),
        ("new_uploads", {}),
        ("remove_channel", {"channel": "mkbhd"}),
    )
    assert payload(added)["id"] == CHANNEL_ID
    assert [v["id"] for v in payload(uploads)["videos"]] == ["new"]
    assert "failed" not in payload(uploads)
    assert removed.content[0].text == "removed Marques Brownlee & Co"


def test_summary_counts():
    _, summary = call(("add_title", {"tmdb_id": 95396, "kind": "show"}), ("summary", {}))
    assert payload(summary)["counts"] == {"movie": {}, "show": {"to_watch": 1}}


def test_only_read_tools_are_read_only():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    read_only = {t.name for t in asyncio.run(run()) if t.annotations and t.annotations.read_only_hint}
    assert read_only == {"summary", "search", "list_titles", "list_channels", "new_uploads"}
