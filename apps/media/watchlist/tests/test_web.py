import tempfile
from pathlib import Path

import pytest
from conftest import CHANNEL_ID, SOON
from fastapi.testclient import TestClient

from watchlist import db, service
from watchlist.web.app import create_app, listen

HX = {"HX-Request": "true"}


@pytest.fixture
def client(tmp_path, monkeypatch, http):
    monkeypatch.setenv("WATCHLIST_DB", str(tmp_path / "web.db"))
    monkeypatch.setenv("TMDB_API_KEY", "k")
    return TestClient(create_app(http))


def titles():
    conn = db.connect()
    try:
        return service.list_titles(conn)
    finally:
        conn.close()


def add(client, tmdb_id=95396, kind="show"):
    return client.post("/titles", headers=HX, data={"tmdb_id": str(tmdb_id), "kind": kind})


def test_the_pages_link_under_the_prefix_lo9i_serves_them_at(client):
    page = client.get("/", headers={"X-Forwarded-Prefix": "/watchlist"}).text
    assert '<base href="/watchlist/">' in page and "No shows yet" in page
    assert client.get("/static/app.css", headers={"X-Forwarded-Prefix": "/watchlist"}).status_code == 200


def test_searching_lists_movies_and_shows_to_add(client):
    results = client.get("/search", headers=HX, params={"q": "sev"}).text
    assert "Severance" in results and "TV · 2022" in results and "Dune: Part Three" in results
    assert client.get("/search", headers=HX, params={"q": " "}).text == ""


def test_search_errors_show_in_the_results(client, monkeypatch):
    monkeypatch.setattr(client.app.state.tmdb, "key", "")
    assert "TMDB_API_KEY isn&#39;t set" in client.get("/search", headers=HX, params={"q": "x"}).text


def test_adding_a_title_opens_its_page(client):
    r = add(client, kind="show")
    assert r.headers["HX-Redirect"] == "/titles/1"
    page = client.get("/titles/1").text
    assert "Severance" in page and "RETURNING SERIES" in page and "19 aired episodes to watch" in page
    assert 'hx-get="titles/1/seasons/2"' in page
    # Search now links to it instead of adding it again.
    assert 'href="titles/1">Open' in client.get("/search", headers=HX, params={"q": "sev"}).text


def test_adding_twice_says_so(client):
    add(client)
    r = add(client)
    assert r.status_code == 400 and "already on the list" in r.text


def test_the_home_page_shows_whats_next(client):
    add(client)
    add(client, 1170608, "movie")
    client.post("/titles/1/progress", headers=HX, data={"season": "2", "episode": "8"})
    page = client.get("/").text
    assert "Up next" in page and "2 episodes to watch" in page and "Watched S02E09" in page
    assert f"releases {SOON}" in page and "in 10 days" in page


def test_watching_the_next_episode_reloads_the_page(client):
    add(client)
    r = client.post("/titles/1/next", headers=HX)
    assert r.headers["HX-Refresh"] == "true"
    t = titles()[0]
    assert (t.watched_up_to.code, t.status) == ("S01E01", "watching")


def test_lists_have_a_tab_per_status(client):
    add(client)
    add(client, 1170608, "movie")
    client.post("/titles/1/status", headers=HX, data={"status": "dropped"})
    page = client.get("/shows").text
    assert "All 1" in page and "Dropped 1" in page and "Severance" in page and "Dune" not in page
    assert "Severance" not in client.get("/shows", params={"status": "watching"}).text
    assert "Dune: Part Three" in client.get("/movies").text


def test_a_season_lists_its_episodes_to_mark_progress(client, tmdb_data):
    tmdb_data["/3/tv/95396/season/1"] = {
        "episodes": [{"episode_number": n, "name": f"Ep {n}", "air_date": "2022-02-18"} for n in range(1, 10)]
    }
    add(client)
    client.post("/titles/1/progress", headers=HX, data={"season": "1", "episode": "2"})
    season = client.get("/titles/1/seasons/1", headers=HX).text
    assert season.count("✓ Watched") == 2 and "Ep 9" in season
    assert '"season": 1, "episode": 3' in season
    assert "no season 7" in client.get("/titles/1/seasons/7", headers=HX).text


def test_progress_errors_reach_the_page(client):
    add(client)
    r = client.post("/titles/1/progress", headers=HX, data={"season": "4", "episode": "1"})
    assert r.status_code == 400 and "no S04E01" in r.text


def test_notes_are_saved(client):
    add(client)
    assert "Saved" in client.post("/titles/1/notes", headers=HX, data={"notes": "with Ana"}).text
    assert titles()[0].notes == "with Ana"


def test_removing_from_its_page_goes_back_to_the_list(client):
    add(client, 1170608, "movie")
    assert client.post("/titles/1/delete", headers=HX, data={"back": "1"}).headers["HX-Redirect"] == "/movies"
    r = client.post("/titles/1/delete", headers=HX)
    assert r.status_code == 404 and "no title 1" in r.text


def test_following_a_channel_and_its_uploads(client):
    assert client.post("/channels", headers=HX, data={"channel": "@mkbhd"}).headers["HX-Refresh"] == "true"
    assert "Marques Brownlee &amp; Co" in client.get("/channels").text
    uploads = client.get("/uploads", headers=HX).text
    assert "New phone" in uploads and "yesterday" in uploads and "A short" not in uploads and "Old phone" not in uploads
    channel = client.get(f"/channels/{CHANNEL_ID}").text
    assert "A short" in channel and "Old phone" in channel
    client.post(f"/channels/{CHANNEL_ID}/delete", headers=HX)
    assert "No channels yet" in client.get("/channels").text


def test_a_channel_that_cant_be_found_keeps_what_was_typed(client):
    r = client.post("/channels", headers=HX, data={"channel": "@nobody"})
    assert r.headers["HX-Retarget"] == "#add-channel" and "no page" in r.text and 'value="@nobody"' in r.text


def test_the_socket_is_only_the_users():
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sock = listen(str(Path(d) / "web.sock"))
        try:
            assert (Path(d) / "web.sock").stat().st_mode & 0o777 == 0o600
        finally:
            sock.close()
