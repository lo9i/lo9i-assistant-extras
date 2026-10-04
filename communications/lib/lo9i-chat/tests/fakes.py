"""A chat that records what a turn does with it."""

from lo9i_chat.turn import Limits

LIMITS = Limits(edit_interval=0, stream_limit=3500, chunk=3500)


class FakeChat:
    def __init__(self):
        self.messages, self.log, self._next = {}, [], 0

    async def send(self, text, *, markdown=False, buttons=None):
        self._next += 1
        self.messages[self._next] = text
        self.log.append(("send", self._next, text, markdown, buttons))
        return self._next

    async def edit(self, message_id, text, *, markdown=False):
        self.messages[message_id] = text
        self.log.append(("edit", message_id, text, markdown))

    async def typing(self):
        pass
