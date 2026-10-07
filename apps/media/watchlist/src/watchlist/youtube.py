"""YouTube channels: finding one from what the user gives (a handle, a link,
an id), and its latest uploads, which need no API key.

Uploads come from the channel's feed, which has their exact times and its
last 15 uploads. YouTube's feeds fail often, though (404 or 500 for hours,
for some channels and not others), so when one does they're read from the
channel's Videos page instead: its last 30 uploads, without Shorts, whose
times are as near as the page says ("4 days ago").
"""

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from .models import Video

FEED = "https://www.youtube.com/feeds/videos.xml"
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept-Language": "en"}
# The cookie skips the consent page YouTube shows some countries first.
PAGE_HEADERS = {**HEADERS, "Cookie": "SOCS=CAI"}
# How long ago an upload was, as a channel's Videos page says it: "4 days ago".
AGO = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago")
UNIT = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


class YoutubeError(Exception):
    pass


@dataclass(frozen=True)
class Found:
    id: str
    name: str
    handle: str | None
    thumbnail: str | None


def page_url(text: str) -> str:
    """The page to read a channel from: "@mkbhd", "mkbhd", "UC...", or a
    link to a channel or one of its videos."""
    text = text.strip()
    if not text:
        raise YoutubeError("give a channel: its @handle, a link to it or to one of its videos, or its UC… id")
    if CHANNEL_ID.match(text):
        return f"https://www.youtube.com/channel/{text}"
    if text.startswith("@") or not re.search(r"[/.]", text):
        return f"https://www.youtube.com/@{text.removeprefix('@')}"
    url = text if "://" in text else f"https://{text}"
    host = urlparse(url).hostname or ""
    if host != "youtu.be" and not (host == "youtube.com" or host.endswith(".youtube.com")):
        raise YoutubeError(f'"{text}" isn\'t a YouTube link')
    return url


def _first(patterns: list[str], page: str) -> str | None:
    for p in patterns:
        if m := re.search(p, page):
            return html.unescape(m.group(1))
    return None


def _fetch(http: httpx.Client, url: str) -> str:
    try:
        r = http.get(url, headers=PAGE_HEADERS, follow_redirects=True, timeout=15)
    except httpx.HTTPError as e:
        raise YoutubeError(f"couldn't reach YouTube: {e}") from None
    if r.status_code == 404:
        raise YoutubeError(f"YouTube has no page at {url}")
    if r.status_code != 200:
        raise YoutubeError(f"YouTube answered {r.status_code} for {url}")
    return r.text


def resolve(text: str, http: httpx.Client) -> Found:
    """The channel `text` names. A video's link gives its channel."""
    url = page_url(text)
    page = _fetch(http, url)
    canonical = _first([r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'], page)
    if canonical is None:
        # A video's page: read its channel's.
        owner = _first([r'"channelId":"(UC[\w-]{22})"'], page)
        if owner is None:
            raise YoutubeError(f"couldn't find a YouTube channel at {url}")
        page = _fetch(http, f"https://www.youtube.com/channel/{owner}")
        canonical = _first([r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'], page)
        canonical = canonical or owner
    name = _first([r'<meta property="og:title" content="([^"]*)"', r"<title>([^<]*?)(?: - YouTube)?</title>"], page)
    handle = _first([r'"vanityChannelUrl":"https?://www\.youtube\.com/(@[^"]+)"'], page)
    if handle is None and text.strip().startswith("@"):
        handle = text.strip()
    thumbnail = _first([r'<meta property="og:image" content="([^"]*)"'], page)
    return Found(canonical, name or canonical, handle, thumbnail)


def uploads(channel_id: str, http: httpx.Client) -> list[Video]:
    """The channel's latest uploads, newest first: from its feed, or its
    Videos page when the feed fails."""
    try:
        return _feed_uploads(channel_id, http)
    except YoutubeError as feed_error:
        try:
            return _page_uploads(channel_id, http)
        except YoutubeError as page_error:
            raise YoutubeError(f"{feed_error}; its Videos page failed too: {page_error}") from None


def _feed_uploads(channel_id: str, http: httpx.Client) -> list[Video]:
    url = f"{FEED}?channel_id={channel_id}"
    try:
        r = http.get(url, headers=HEADERS, timeout=15)
    except httpx.HTTPError as e:
        raise YoutubeError(f"couldn't reach YouTube: {e}") from None
    if r.status_code != 200:
        raise YoutubeError(f"YouTube's feed answered {r.status_code} for channel {channel_id}")
    try:
        feed = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise YoutubeError(f"YouTube's feed for channel {channel_id} isn't readable: {e}") from None
    channel = feed.findtext("atom:title", "", NS)
    videos = []
    for entry in feed.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", "", NS)
        link = entry.find("atom:link[@rel='alternate']", NS)
        href = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={video_id}"
        thumb = entry.find("media:group/media:thumbnail", NS)
        videos.append(
            Video(
                id=video_id,
                channel_id=channel_id,
                channel=entry.findtext("atom:author/atom:name", channel, NS),
                title=entry.findtext("atom:title", "", NS),
                url=href,
                published=entry.findtext("atom:published", "", NS),
                thumbnail=thumb.get("url") if thumb is not None else None,
                short="/shorts/" in href,
            )
        )
    return sorted(videos, key=lambda v: v.published, reverse=True)


def _page_uploads(channel_id: str, http: httpx.Client) -> list[Video]:
    """From the data the channel's Videos page is drawn from (ytInitialData)."""
    page = _fetch(http, f"https://www.youtube.com/channel/{channel_id}/videos")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if m is None:
        raise YoutubeError(f"the Videos page of channel {channel_id} has no list of videos")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise YoutubeError(f"the Videos page of channel {channel_id} isn't readable: {e}") from None
    channel = data.get("metadata", {}).get("channelMetadataRenderer", {}).get("title") or channel_id
    now = datetime.now(UTC)
    videos = []
    for lockup in _find(data, "lockupViewModel"):
        if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO" or not lockup.get("contentId"):
            continue
        meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
        labels = [part.get("accessibilityLabel", "") for parts in _find(meta, "metadataParts") for part in parts]
        age = next((m for label in labels if (m := AGO.search(label))), None)
        if age is None:
            continue
        video_id = lockup["contentId"]
        videos.append(
            Video(
                id=video_id,
                channel_id=channel_id,
                channel=channel,
                title=meta.get("title", {}).get("content", ""),
                url=f"https://www.youtube.com/watch?v={video_id}",
                published=(now - int(age.group(1)) * UNIT[age.group(2)]).isoformat(timespec="seconds"),
                thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                short=False,
            )
        )
    return sorted(videos, key=lambda v: v.published, reverse=True)


def _find(data: object, key: str) -> list:
    """Every value under `key`, anywhere in decoded JSON."""
    found = []
    stack = [data]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if key in o:
                found.append(o[key])
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return found
