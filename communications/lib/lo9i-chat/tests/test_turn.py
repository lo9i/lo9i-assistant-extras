"""How a run looks in a chat: status messages for tools, the streamed reply, approval buttons."""

import asyncio

from lo9i_chat.events import ApprovalRequired, Done, Error, TextDelta, ToolFinished, ToolStarted, Transcribed
from lo9i_chat.turn import ChatTurn
from tests.fakes import LIMITS, FakeChat


async def _events():
    yield Transcribed("busca la documentación", "es")
    yield ToolStarted("web_search", {"query": "langgraph"})
    yield ToolFinished("web_search", "results")
    for word in ["The package is ", "**langgraph-checkpoint-sqlite**."]:
        await asyncio.sleep(0)
        yield TextDelta(word)
    yield ToolStarted("bash", {"command": "rm -rf build"})
    yield ApprovalRequired("abc", {"tool": "bash", "command": "rm -rf build", "reason": "recursive delete"})
    yield Error("boom")
    yield Done()


async def test_rendering_order_and_final_markdown():
    chat = FakeChat()
    turn = ChatTurn(chat, LIMITS)
    await turn.render(_events())
    assert turn.final_text == ""  # the reply came before the last tool call
    assert chat.messages[1] == "🎤 I heard (es): busca la documentación"
    assert chat.messages[2] == "✓ web_search(langgraph)"
    assert chat.messages[3] == "The package is **langgraph-checkpoint-sqlite**."
    assert ("edit", 3, chat.messages[3], True) in chat.log
    assert chat.messages[4] == "⏺ bash(rm -rf build)"  # a new status message below the reply
    approval = next(entry for entry in chat.log if entry[0] == "send" and entry[4])
    assert approval[2] == "Approve bash? (recursive delete)\n\nrm -rf build"
    assert approval[4] == [("✅ Approve", "a:abc"), ("❌ Reject", "r:abc")]
    assert list(chat.messages.values())[-1] == "⚠️ boom"


async def test_the_final_text_is_the_answer_after_the_last_tool():
    async def events():
        yield TextDelta("Let me check. ")
        yield ToolStarted("web_search", {"query": "x"})
        yield ToolFinished("web_search", "results")
        yield TextDelta("It's sunny.")
        yield Done()

    turn = ChatTurn(FakeChat(), LIMITS)
    await turn.render(events())
    assert turn.final_text == "It's sunny."
