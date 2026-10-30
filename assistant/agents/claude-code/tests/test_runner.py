import asyncio
import json
import os
import signal

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_code_tasks import runner
from claude_code_tasks.task_folder import Context, Inbox, Step, Task, note


def _task(follow_up_of=""):
    return Task("t1", "/repo", "fix the tests", "6f1c8e2a-0000-4000-8000-000000000000", follow_up_of, "2026-10-04")


def _result(is_error=False, text="Fixed 2 tests."):
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=3,
        session_id="s",
        total_cost_usd=0.42,
        result=text,
    )


class FakeClient:
    """Claude Code's side of the session. `replies(text)` is what it sends back for each message it's sent:
    it echoes the message first, as with replay-user-messages; an interrupt ends the turn with an error."""

    def __init__(self, replies, context=None):
        self._replies = replies
        self._context = context
        self._out: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.interrupts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text):
        self.sent.append(text)
        for message in [UserMessage(text), *self._replies(text)]:
            self._out.put_nowait(message)

    async def interrupt(self):
        self.interrupts += 1
        stopped = ToolResultBlock("1", "The user doesn't want to proceed with this tool use.", is_error=True)
        self._out.put_nowait(UserMessage([stopped]))
        self._out.put_nowait(_result(is_error=True, text=""))

    async def receive_messages(self):
        while True:
            yield await self._out.get()

    async def get_context_usage(self):
        if self._context is None:
            raise Exception("Unknown control request: get_context_usage")
        return self._context


@pytest.fixture
async def inbox(tmp_path):
    opened = Inbox(tmp_path)
    opened.open()
    yield opened
    opened.close()


def _send(folder, name, message):
    """As lo9i does: the message's file in inbox/, then SIGUSR1."""
    (folder / "inbox" / f"{name}.json").write_text(json.dumps(message))
    os.kill(os.getpid(), signal.SIGUSR1)


def _fake(monkeypatch, replies, context=None):
    seen = {}

    def client(options):
        seen["options"] = options
        seen["client"] = FakeClient(replies, context)
        return seen["client"]

    monkeypatch.setattr(runner, "ClaudeSDKClient", client)
    monkeypatch.setenv(runner.TOKEN_ENV, "sk-ant-api03-x")
    return seen


def test_a_new_task_starts_its_session_and_a_follow_up_resumes_it():
    env = runner.auth_env("sk-ant-api-x")
    new, follow = runner.options(_task(), env), runner.options(_task("t0"), env)
    assert new.session_id == _task().session and new.resume is None
    assert follow.resume == _task().session and follow.session_id is None
    assert new.cwd == "/repo" and new.permission_mode == "bypassPermissions"
    assert new.max_buffer_size == 64 * 1024 * 1024  # a screenshot read whole fits
    assert new.system_prompt == {"type": "preset", "preset": "claude_code", "append": runner._UNDER_LO9I}
    assert new.extra_args == {"replay-user-messages": None}  # each message it takes comes back


def test_a_plan_token_and_an_api_key_go_in_their_own_variables():
    assert runner.auth_env("sk-ant-oat01-x") == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    assert runner.auth_env("sk-ant-api03-x") == {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}


def test_steps_are_its_thinking_what_it_says_and_each_tool_with_its_result():
    steps = runner.Steps()
    said = AssistantMessage(
        content=[
            ThinkingBlock("The test expects a list.", "sig"),
            TextBlock("Let me look at the failing test.\nMore detail."),
            ToolUseBlock("1", "Bash", {"command": "pytest -q", "description": "Run tests"}),
            ToolUseBlock("2", "TodoWrite", {"todos": []}),
        ],
        model="claude",
    )
    results = UserMessage(
        content=[
            ToolResultBlock("1", "1 failed", is_error=True),
            ToolResultBlock("2", [{"type": "text", "text": "x" * 100_010}]),
        ]
    )
    assert steps.of(said) == [
        Step("thinking", "The test expects a list."),
        Step("text", "Let me look at the failing test.\nMore detail."),
        Step("tool", tool="Bash", detail="pytest -q", id="1"),
        Step("tool", tool="TodoWrite", id="2"),
    ]
    failed, long = steps.of(results)
    assert failed == Step("result", "Error: 1 failed", "Bash", id="1")
    assert long.tool == "TodoWrite" and long.text == "x" * 100_000 + "\n…"
    assert steps.of(_result()) == []


