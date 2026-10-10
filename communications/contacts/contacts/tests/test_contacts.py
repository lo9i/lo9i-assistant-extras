"""Contacts with a fake Contacts framework, so the tests never touch the user's real contacts."""

import sys
import types

import pytest

from lo9i_contacts import contacts
from lo9i_contacts.contacts import Contact, Labeled

NOT_DETERMINED, DENIED, AUTHORIZED = 0, 2, 3


class _Labeled:
    def __init__(self, label, value):
        self._label, self._value = label, value

    def label(self):
        return self._label

    def value(self):
        return self._value


class _Date:
    def __init__(self, year, month, day):
        self._year, self._month, self._day = year, month, day

    def year(self):
        return self._year

    def month(self):
        return self._month

    def day(self):
        return self._day


class _Found:
    def __init__(self, given, family, organization="", numbers=(), emails=(), birthday=None, addresses=()):
        self.given, self.family, self.organization = given, family, organization
        self.numbers, self.emails, self.date, self.addresses = numbers, emails, birthday, addresses

    def givenName(self):  # noqa: N802 - the framework's names
        return self.given

    def familyName(self):  # noqa: N802
        return self.family

    def organizationName(self):  # noqa: N802
        return self.organization

    def phoneNumbers(self):  # noqa: N802
        return [_Labeled(label, types.SimpleNamespace(stringValue=lambda n=n: n)) for label, n in self.numbers]

    def emailAddresses(self):  # noqa: N802
        return [_Labeled(label, email) for label, email in self.emails]

    def birthday(self):
        return self.date

    def postalAddresses(self):  # noqa: N802
        return [_Labeled(label, address) for label, address in self.addresses]


def _matches(found, predicate):
    kind, value = predicate
    if kind == "email":
        return any(value == email for _, email in found.emails)
    if kind == "phone":
        return any(value == number for _, number in found.numbers)
    return value in f"{found.given} {found.family}"


def _framework(status, found, grant=True):
    asked = []

    class Store:
        @staticmethod
        def authorizationStatusForEntityType_(_kind):  # noqa: N802
            return status

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def requestAccessForEntityType_completionHandler_(self, _kind, done):  # noqa: N802
            asked.append(True)
            done(grant, None)

        def unifiedContactsMatchingPredicate_keysToFetch_error_(self, predicate, keys, _error):  # noqa: N802
            return [f for f in found if _matches(f, predicate)], None

    contact = types.SimpleNamespace(
        predicateForContactsMatchingName_=lambda name: ("name", name),
        predicateForContactsMatchingEmailAddress_=lambda email: ("email", email),
        predicateForContactsMatchingPhoneNumber_=lambda number: ("phone", number),
    )
    framework = types.SimpleNamespace(
        CNContactStore=Store,
        CNEntityTypeContacts=0,
        CNAuthorizationStatusNotDetermined=NOT_DETERMINED,
        CNAuthorizationStatusDenied=DENIED,
        CNAuthorizationStatusAuthorized=AUTHORIZED,
        CNContact=contact,
        CNPhoneNumber=types.SimpleNamespace(phoneNumberWithStringValue_=lambda number: number),
        CNLabeledValue=types.SimpleNamespace(localizedStringForLabel_=lambda label: label.strip("_$!<>")),
        CNPostalAddressFormatter=types.SimpleNamespace(stringFromPostalAddress_style_=lambda address, _style: address),
        CNPostalAddressFormatterStyleMailingAddress=0,
        **{
            f"CNContact{key}Key": key
            for key in ("GivenName", "FamilyName", "OrganizationName", "PhoneNumbers", "EmailAddresses", "Birthday")
        },
        CNContactPostalAddressesKey="PostalAddresses",
    )
    return framework, asked


@pytest.fixture
def on_a_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def install(framework):
        monkeypatch.setitem(sys.modules, "Contacts", framework)

    return install


ANA = _Found(
    "Ana",
    "García",
    numbers=[("_$!<Mobile>!$_", "+54 9 11 1234-5678")],
    emails=[("_$!<Home>!$_", "ana@example.com")],
    birthday=_Date(sys.maxsize, 3, 7),
    addresses=[("_$!<Home>!$_", "Av. Siempre Viva 742\nBuenos Aires\nArgentina")],
)
ACME = _Found("", "", "Acme", emails=[(None, "hola@acme.com")], birthday=None)


def test_a_contact_comes_with_its_numbers_emails_birthday_and_addresses(on_a_mac):
    on_a_mac(_framework(AUTHORIZED, [ANA, ACME])[0])
    assert contacts.search("Ana") == [
        Contact(
            "Ana García",
            numbers=[Labeled("Mobile", "+54 9 11 1234-5678")],
            emails=[Labeled("Home", "ana@example.com")],
            birthday="--03-07",
            addresses=[Labeled("Home", "Av. Siempre Viva 742, Buenos Aires, Argentina")],
        )
    ]


def test_an_email_or_a_phone_number_finds_who_has_it(on_a_mac):
    on_a_mac(_framework(AUTHORIZED, [ANA, ACME])[0])
    assert [c.name for c in contacts.search("hola@acme.com")] == ["Acme"]
    assert [c.name for c in contacts.search("+54 9 11 1234-5678")] == ["Ana García"]
    assert contacts.search("hola@acme.com")[0].emails == [Labeled("other", "hola@acme.com")]


def test_a_birthday_with_its_year_is_a_full_date(on_a_mac):
    on_a_mac(_framework(AUTHORIZED, [_Found("Juan", "Pérez", birthday=_Date(1990, 12, 1))])[0])
    assert contacts.search("Juan")[0].birthday == "1990-12-01"


def test_the_first_search_asks_for_access(on_a_mac):
    framework, asked = _framework(NOT_DETERMINED, [ANA])
    on_a_mac(framework)
    assert [c.name for c in contacts.search("Ana")] == ["Ana García"] and asked == [True]


def test_denied_access_says_where_to_allow_it(on_a_mac):
    on_a_mac(_framework(NOT_DETERMINED, [ANA], grant=False)[0])
    with pytest.raises(contacts.ContactsError, match="Privacy & Security → Contacts"):
        contacts.search("Ana")
    on_a_mac(_framework(DENIED, [ANA])[0])
    with pytest.raises(contacts.ContactsError, match="No access to Contacts"):
        contacts.search("Ana")


def test_off_a_mac_there_are_no_contacts(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(contacts.ContactsError, match="only on a Mac"):
        contacts.search("Ana")
