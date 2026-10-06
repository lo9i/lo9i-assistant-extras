from datetime import date, timedelta

import pytest
from conftest import CHANNEL_ID, LATER, SOON

from watchlist import service, youtube
from watchlist.service import Conflict, NotFound, RemoteError, ValidationError
from watchlist.tmdb import Tmdb


def add_show(conn, tmdb, **kw):
    return service.add_title(conn, tmdb, 95396, "show", **kw)


def make_old(conn, id):
    conn.execute("UPDATE titles SET refreshed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (id,))
    conn.commit()


# --- Titles ---


def test_search_finds_movies_and_shows_only(tmdb):
    found = service.search(tmdb, "sev")
    assert [(f.kind, f.name, f.year) for f in found] == [
        ("show", "Severance", "2022"),
        ("movie", "Dune: Part Three", SOON[:4]),
    ]
    assert found[0].poster == "https://image.tmdb.org/t/p/w342/sev.jpg"
    assert [f.kind for f in service.search(tmdb, "sev", "show")] == ["show"]


def test_a_rejected_key_says_where_to_fix_it(http):
    with pytest.raises(RemoteError, match="TMDB_API_KEY"):
        service.search(Tmdb("wrong", http), "sev")
    with pytest.raises(RemoteError, match="isn't set"):
        service.search(Tmdb("", http), "sev")


def test_a_show_keeps_what_tmdb_says(conn, tmdb):
    t = add_show(conn, tmdb)
    assert (t.name, t.kind, t.status, t.year, t.runtime) == ("Severance", "show", "to_watch", "2022", 55)
    assert t.seasons == {1: 9, 2: 10}  # specials left out
    assert t.last_aired.code == "S02E10" and t.next_airing is None
    assert t.genres == ["Drama", "Mystery", "Sci-Fi"]
    assert t.unwatched == 19 and t.watched_up_to is None
    assert (t.rating, t.imdb_id) == (8.4, "tt11280740")


def test_a_title_is_added_once(conn, tmdb):
    first = add_show(conn, tmdb)
    with pytest.raises(Conflict, match=f"already on the list, as {first.id}"):
        add_show(conn, tmdb)


def test_an_unknown_tmdb_id_is_an_error(conn, tmdb):
    with pytest.raises(RemoteError, match="nothing"):
        service.add_title(conn, tmdb, 5, "movie")
    assert service.list_titles(conn) == []


def test_movies_are_not_watched_halfway(conn, tmdb):
    movie = service.add_title(conn, tmdb, 1170608, "movie")
    with pytest.raises(ValidationError, match="'to_watch', 'watched', 'dropped'"):
        service.update_title(conn, movie.id, status="watching")
    with pytest.raises(ValidationError, match="only shows have episodes"):
        service.update_title(conn, movie.id, season=1, episode=1)


def test_watching_the_next_episode_starts_the_show(conn, tmdb):
    t = add_show(conn, tmdb)
    t = service.watch_next(conn, t.id)
    assert (t.watched_up_to.code, t.status, t.unwatched) == ("S01E01", "watching", 18)


def test_the_last_episode_of_a_season_leads_to_the_next_season(conn, tmdb):
    t = service.update_title(conn, add_show(conn, tmdb).id, season=1, episode=9)
    assert service.watch_next(conn, t.id).watched_up_to.code == "S02E01"


def test_an_episode_not_aired_yet_cant_be_watched(conn, tmdb, tmdb_data):
    tmdb_data["/3/tv/95396"]["last_episode_to_air"] = {"season_number": 2, "episode_number": 4}
    tmdb_data["/3/tv/95396"]["next_episode_to_air"] = {"season_number": 2, "episode_number": 5, "air_date": SOON}
    t = service.update_title(conn, add_show(conn, tmdb).id, season=2, episode=4)
    assert t.unwatched == 0
    with pytest.raises(ValidationError, match=f"S02E05 hasn't aired yet: it airs {SOON}"):
        service.watch_next(conn, t.id)


def test_catching_up_with_an_ended_show_finishes_it(conn, tmdb, tmdb_data):
    tmdb_data["/3/tv/95396"]["status"] = "Ended"
    t = service.update_title(conn, add_show(conn, tmdb).id, season=2, episode=9)
    t = service.watch_next(conn, t.id)
    assert (t.status, t.unwatched) == ("watched", 0)
    with pytest.raises(ValidationError, match="no episode of Severance after S02E10"):
        service.watch_next(conn, t.id)


def test_progress_must_be_an_episode_of_the_show(conn, tmdb):
    t = add_show(conn, tmdb)
    with pytest.raises(ValidationError, match="no S03E01; episodes per season are season 1: 9, season 2: 10"):
        service.update_title(conn, t.id, season=3, episode=1)
    with pytest.raises(ValidationError, match="give both season and episode"):
        service.update_title(conn, t.id, season=1)


def test_progress_can_be_cleared_and_status_given_with_it(conn, tmdb):
    t = add_show(conn, tmdb)
    t = service.update_title(conn, t.id, season=1, episode=3, status="dropped")
    assert (t.watched_up_to.code, t.status) == ("S01E03", "dropped")
    t = service.update_title(conn, t.id, season=None, episode=None)
    assert t.watched_up_to is None and t.unwatched == 19


def test_a_show_marked_watched_is_caught_up(conn, tmdb):
    t = service.update_title(conn, add_show(conn, tmdb).id, status="watched")
    assert (t.watched_up_to.code, t.unwatched) == ("S02E10", 0)
    t = service.update_title(conn, t.id, status="watched", season=1, episode=4)
    assert t.watched_up_to.code == "S01E04"


def test_unknown_ids_list_the_known_ones(conn, tmdb):
    add_show(conn, tmdb)
    with pytest.raises(NotFound, match=r"\['1 \(Severance\)'\]"):
        service.get_title(conn, 7)


def test_refresh_fetches_only_old_copies_of_titles_that_change(conn, tmdb, tmdb_data):
    show = add_show(conn, tmdb)
    tmdb_data["/3/tv/95396"]["next_episode_to_air"] = {"season_number": 3, "episode_number": 1, "air_date": LATER}
    assert service.refresh(conn, tmdb) == 0  # still fresh
    make_old(conn, show.id)
    assert service.refresh(conn, tmdb) == 1
    assert service.get_title(conn, show.id).next_airing.code == "S03E01"


def test_ended_shows_are_refreshed_less_often(conn, tmdb, tmdb_data):
    tmdb_data["/3/tv/95396"]["status"] = "Ended"
    show = add_show(conn, tmdb)
    old = (date.today() - timedelta(days=2)).isoformat() + "T00:00:00+00:00"
    conn.execute("UPDATE titles SET refreshed_at = ? WHERE id = ?", (old, show.id))
    assert service.refresh(conn, tmdb) == 0
    make_old(conn, show.id)
    assert service.refresh(conn, tmdb) == 1


def test_a_failed_refresh_keeps_the_old_copy(conn, tmdb, tmdb_data):
    show = add_show(conn, tmdb)
    make_old(conn, show.id)
    del tmdb_data["/3/tv/95396"]
    assert service.refresh(conn, tmdb) == 0
    assert service.get_title(conn, show.id).seasons == {1: 9, 2: 10}


def test_summary_lists_episodes_to_catch_up_and_releases(conn, tmdb, tmdb_data):
    tmdb_data["/3/tv/95396"]["next_episode_to_air"] = {"season_number": 3, "episode_number": 1, "air_date": SOON, "name": "Back"}
    show = service.update_title(conn, add_show(conn, tmdb).id, season=2, episode=8)
    service.add_title(conn, tmdb, 1170608, "movie")
    s = service.summary(conn)
    assert s["catch_up"] == [
        {"id": show.id, "name": "Severance", "unwatched": 2, "watched_up_to": "S02E08", "next_to_watch": "S02E09"}
    ]
    assert [(c["kind"], c["date"], c.get("episode")) for c in s["coming"]] == [("movie", SOON, None), ("show", SOON, "S03E01")]
    assert s["counts"] == {"movie": {"to_watch": 1}, "show": {"watching": 1}}
    assert [c["name"] for c in service.summary(conn, days=5)["coming"]] == []


# --- Channels ---


def test_a_channel_is_found_from_its_handle(conn, http):
    c = service.add_channel(conn, http, "@mkbhd")
    # The handle is the page's own, not one it links to.
    assert (c.id, c.name, c.handle, c.thumbnail) == (CHANNEL_ID, "Marques Brownlee & Co", "@mkbhd", "https://yt3.example/mkbhd.jpg")
    assert c.url == "https://www.youtube.com/@mkbhd"


def test_a_channel_is_found_from_one_of_its_videos(conn, http):
    assert service.add_channel(conn, http, "https://www.youtube.com/watch?v=abc").id == CHANNEL_ID
    with pytest.raises(Conflict, match="already followed"):
        service.add_channel(conn, http, "mkbhd")


def test_what_names_a_channel():
    assert youtube.page_url("mkbhd") == "https://www.youtube.com/@mkbhd"
    assert youtube.page_url("@some.name") == "https://www.youtube.com/@some.name"
    assert youtube.page_url(CHANNEL_ID) == f"https://www.youtube.com/channel/{CHANNEL_ID}"
    assert youtube.page_url("youtu.be/abc") == "https://youtu.be/abc"
    with pytest.raises(youtube.YoutubeError, match="isn't a YouTube link"):
        youtube.page_url("https://vimeo.com/123")


def test_an_unknown_handle_is_an_error(conn, http):
    with pytest.raises(RemoteError, match="no page"):
        service.add_channel(conn, http, "@nobody")


def test_channels_are_found_by_id_or_handle(conn, http):
    service.add_channel(conn, http, "@mkbhd")
    assert service.get_channel(conn, "MKBHD").id == CHANNEL_ID
    assert service.remove_channel(conn, "@mkbhd").id == CHANNEL_ID
    with pytest.raises(NotFound, match="no channel"):
        service.get_channel(conn, CHANNEL_ID)


def test_new_uploads_are_recent_and_skip_shorts(conn, http):
    service.add_channel(conn, http, "@mkbhd")
    videos, failed = service.new_uploads(conn, http, days=7)
    assert ([v.id for v in videos], failed) == (["new"], {})
    assert videos[0].url == "https://www.youtube.com/watch?v=new"
    videos, _ = service.new_uploads(conn, http, days=60, shorts=True)
    assert [v.id for v in videos] == ["new", "short", "old"]


def test_a_failing_feed_is_reported_with_the_others_read(conn, http, youtube_data):
    service.add_channel(conn, http, "@mkbhd")
    conn.execute("INSERT INTO channels (id, name, added_at) VALUES ('UCxxxxxxxxxxxxxxxxxxxxxx', 'Gone', 'x')")
    videos, failed = service.new_uploads(conn, http)
    assert [v.id for v in videos] == ["new"]
    assert list(failed) == ["Gone"] and "404" in failed["Gone"]
