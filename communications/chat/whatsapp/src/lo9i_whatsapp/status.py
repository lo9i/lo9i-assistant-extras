"""What the apps show for WhatsApp: whether the account is linked and where to talk to the assistant, or how to
link it, with buttons for a new QR code, a code to type instead, and unlinking. Each item is `kind` (text,
code, copy or action), `label` and `value`."""

from lo9i_whatsapp.link import Snapshot, State

NEW_QR = "new-qr"
LINK_CODE = "link-code"
UNLINK = "unlink"

_SCAN = (
    "Not linked yet. Open the WhatsApp page in the web app and scan its QR code with your phone: "
    "WhatsApp → Settings → Linked devices → Link a device."
)
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
            return [*problem, _item("text", "", _SCAN), *code, *ask]
        case State.EXPIRED:
            return [*problem, _item("text", "", "The QR code expired."), _item("action", "New QR code", NEW_QR)]
        case State.FAILED:
            return [_item("text", "", f"⚠️ {link.problem}")]
        case _:
            return [*problem, _item("text", "", "Connecting to WhatsApp…")]


def _item(kind: str, label: str, value: str) -> dict[str, str]:
    return {"kind": kind, "label": label, "value": value}
