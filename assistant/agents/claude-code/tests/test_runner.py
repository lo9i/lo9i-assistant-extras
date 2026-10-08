import os

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
from claude_code_tasks.task_folder import Step, Task, note


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


def test_a_new_task_starts_its_session_and_a_follow_up_resumes_it():
    env = runner.auth_env("sk-ant-api-x")
    new, follow = runner.options(_task(), env), runner.options(_task("t0"), env)
    assert new.session_id == _task().session and new.resume is None
    assert follow.resume == _task().session and follow.session_id is None
    assert new.cwd == "/repo" and new.permission_mode == "bypassPermissions"
    assert new.max_buffer_size == 64 * 1024 * 1024  # a screenshot read whole fits
    assert new.system_prompt == {"type": "preset", "preset": "claude_code", "append": runner._UNDER_LO9I}


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
            ToolResultBlock("2", [{"type": "text", "text": "x" * 3000}]),
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
    assert long.tool == "TodoWrite" and long.text == "x" * 2000 + "\n…"
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


async def test_a_run_reports_the_result_and_keeps_the_token_out_of_the_environment(monkeypatch):
    seen = {}

    async def query(prompt, options):
        seen.update(prompt=prompt, env=options.env)
        yield AssistantMessage(content=[ToolUseBlock("1", "Bash", {"command": "pytest"})], model="claude")
        yield _result()

    monkeypatch.setattr(runner, "query", query)
    monkeypatch.setenv(runner.TOKEN_ENV, "sk-ant-api03-x")
    steps = []
    outcome = await runner.run(_task(), steps.append)
    assert outcome.ok and outcome.text == "Fixed 2 tests." and outcome.cost_usd == 0.42
    assert steps == [Step("tool", tool="Bash", detail="pytest", id="1")] and seen["env"] == {
        "ANTHROPIC_API_KEY": "sk-ant-api03-x"
    }
    assert runner.TOKEN_ENV not in os.environ


async def test_an_error_result_fails_the_task_and_no_token_is_refused(monkeypatch):
    async def query(prompt, options):
        yield _result(is_error=True, text="")

    monkeypatch.setattr(runner, "query", query)
    monkeypatch.setenv(runner.TOKEN_ENV, "sk-ant-api03-x")
    outcome = await runner.run(_task(), lambda _: None)
    assert not outcome.ok and outcome.text == "Claude Code ended with error_during_execution."
    with pytest.raises(RuntimeError, match="No login for Claude Code"):
        await runner.run(_task(), lambda _: None)


async def test_without_a_claude_token_it_runs_on_copilot_through_the_relay(monkeypatch):
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

    async def query(prompt, options):
        seen["env"] = options.env
        yield _result()

    monkeypatch.setattr(runner, "Relay", FakeRelay)
    monkeypatch.setattr(runner, "query", query)
    monkeypatch.setenv(runner.COPILOT_ENV, "ghu_x")
    monkeypatch.delenv(runner.TOKEN_ENV, raising=False)
    steps = []
    outcome = await runner.run(_task(), steps.append)
    assert outcome.ok and outcome.cost_usd is None  # Anthropic's prices aren't what Copilot charges
    assert seen == {"github_token": "ghu_x", "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}}
    assert steps == [note("Using Claude on your GitHub Copilot plan")] and runner.COPILOT_ENV not in os.environ
