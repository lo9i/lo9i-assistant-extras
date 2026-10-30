"""Contacts on a CardDAV server (iCloud, Fastmail, Nextcloud, Google with its own sign-in in google.py, ...), for a
host that isn't a Mac or an account the Mac's Contacts don't have. The server is found the way CardDAV clients find
it (RFC 6764, RFC 6352): the user's principal at the URL given or its /.well-known/carddav, the principal's address
book home, the address books in it; then every vCard in them, read once and kept for CACHE_SECONDS, and searched
here."""

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx2

from lo9i_contacts import vcard
from lo9i_contacts.contacts import MAX_MATCHES, Contact, ContactsError

# How long the contacts read are searched before they're read again.
CACHE_SECONDS = 300
# How many of a number's last digits make it the same number (a subscriber number, without the codes before it).
_SAME_DIGITS = 8
_TIMEOUT = 30
_DAV = "{DAV:}"
_CARD = "{urn:ietf:params:xml:ns:carddav}"
_PRINCIPAL = '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
_HOME = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><c:addressbook-home-set/></d:prop></d:propfind>"
)
_BOOKS = '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
# What a 401 means with a user name and password.
PASSWORD_REFUSED = (
    "The CardDAV server refused the user name and password. iCloud needs an app-specific password "
    "(account.apple.com → Sign-In and Security)."
)
_CARDS = (
    '<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><c:address-data/></d:prop></c:addressbook-query>"
)


class _NotDavError(ContactsError):
    """The URL answered, but not as a CardDAV collection does."""


class _RefusedError(ContactsError):
    """The server refused the credentials (401): what to do about it depends on what they were."""


