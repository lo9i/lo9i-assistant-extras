"""The Mac's Contacts, read with the Contacts framework (pyobjc). The first search asks macOS for access,
which shows the user a prompt; once they decide, macOS remembers it."""

import sys
import threading
from dataclasses import dataclass, field

# How long the first search waits for the user to answer macOS's access prompt.
ACCESS_PROMPT_SECONDS = 120
# The most contacts a search returns.
MAX_MATCHES = 10


class ContactsError(Exception):
    pass


@dataclass(frozen=True)
class PhoneNumber:
    label: str
    number: str


@dataclass(frozen=True)
class Contact:
    name: str
    organization: str = ""
    numbers: list[PhoneNumber] = field(default_factory=list)


def search(name: str) -> list[Contact]:
    """Contacts whose name matches, with their phone numbers; those without a number are left out.
    Raises ContactsError off a Mac or when access is denied."""
    if sys.platform != "darwin":
        raise ContactsError("Contacts are only on a Mac.")
    import Contacts  # macOS only; the dependency is installed only there

    store = Contacts.CNContactStore.alloc().init()
    _require_access(Contacts, store)
    keys = [Contacts.CNContactGivenNameKey, Contacts.CNContactFamilyNameKey, Contacts.CNContactOrganizationNameKey]
    predicate = Contacts.CNContact.predicateForContactsMatchingName_(name)
    found, error = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
        predicate, [*keys, Contacts.CNContactPhoneNumbersKey], None
    )
    if error is not None:
        raise ContactsError(f"Contacts couldn't be searched: {error.localizedDescription()}")
    contacts = [_contact(Contacts, c) for c in found or []]
    return [c for c in contacts if c.numbers][:MAX_MATCHES]


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
    numbers = [
        PhoneNumber(_label(cn, labeled.label()), labeled.value().stringValue()) for labeled in found.phoneNumbers()
    ]
    return Contact(name or found.organizationName(), found.organizationName() if name else "", numbers)


def _label(cn, label: str | None) -> str:
    return str(cn.CNLabeledValue.localizedStringForLabel_(label)) if label else "phone"
