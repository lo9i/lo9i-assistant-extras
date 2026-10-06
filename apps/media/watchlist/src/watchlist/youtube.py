"""YouTube channels: finding one from what the user gives (a handle, a link,
an id), and its latest uploads from the channel's feed, which needs no API
key. The feed lists a channel's last 15 uploads."""

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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
    """The channel's latest uploads, newest first."""
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