class CardDav:
    def __init__(
        self,
        url: str,
        auth: httpx2.Auth,
        refused: str = PASSWORD_REFUSED,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """`auth` signs its requests (a user name and password, or a sign-in); `refused` says what to do when the
        server refuses them."""
        self._url = url if url.endswith("/") else url + "/"
        self._auth = auth
        self._refused = refused
        self._transport = transport
        self._read: tuple[float, list[Contact]] | None = None

    async def check(self) -> None:
        """Signs in and finds the account's address books, without reading them: whether the account works.
        Raises ContactsError saying why not."""
        await self._session(self._address_books)

    async def search(self, query: str) -> list[Contact]:
        """Contacts matching `query`: an email address, a phone number or a name. Raises ContactsError."""
        return [c for c in await self._contacts() if matches(c, query.strip())][:MAX_MATCHES]

    async def _contacts(self) -> list[Contact]:
        if self._read is None or time.monotonic() - self._read[0] > CACHE_SECONDS:
            self._read = (time.monotonic(), await self._read_all())
        return self._read[1]

    async def _read_all(self) -> list[Contact]:
        return await self._session(self._all_cards)

    async def _session[T](self, work: Callable[[httpx2.AsyncClient], Awaitable[T]]) -> T:
        try:
            async with httpx2.AsyncClient(
                auth=self._auth, timeout=_TIMEOUT, follow_redirects=True, transport=self._transport
            ) as http:
                return await work(http)
        except _RefusedError:
            raise ContactsError(self._refused) from None
        except httpx2.HTTPError as e:
            raise ContactsError(f"Couldn't reach the CardDAV server at {self._url}: {e}") from e

    async def _all_cards(self, http: httpx2.AsyncClient) -> list[Contact]:
        books = await self._address_books(http)
        return [contact for book in books for contact in await self._cards(http, book)]

    async def _address_books(self, http: httpx2.AsyncClient) -> list[str]:
        return await self._books(http, await self._home(http, await self._principal(http)))

    async def _principal(self, http: httpx2.AsyncClient) -> str:
        for url in (self._url, urljoin(self._url, "/.well-known/carddav")):
            try:
                found = _hrefs(await _dav(http, "PROPFIND", url, _PRINCIPAL, 0), f"{_DAV}current-user-principal")
            except _NotDavError:
                continue
            if found:
                return found[0]
        raise ContactsError(f"The CardDAV server at {self._url} didn't say whose contacts these are. Check its URL.")

    async def _home(self, http: httpx2.AsyncClient, principal: str) -> str:
        found = _hrefs(await _dav(http, "PROPFIND", principal, _HOME, 0), f"{_CARD}addressbook-home-set")
        if not found:
            raise ContactsError(f"The CardDAV server at {self._url} has no address books for this account.")
        return found[0]

    async def _books(self, http: httpx2.AsyncClient, home: str) -> list[str]:
        answer = await _dav(http, "PROPFIND", home, _BOOKS, 1)
        return [
            urljoin(home, href)
            for response in answer.iter(f"{_DAV}response")
            if (href := response.findtext(f"{_DAV}href")) and response.find(f".//{_CARD}addressbook") is not None
        ]

    async def _cards(self, http: httpx2.AsyncClient, book: str) -> list[Contact]:
        answer = await _dav(http, "REPORT", book, _CARDS, 1)
        return vcard.parse("\n".join(data.text or "" for data in answer.iter(f"{_CARD}address-data")))


async def _dav(http: httpx2.AsyncClient, method: str, url: str, body: str, depth: int) -> ET.Element:
    """The multistatus the server answers, with each href in it made absolute. Raises ContactsError for an
    answer that isn't one."""
    headers = {"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"}
    response = await http.request(method, url, content=body, headers=headers)
    if response.status_code == 401:
        raise _RefusedError()
    if response.status_code == 403:
        # Google says here which API to turn on in the user's project, with its link.
        raise ContactsError(f"The CardDAV server refused to answer {method} {url}: {_said(response)}")
    if response.status_code != 207:
        raise _NotDavError(f"The CardDAV server answered {method} {url} with {response.status_code}.")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        raise ContactsError(f"The CardDAV server's answer to {method} {url} couldn't be read: {e}") from e
    for href in root.iter(f"{_DAV}href"):
        href.text = urljoin(str(response.url), (href.text or "").strip())
    return root


def _said(response: httpx2.Response) -> str:
    """The server's own words for a refusal: Google's error message, or the start of what it answered."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message or response.text[:300] or f"HTTP {response.status_code}").strip()


def _hrefs(root: ET.Element, prop: str) -> list[str]:
    return [href.text for found in root.iter(prop) for href in found.iter(f"{_DAV}href") if href.text]


def matches(contact: Contact, query: str) -> bool:
    """`query` is one of its emails, one of its numbers (the same digits, or the same last ones of a number
    written with or without its country code), or part of its name or organization."""
    if "@" in query:
        return any(e.value.lower() == query.lower() for e in contact.emails)
    if _is_number(query):
        return any(_same_number(n.value, query) for n in contact.numbers)
    words = query.lower().split()
    named = f"{contact.name} {contact.organization}".lower()
    return bool(words) and all(word in named for word in words)


def _is_number(query: str) -> bool:
    digits = sum(c.isdigit() for c in query)
    return digits >= 4 and all(c.isdigit() or c in "+-() ." for c in query)


def _same_number(one: str, other: str) -> bool:
    """The same last _SAME_DIGITS digits: a number is written with or without its country code, area code or
    trunk 0. Shorter numbers must be the same digits."""
    a, b = _digits(one), _digits(other)
    if min(len(a), len(b)) < _SAME_DIGITS:
        return a == b
    return a[-_SAME_DIGITS:] == b[-_SAME_DIGITS:]


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number)


def configured(url: str, user: str, password: str) -> bool:
    """Whether the plugin was given a CardDAV account: all three, or none."""
    given = [v.strip() for v in (url, user, password)]
    if any(given) and not all(given):
        raise ContactsError("The CardDAV account needs its URL, user name and password, all three.")
    return all(given)
