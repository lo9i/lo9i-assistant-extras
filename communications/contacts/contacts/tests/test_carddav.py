"""Contacts on a CardDAV server: a fake one, answering as iCloud's does, so nothing reaches a network."""

import time
from xml.sax.saxutils import escape

import httpx2
import pytest

from lo9i_contacts import carddav
from lo9i_contacts.carddav import CardDav
from lo9i_contacts.contacts import Contact, ContactsError, Labeled

# As iCloud writes them: vCard 3.0, labels on item groups, a birthday without its year saved as 1604.
ANA = """BEGIN:VCARD
VERSION:3.0
N:García;Ana;;;
FN:Ana García
item1.TEL;type=pref:+54 9 11 1234-5678
item1.X-ABLabel:_$!<Mobile>!$_
EMAIL;type=INTERNET;type=HOME;type=pref:ana@example.com
BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-07
item2.ADR;type=HOME:;;Av. Siempre Viva 742;Buenos Aires;;1414;Argen
 tina
item2.X-ABLabel:_$!<Home>!$_
END:VCARD
"""
JUAN = """BEGIN:VCARD
VERSION:4.0
N:Pérez;Juan;;;
TEL;TYPE=work:011 4321-8765
EMAIL:juan@work.example
BDAY:19901201
END:VCARD
"""
ACME = """BEGIN:VCARD
VERSION:3.0
FN:
ORG:Acme\\, Inc.;Sales
EMAIL:hola@acme.com
item1.TEL:+1 555 0100
item1.X-ABLabel:Front desk
END:VCARD
"""


def _multistatus(*responses: str) -> str:
    namespaces = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"'
    return f'<?xml version="1.0"?><d:multistatus {namespaces}>{"".join(responses)}</d:multistatus>'


def _response(href: str, prop: str) -> str:
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{prop}</d:prop></d:propstat></d:response>"


class Server:
    """A CardDAV server for user `me` with password `pw`: its principal found at the root (or only at
    /.well-known/carddav with `well_known_only`), one address book with ANA, JUAN and ACME."""

    def __init__(self, well_known_only: bool = False) -> None:
        self.well_known_only = well_known_only
        self.reports = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("Authorization") != httpx2.BasicAuth("me", "pw")._auth_header:
            return httpx2.Response(401)
        path, body = request.url.path, request.content.decode()
        if request.method == "PROPFIND" and "current-user-principal" in body:
            if path == "/" and self.well_known_only:
                return httpx2.Response(404)
            principal = "<d:current-user-principal><d:href>/123/principal/</d:href></d:current-user-principal>"
            return httpx2.Response(207, text=_multistatus(_response(path, principal)))
        if request.method == "PROPFIND" and path == "/123/principal/":
            home = "<c:addressbook-home-set><d:href>/123/carddav/</d:href></c:addressbook-home-set>"
            return httpx2.Response(207, text=_multistatus(_response(path, home)))
        if request.method == "PROPFIND" and path == "/123/carddav/":
            collection = "<d:resourcetype><d:collection/></d:resourcetype>"
            book = "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype>"
            return httpx2.Response(
                207, text=_multistatus(_response(path, collection), _response("/123/carddav/card/", book))
            )
        if request.method == "REPORT" and path == "/123/carddav/card/":
            self.reports += 1
            cards = [
                _response(f"/123/carddav/card/{i}.vcf", f"<c:address-data>{escape(card)}</c:address-data>")
                for i, card in enumerate((ANA, JUAN, ACME))
            ]
            return httpx2.Response(207, text=_multistatus(*cards))
        return httpx2.Response(404)


def _account(server: Server, password: str = "pw") -> CardDav:
    auth = httpx2.BasicAuth("me", password)
    return CardDav("https://contacts.example.com", auth, transport=httpx2.MockTransport(server))


async def test_contacts_are_found_on_the_account_with_their_labels_birthday_and_addresses():
    account = _account(Server())
    assert await account.search("ana") == [
        Contact(
            "Ana García",
            numbers=[Labeled("mobile", "+54 9 11 1234-5678")],
            emails=[Labeled("home", "ana@example.com")],
            birthday="--03-07",
            addresses=[Labeled("home", "Av. Siempre Viva 742, Buenos Aires, 1414, Argentina")],
        )
    ]
    [juan] = await account.search("Juan Pérez")
    assert (juan.birthday, juan.numbers) == ("1990-12-01", [Labeled("work", "011 4321-8765")])
    [acme] = await account.search("acme")
    assert (acme.name, acme.organization, acme.numbers) == ("Acme, Inc.", "", [Labeled("Front desk", "+1 555 0100")])


async def test_an_email_or_a_number_with_or_without_its_country_code_finds_who_has_it():
    account = _account(Server())
    assert [c.name for c in await account.search("ANA@example.com")] == ["Ana García"]
    assert [c.name for c in await account.search("11 1234 5678")] == ["Ana García"]
    assert [c.name for c in await account.search("+54 11 4321-8765")] == ["Juan Pérez"]
    assert await account.search("345-678") == []  # too few digits to be sure


async def test_the_contacts_are_read_once_and_again_after_a_while(monkeypatch):
    server = Server()
    account = _account(server)
    await account.search("ana")
    await account.search("juan")
    assert server.reports == 1
    later = time.monotonic() + carddav.CACHE_SECONDS + 1
    monkeypatch.setattr(carddav.time, "monotonic", lambda: later)
    await account.search("ana")
    assert server.reports == 2


async def test_a_server_found_only_at_its_well_known_address_still_works():
    assert [c.name for c in await _account(Server(well_known_only=True)).search("ana")] == ["Ana García"]


async def test_a_wrong_password_says_so():
    with pytest.raises(ContactsError, match="refused the user name and password"):
        await _account(Server(), password="wrong").search("ana")


async def test_checking_an_account_signs_in_and_finds_its_books_without_reading_them():
    server = Server()
    await _account(server).check()
    assert server.reports == 0
    with pytest.raises(ContactsError, match="refused the user name and password"):
        await _account(Server(), password="wrong").check()


def test_an_account_needs_all_three_or_none():
    assert carddav.configured("https://contacts.icloud.com", "me@icloud.com", "pw")
    assert not carddav.configured("", "", "")
    with pytest.raises(ContactsError, match="all three"):
        carddav.configured("https://contacts.icloud.com", "", "")
