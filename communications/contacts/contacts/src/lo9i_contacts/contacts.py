"""The Mac's Contacts, read with the Contacts framework (pyobjc). The first search asks macOS for access,
which shows the user a prompt; once they decide, macOS remembers it."""

import sys
import threading
from dataclasses import dataclass, field

# How long the first search waits for the user to answer macOS's access prompt.
ACCESS_PROMPT_SECONDS = 120
# The most contacts a search returns.
MAX_MATCHES = 10
# NSDateComponentUndefined: a birthday saved without its year.
_UNDEFINED = sys.maxsize


class ContactsError(Exception):
    pass


@dataclass(frozen=True)
class Labeled:
    """A number, an email or an address, with its label (mobile, home, work…)."""

    label: str
    value: str


@dataclass(frozen=True)
class Contact:
    name: str
    organization: str = ""
    numbers: list[Labeled] = field(default_factory=list)
    emails: list[Labeled] = field(default_factory=list)
    # YYYY-MM-DD, or --MM-DD when it was saved without the year; "" when there's none.
    birthday: str = ""
    addresses: list[Labeled] = field(default_factory=list)


def search(query: str) -> list[Contact]:
    """Contacts matching `query`: an email address, a phone number or a name. Raises ContactsError off a
    Mac or when access is denied."""
    if sys.platform != "darwin":
        raise ContactsError("Contacts are only on a Mac.")
    import Contacts  # macOS only; the dependency is installed only there

    store = Contacts.CNContactStore.alloc().init()
    _require_access(Contacts, store)
    found, error = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
        _predicate(Contacts, query.strip()), _keys(Contacts), None
    )
    if error is not None:
        raise ContactsError(f"Contacts couldn't be searched: {error.localizedDescription()}")
    return [_contact(Contacts, c) for c in found or []][:MAX_MATCHES]


def _predicate(cn, query: str):
    if "@" in query:
        return cn.CNContact.predicateForContactsMatchingEmailAddress_(query)
    if _is_number(query):
        number = cn.CNPhoneNumber.phoneNumberWithStringValue_(query)
        return cn.CNContact.predicateForContactsMatchingPhoneNumber_(number)
    return cn.CNContact.predicateForContactsMatchingName_(query)


def _is_number(query: str) -> bool:
    digits = sum(c.isdigit() for c in query)
    return digits >= 4 and all(c.isdigit() or c in "+-() ." for c in query)


def _keys(cn) -> list:
    return [
        cn.CNContactGivenNameKey,
        cn.CNContactFamilyNameKey,
        cn.CNContactOrganizationNameKey,
        cn.CNContactPhoneNumbersKey,
        cn.CNContactEmailAddressesKey,
        cn.CNContactBirthdayKey,
        cn.CNContactPostalAddressesKey,
    ]


def _require_access(cn, store) -> None:
    status = cn.CNContactStore.authorizationStatusForEntityType_(cn.CNEntityTypeContacts)
    if status in (cn.CNAuthorizationStatusAuthorized, getattr(cn, "CNAuthorizationStatusLimited", -1)):
        return
    if status != cn.CNAuthorizationStatusNotDetermined or not _ask_access(cn, store):
        raise ContactsError(
            "No access to Contacts. Allow it in System Settings → Privacy & Security → Contacts, "
            "for the app lo9i was started from."
        )


def _ask_access(cn, store) -> bool:
    """macOS answers on another thread once the user picks Allow or Don't Allow."""
    answered = threading.Event()
    granted: list[bool] = []

    def done(ok: bool, _error: object) -> None:
        granted.append(bool(ok))
        answered.set()

    store.requestAccessForEntityType_completionHandler_(cn.CNEntityTypeContacts, done)
    return answered.wait(ACCESS_PROMPT_SECONDS) and granted[0]


def _contact(cn, found) -> Contact:
    name = " ".join(part for part in (found.givenName(), found.familyName()) if part)
    return Contact(
        name or found.organizationName(),
        found.organizationName() if name else "",
        [Labeled(_label(cn, n.label()), n.value().stringValue()) for n in found.phoneNumbers()],
        [Labeled(_label(cn, e.label()), str(e.value())) for e in found.emailAddresses()],
        _birthday(found.birthday()),
        [Labeled(_label(cn, a.label()), _address(cn, a.value())) for a in found.postalAddresses()],
    )


def _birthday(date) -> str:
    if date is None:
        return ""
    year = date.year()
    day = f"{date.month():02d}-{date.day():02d}"
    return f"--{day}" if year in (None, _UNDEFINED) else f"{year:04d}-{day}"


def _address(cn, address) -> str:
    """On one line: the lines macOS would print it on, joined."""
    style = cn.CNPostalAddressFormatterStyleMailingAddress
    text = str(cn.CNPostalAddressFormatter.stringFromPostalAddress_style_(address, style))
    return ", ".join(line.strip() for line in text.splitlines() if line.strip())


def _label(cn, label: str | None) -> str:
    return str(cn.CNLabeledValue.localizedStringForLabel_(label)) if label else "other"
