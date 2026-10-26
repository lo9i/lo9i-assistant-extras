"""What the apps show for WhatsApp: whether the account is linked and where to talk to the assistant, or the QR
code that links it, with buttons for a new QR code, a code to type instead, and unlinking. Each item is `kind` (text,
code, copy, qr or action), `label` and `value`. A QR item's value is the text it encodes; each app draws it."""

from lo9i_whatsapp.link import Snapshot, State

NEW_QR = "new-qr"
LINK_CODE = "link-code"
UNLINK = "unlink"

_SCAN = "Not linked yet. On your phone: WhatsApp → Settings → Linked devices → Link a device, and scan this code."
_TYPE = "On your phone: WhatsApp → Settings → Linked devices → Link a device → Link with phone number instead."


def items(link: Snapshot, phone_given: bool) -> list[dict[str, str]]:
    """`phone_given`: the user entered their number, so a code to type can be asked for."""
    problem = [_item("text", "", f"⚠️ {link.problem}")] if link.problem else []
    match link.state:
        case State.LINKED:
            return [
                *problem,
                _item("text", "", f"Linked to {link.phone}" + (f" ({link.name})." if link.name else ".")),
                _item("text", "", "Talk to the assistant in your WhatsApp chat with yourself (Message yourself)."),
                _item("action", "Unlink", UNLINK),
            ]
        case State.WAITING:
            code = [_item("code", "Link code", link.code), _item("text", "", _TYPE)] if link.code else []
            ask = [_item("action", "Link with a code instead", LINK_CODE)] if phone_given and not link.code else []
            qr = _item("qr", "Scan with WhatsApp", link.qr)
            return [*problem, _item("text", "", _SCAN), qr, *code, *ask]
        case State.EXPIRED:
            return [*problem, _item("text", "", "The QR code expired."), _item("action", "New QR code", NEW_QR)]
        case State.FAILED:
            return [_item("text", "", f"⚠️ {link.problem}")]
        case _:
            return [*problem, _item("text", "", "Connecting to WhatsApp…")]


def _item(kind: str, label: str, value: str) -> dict[str, str]:
    return {"kind": kind, "label": label, "value": value}