def test_words_stream_as_deltas_before_their_whole_block():
    def event(delta, parent=None):
        return StreamEvent("u", "s", {"type": "content_block_delta", "index": 0, "delta": delta}, parent)

    steps = runner.Steps()
    assert steps.of(event({"type": "text_delta", "text": "Run"})) == [Step("text_delta", "Run")]
    assert steps.of(event({"type": "thinking_delta", "thinking": "hm"}, "a1")) == [
        Step("thinking_delta", "hm", within="a1")
    ]
    assert steps.of(event({"type": "input_json_delta", "partial_json": "{"})) == []
    assert steps.of(StreamEvent("u", "s", {"type": "message_stop"})) == []
    assert runner.options(_task(), runner.auth_env("sk-ant-api-x")).include_partial_messages


def test_a_subagents_steps_name_the_call_that_started_it():
    message = AssistantMessage(
        content=[ToolUseBlock("9", "Read", {"file_path": "/repo/a.py"})], model="c", parent_tool_use_id="5"
    )
    assert runner.Steps().of(message) == [Step("tool", tool="Read", detail="/repo/a.py", id="9", within="5")]


def test_thinking_is_shown_as_a_summary():
    options = runner.options(_task(), runner.auth_env("sk-ant-api-x"))
    assert options.thinking == {"type": "adaptive", "display": "summarized"}


async def test_a_run_reports_the_result_and_keeps_the_token_out_of_the_environment(monkeypatch, inbox):
    seen = _fake(
        monkeypatch, lambda text: [AssistantMessage([ToolUseBlock("1", "Bash", {"command": "pytest"})], "c"), _result()]
    )
    steps = []
    outcome = await runner.run(_task(), steps.append, inbox)
    assert outcome.ok and outcome.text == "Fixed 2 tests." and outcome.cost_usd == 0.42
    assert seen["client"].sent == ["fix the tests"]
    assert steps == [Step("tool", tool="Bash", detail="pytest", id="1")] and seen["options"].env == {
        "ANTHROPIC_API_KEY": "sk-ant-api03-x"
    }
    assert runner.TOKEN_ENV not in os.environ


async def test_the_outcome_says_how_full_its_context_is_once_it_answered(monkeypatch, inbox):
    usage = {"totalTokens": 61_000, "maxTokens": 167_000, "rawMaxTokens": 200_000, "percentage": 30.5}
    _fake(monkeypatch, lambda text: [_result()], context=usage)
    outcome = await runner.run(_task(), lambda _: None, inbox)
    assert outcome.ok and outcome.context == Context(tokens=61_000, window=200_000)


async def test_an_answer_stands_when_claude_code_cant_say_how_full_its_context_is(monkeypatch, inbox, capsys):
    _fake(monkeypatch, lambda text: [_result()])
    outcome = await runner.run(_task(), lambda _: None, inbox)
    assert outcome.ok and outcome.text == "Fixed 2 tests." and outcome.context is None
    assert "Couldn't read Claude Code's context use" in capsys.readouterr().err


async def test_an_error_result_fails_the_task_and_no_token_is_refused(monkeypatch, inbox):
    _fake(monkeypatch, lambda text: [_result(is_error=True, text="")])
    outcome = await runner.run(_task(), lambda _: None, inbox)
    assert not outcome.ok and outcome.text == "Claude Code ended with error_during_execution."
    with pytest.raises(RuntimeError, match="No login for Claude Code"):
        await runner.run(_task(), lambda _: None, inbox)


async def test_a_message_sent_while_it_works_goes_to_it_and_it_ends_once_it_answered_it(monkeypatch, inbox, tmp_path):
    def replies(text):
        if text == "fix the tests":
            return [AssistantMessage([ToolUseBlock("1", "Bash", {"command": "pytest"})], "c"), _result()]
        return [AssistantMessage([TextBlock("Fixed, and the docs too.")], "c"), _result(text="Docs too.")]

    seen = _fake(monkeypatch, replies)
    steps = []

    def progress(step):
        steps.append(step)
        if step.kind == "tool":
            _send(tmp_path, "0001", {"kind": "message", "text": "and update the docs"})

    outcome = await runner.run(_task(), progress, inbox)
    assert seen["client"].sent == ["fix the tests", "and update the docs"]
    assert steps == [
        Step("tool", tool="Bash", detail="pytest", id="1"),
        Step("user", "and update the docs", id="0001", waiting=True),
        Step("taken", id="0001"),  # Claude Code echoed it: it got to it
        Step("text", "Fixed, and the docs too."),
    ]
    assert outcome.ok and outcome.text == "Docs too."


