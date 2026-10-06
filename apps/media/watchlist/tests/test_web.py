import tempfile
from pathlib import Path

import pytest
from conftest import CHANNEL_ID
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


def test_the_pages_link_under_the_prefix_lo9i_serves_them_at(client):
    page = client.get("/", headers={"X-Forwarded-Prefix": "/watchlist"}).text
    assert '<base href="/watchlist/">' in page and "Search for a show" in page
    assert client.get("/static/app.css", headers={"X-Forwarded-Prefix": "/watchlist"}).status_code == 200


def test_a_show_is_found_and_added(client):
    results = client.get("/search", headers=HX, params={"kind": "show", "q": "sev"}).text
    assert "Severance" in results and "Dune" not in results and "hx-post=\"titles\"" in results
    page = client.post("/titles", headers=HX, data={"tmdb_id": "95396", "kind": "show"}).text
    assert "Severance" in page and "19 to watch" in page and "Watched S01E01" in page
    assert "On the list" in client.get("/search", headers=HX, params={"kind": "show", "q": "sev"}).text


def test_search_errors_show_in_the_results(client, monkeypatch):
    monkeypatch.setattr(client.app.state.tmdb, "key", "")
    assert "TMDB_API_KEY isn&#39;t set" in client.get("/search", headers=HX, params={"kind": "show", "q": "x"}).text


def test_adding_twice_says_so(client):
    client.post("/titles", headers=HX, data={"tmdb_id": "95396", "kind": "show"})
    page = client.post("/titles", headers=HX, data={"tmdb_id": "95396", "kind": "show"}).text
    assert 'class="banner"' in page and "already on the list" in page


def test_watching_episodes_and_moving_between_groups(client):
    client.post("/titles", headers=HX, data={"tmdb_id": "95396", "kind": "show"})
    page = client.post("/titles/1/next", headers=HX).text
    assert "Watched up to S01E01" in page and "Watched S01E02" in page
    assert titles()[0].status == "watching"
    page = client.post("/titles/1/status", headers=HX, data={"status": "dropped"}).text
    assert "Watched S01E02" not in page
    assert '<details class="card group" >\n    <summary>Dropped' in page  # folded


def test_editing_progress_rerenders_the_form_on_errors(client):
    client.post("/titles", headers=HX, data={"tmdb_id": "95396", "kind": "show"})
    assert 'name="season"' in client.get("/titles/1/edit", headers=HX).text
    r = client.post("/titles/1", headers=HX, data={"status": "watching", "season": "4", "episode": "1", "notes": "x"})
    assert r.headers["HX-Retarget"] == "#title-1" and "no S04E01" in r.text and 'value="4"' in r.text
    client.post("/titles/1", headers=HX, data={"status": "watching", "season": "2", "episode": "2", "notes": "with Ana"})
    t = titles()[0]
    assert (t.watched_up_to.code, t.notes) == ("S02E02", "with Ana")


def test_movies_have_their_own_page(client):
    client.post("/titles", headers=HX, data={"tmdb_id": "1170608", "kind": "movie"})
    page = client.get("/movies").text
    assert "Dune: Part Three" in page and "Out " in page and "2h 30m" in page and '"watching"' not in page
    assert "Dune" not in client.get("/").text


def test_deleting_a_title(client):
    client.post("/titles", headers=HX, data={"tmdb_id": "95396", "kind": "show"})
    assert "Severance" not in client.post("/titles/1/delete", headers=HX).text
    r = client.post("/titles/1/delete", headers=HX)
    assert r.status_code == 404 and "no title 1" in r.text


def test_following_a_channel_and_its_uploads(client):
    page = client.post("/channels", headers=HX, data={"channel": "@mkbhd"}).text
    assert "Marques Brownlee &amp; Co" in page and 'hx-get="uploads"' in page
    uploads = client.get("/uploads", headers=HX).text
    assert "New phone" in uploads and "yesterday" in uploads and "A short" not in uploads and "Old phone" not in uploads
    assert "Marques" not in client.post(f"/channels/{CHANNEL_ID}/delete", headers=HX).text


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
