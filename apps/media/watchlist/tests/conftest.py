"""TMDB and YouTube as the tests see them: canned answers over an httpx mock
transport, so the real clients run without the network. Tests change TMDB's
answers through the `tmdb_data` fixture."""

import copy
import json
from datetime import date, timedelta

import httpx
import pytest

from watchlist import db
from watchlist.tmdb import Tmdb

SOON = (date.today() + timedelta(days=10)).isoformat()
LATER = (date.today() + timedelta(days=90)).isoformat()

SHOW = {
    "id": 95396,
    "name": "Severance",
    "overview": "Mark leads a team of office workers.",
    "poster_path": "/sev.jpg",
    "first_air_date": "2022-02-17",
    "genres": [{"name": "Drama"}, {"name": "Mystery"}, {"name": "Sci-Fi"}],
    "status": "Returning Series",
    "episode_run_time": [55],
    "seasons": [
        {"season_number": 0, "episode_count": 3},
        {"season_number": 1, "episode_count": 9},
        {"season_number": 2, "episode_count": 10},
    ],
    "last_episode_to_air": {"season_number": 2, "episode_number": 10, "air_date": "2025-03-20", "name": "Cold Harbor"},
    "next_episode_to_air": None,
    "vote_average": 8.4,
    "external_ids": {"imdb_id": "tt11280740"},
}
MOVIE = {
    "id": 1170608,
    "title": "Dune: Part Three",
    "overview": "Paul's story ends.",
    "poster_path": "/dune.jpg",
    "release_date": SOON,
    "genres": [{"name": "Science Fiction"}],
    "status": "Post Production",
    "runtime": 150,
}
SEARCH = {
    "results": [
        {
            "id": 95396,
            "media_type": "tv",
            "name": "Severance",
            "first_air_date": "2022-02-17",
            "poster_path": "/sev.jpg",
        },
        {"id": 1, "media_type": "person", "name": "Someone"},
        {"id": 1170608, "media_type": "movie", "title": "Dune: Part Three", "release_date": SOON},
    ]
}

CHANNEL_ID = "UCBJycsmduvYEL83R_U4JriQ"
CHANNEL_PAGE = f"""<html><head>
<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">
<meta property="og:title" content="Marques Brownlee &amp; Co">
<meta property="og:image" content="https://yt3.example/mkbhd.jpg">
</head><body><script>var d = {{"canonicalBaseUrl":"/@Waveform","vanityChannelUrl":"http://www.youtube.com/@mkbhd"}};</script></body></html>"""
VIDEO_PAGE = f"""<html><head><link rel="canonical" href="https://www.youtube.com/watch?v=abc"></head>
<body><script>var d = {{"videoDetails":{{"channelId":"{CHANNEL_ID}"}}}};</script></body></html>"""


def feed(*entries: tuple[str, str, str, bool]) -> str:
    """A channel feed with (id, title, published, short) entries."""
    items = "".join(
        f"""<entry><yt:videoId>{vid}</yt:videoId><title>{title}</title>
        <link rel="alternate" href="https://www.youtube.com/{'shorts/' if short else 'watch?v='}{vid}"/>
        <author><name>Marques Brownlee</name></author><published>{published}</published>
        <media:group><media:thumbnail url="https://i.ytimg.com/vi/{vid}/hqdefault.jpg"/></media:group></entry>"""
        for vid, title, published, short in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom"><title>Marques Brownlee</title>{items}</feed>"""


def videos_page(*entries: tuple[str, str, str]) -> str:
    """A channel's Videos page with (id, title, "4 days ago") entries, in the
    shape of YouTube's ytInitialData."""
    lockups = [
        {
            "richItemRenderer": {
                "content": {
                    "lockupViewModel": {
                        "contentId": vid,
                        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                        "metadata": {
                            "lockupMetadataViewModel": {
                                "title": {"content": title},
                                "metadata": {
                                    "contentMetadataViewModel": {
                                        "metadataRows": [
                                            {
                                                "metadataParts": [
                                                    {"text": {"content": "1M"}, "accessibilityLabel": "1M views"},
                                                    {"text": {"content": "?"}, "accessibilityLabel": age},
                                                ]
                                            }
                                        ]
                                    }
                                },
                            }
                        },
                    }
                }
            }
        }
        for vid, title, age in entries
    ]
    data = {"metadata": {"channelMetadataRenderer": {"title": "Marques Brownlee"}}, "contents": lockups}
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


def ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat() + "T12:00:00+00:00"


@pytest.fixture
def tmdb_data():
    """What TMDB answers, by path. Tests may change it."""
    return {
        "/3/tv/95396": copy.deepcopy(SHOW),
        "/3/movie/1170608": copy.deepcopy(MOVIE),
        "/3/search/multi": SEARCH,
        "/3/search/tv": {"results": [r for r in SEARCH["results"] if r["media_type"] == "tv"]},
    }


@pytest.fixture
def youtube_data():
    """What YouTube answers, by path (with the query for feeds)."""
    return {
        "/@mkbhd": CHANNEL_PAGE,
        f"/channel/{CHANNEL_ID}": CHANNEL_PAGE,
        "/watch?v=abc": VIDEO_PAGE,
        f"/feeds/videos.xml?channel_id={CHANNEL_ID}": feed(
            ("new", "New phone", ago(1), False),
            ("short", "A short", ago(2), True),
            ("old", "Old phone", ago(30), False),
        ),
        f"/channel/{CHANNEL_ID}/videos": videos_page(
            ("paged", "Paged phone", "2 days ago"), ("older", "Older", "1 month ago")
        ),
    }


@pytest.fixture
def http(tmdb_data, youtube_data):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.themoviedb.org":
            if request.url.params.get("api_key") != "k":
                return httpx.Response(401)
            body = tmdb_data.get(request.url.path)
            return httpx.Response(200, json=body) if body else httpx.Response(404)
        if request.url.host in ("www.youtube.com", "youtube.com"):
            key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
            body = youtube_data.get(key)
            return httpx.Response(200, text=body) if body else httpx.Response(404)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def tmdb(http):
    return Tmdb("k", http)


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    yield conn
    conn.close()