async def test_a_message_claude_code_hasnt_got_to_stays_waiting(monkeypatch, inbox, tmp_path):
    """Claude Code takes a message once it's done with the turn in progress: until it echoes it, there's no `taken`
    step, so lo9i sends it again if the user stops the task."""

    class Busy(FakeClient):
        async def query(self, text):
            if text == "fix the tests":
                await super().query(text)
            else:
                self.sent.append(text)  # held until its turn ends, which it never does

    def client(options):
        return Busy(lambda text: [AssistantMessage([ToolUseBlock("1", "Bash", {"command": "sleep 600"})], "c")])

    monkeypatch.setattr(runner, "ClaudeSDKClient", client)
    monkeypatch.setenv(runner.TOKEN_ENV, "sk-ant-api03-x")
    steps = []

    def progress(step):
        steps.append(step)
        if step.kind == "tool":
            _send(tmp_path, "0001", {"kind": "message", "text": "and update the docs"})

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runner.run(_task(), progress, inbox), 0.3)
    assert steps == [
        Step("tool", tool="Bash", detail="sleep 600", id="1"),
        Step("user", "and update the docs", id="0001", waiting=True),
    ]


async def test_an_interrupt_stops_the_step_and_it_waits_for_the_next_message(monkeypatch, inbox, tmp_path):
    def replies(text):
        if text == "fix the tests":
            return [AssistantMessage([ToolUseBlock("1", "Bash", {"command": "sleep 600"})], "c")]  # still working
        return [AssistantMessage([TextBlock("Done another way.")], "c"), _result(text="Another way.")]

    seen = _fake(monkeypatch, replies)
    steps = []

    def progress(step):
        steps.append(step)
        if step.kind == "tool":
            _send(tmp_path, "0001", {"kind": "interrupt"})
        elif step == note(runner._INTERRUPTED):
            # It waits rather than ending with the interrupted turn.
            asyncio.get_running_loop().call_later(
                0.05, _send, tmp_path, "0002", {"kind": "message", "text": "do it so"}
            )

    outcome = await runner.run(_task(), progress, inbox)
    assert seen["client"].interrupts == 1 and seen["client"].sent == ["fix the tests", "do it so"]
    assert steps == [
        Step("tool", tool="Bash", detail="sleep 600", id="1"),
        note(runner._INTERRUPTED),
        Step("result", "Interrupted", "Bash", id="1"),
        Step("user", "do it so", id="0002", waiting=True),
        Step("taken", id="0002"),
        Step("text", "Done another way."),
    ]
    assert outcome.ok and outcome.text == "Another way."


async def test_without_a_claude_token_it_runs_on_copilot_through_the_relay(monkeypatch, inbox):
    seen = {}

    class FakeRelay:
        def __init__(self, github_token):
            seen["github_token"] = github_token

        def running(self):
            relay = self

            class _Running:
                async def __aenter__(self):
                    return relay

                async def __aexit__(self, *exc):
                    return False

            return _Running()

        def claude_env(self):
            return {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}

    def client(options):
        seen["env"] = options.env
        return FakeClient(lambda text: [_result()])

    monkeypatch.setattr(runner, "Relay", FakeRelay)
    monkeypatch.setattr(runner, "ClaudeSDKClient", client)
    monkeypatch.setenv(runner.COPILOT_ENV, "ghu_x")
    monkeypatch.delenv(runner.TOKEN_ENV, raising=False)
    steps = []
    outcome = await runner.run(_task(), steps.append, inbox)
    assert outcome.ok and outcome.cost_usd is None  # Anthropic's prices aren't what Copilot charges
    assert seen == {"github_token": "ghu_x", "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}}
    assert steps == [note("Using Claude on your GitHub Copilot plan")] and runner.COPILOT_ENV not in os.environ
