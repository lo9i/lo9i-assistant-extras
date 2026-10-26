"""WhatsApp has no buttons for a personal account, so lo9i's buttons (an approval's Approve and Reject, a
question's options) are written under the message as numbered options, and the user answers with the number or
the option's name. The messages still waiting for an answer are kept here, so an answer becomes the press lo9i
expects."""

import re
from collections import OrderedDict
from dataclasses import dataclass

# (label, button data), as lo9i sends them.
Buttons = list[list[str]]

HINT = "_Reply with a number._"
# Messages with options kept for answers; older ones can't be answered here any more.
_KEPT = 50
_NUMBER = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s*$")
_NOT_A_LETTER = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class Offer:
    """A message with options: its id, lo9i's text for it (without the options) and its buttons."""

    message: str
    text: str
    buttons: Buttons


@dataclass(frozen=True)
class Pick:
    """An answer to an offer: the pressed button's data."""

    offer: Offer
    data: str


def with_options(text: str, buttons: Buttons | None) -> str:
    if not buttons:
        return text
    lines = [f"*{number}.* {label}" for number, (label, _) in enumerate(buttons, 1)]
    return f"{text}\n\n" + "\n".join(lines) + f"\n{HINT}"


class Choices:
    def __init__(self) -> None:
        self._offers: OrderedDict[str, Offer] = OrderedDict()

    def offer(self, message: str, text: str, buttons: Buttons | None) -> None:
        """A message sent or edited: with buttons it waits for an answer, without them it no longer does."""
        self._offers.pop(message, None)
        if buttons:
            self._offers[message] = Offer(message, text, [list(b) for b in buttons])
            while len(self._offers) > _KEPT:
                self._offers.popitem(last=False)

    def pick(self, answer: str, quoted: str = "") -> Pick | None:
        """The option `answer` picks: in the message it quotes, or else the latest one waiting. None when it
        isn't an answer, so it's an ordinary message. A message is answered once; lo9i sends its options
        again when it wants more (a question with several choices)."""
        offer = self._offers.get(quoted) if quoted else next(reversed(self._offers.values()), None)
        data = _matching(answer, offer.buttons) if offer is not None else None
        if offer is None or data is None:
            return None
        del self._offers[offer.message]
        return Pick(offer, data)


def _matching(answer: str, buttons: Buttons) -> str | None:
    """By number (2), or by name without its marks (approve for ✅ Approve)."""
    if number := _NUMBER.match(answer):
        index = int(number.group(1)) - 1
        return buttons[index][1] if 0 <= index < len(buttons) else None
    wanted = _bare(answer)
    found = [data for label, data in buttons if wanted and _bare(label) == wanted]
    return found[0] if len(found) == 1 else None


def _bare(text: str) -> str:
    return _NOT_A_LETTER.sub(" ", text).strip().casefold()
