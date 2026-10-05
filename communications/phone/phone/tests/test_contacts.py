"""Contacts with a fake Contacts framework, so the tests never touch the user's real contacts."""

import sys
import types

import pytest

from lo9i_phone import contacts

NOT_DETERMINED, DENIED, AUTHORIZED = 0, 2, 3


class _Labeled:
    def __init__(self, label, number):
        self._label, self._number = label, number

    def label(self):
        return self._label

    def value(self):
        return types.SimpleNamespace(stringValue=lambda: self._number)


class _Found:
    def __init__(self, given, family, organization, numbers):
        self.given, self.family, self.organization, self.numbers = given, family, organization, numbers

    def givenName(self):  # noqa: N802 - the framework's names
        return self.given

    def familyName(self):  # noqa: N802
        return self.family

    def organizationName(self):  # noqa: N802
        return self.organization

    def phoneNumbers(self):  # noqa: N802
        return [_Labeled(label, number) for label, number in self.numbers]


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
            return [f for f in found if predicate in f"{f.given} {f.family}"], None

    framework = types.SimpleNamespace(
        CNContactStore=Store,
        CNEntityTypeContacts=0,
        CNAuthorizationStatusNotDetermined=NOT_DETERMINED,
        CNAuthorizationStatusDenied=DENIED,
        CNAuthorizationStatusAuthorized=AUTHORIZED,
        CNContactGivenNameKey="givenName",
        CNContactFamilyNameKey="familyName",
        CNContactOrganizationNameKey="organizationName",
        CNContactPhoneNumbersKey="phoneNumbers",
        CNContact=types.SimpleNamespace(predicateForContactsMatchingName_=lambda name: name),
        CNLabeledValue=types.SimpleNamespace(localizedStringForLabel_=lambda label: label.strip("_$!<>")),
    )
    return framework, asked


@pytest.fixture
def on_a_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def install(framework):
        monkeypatch.setitem(sys.modules, "Contacts", framework)

    return install


ANA = _Found("Ana", "García", "", [("_$!<Mobile>!$_", "+54 9 11 1234-5678"), (None, "4555-1234")])
NO_NUMBER = _Found("Ana", "Sin Teléfono", "", [])


def test_matching_contacts_come_with_their_labelled_numbers(on_a_mac):
    on_a_mac(_framework(AUTHORIZED, [ANA, NO_NUMBER])[0])
    found = contacts.search("Ana")
    assert found == [
        contacts.Contact(
            "Ana García",
            numbers=[contacts.PhoneNumber("Mobile", "+54 9 11 1234-5678"), contacts.PhoneNumber("phone", "4555-1234")],
        )
    ]


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
