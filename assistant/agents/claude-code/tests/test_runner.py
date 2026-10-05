import os

import pytest
from agent_tasks import Task
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from claude_code_tasks import runner


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
    new, follow = runner.options(_task(), "sk-ant-api-x"), runner.options(_task("t0"), "sk-ant-api-x")
    assert new.session_id == _task().session and new.resume is None
    assert follow.resume == _task().session and follow.session_id is None
    assert new.cwd == "/repo" and new.permission_mode == "bypassPermissions"
    assert new.system_prompt == {"type": "preset", "preset": "claude_code"}


def test_a_plan_token_and_an_api_key_go_in_their_own_variables():
    assert runner.auth_env("sk-ant-oat01-x") == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    assert runner.auth_env("sk-ant-api03-x") == {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}


def test_progress_shows_what_it_says_and_each_tool_it_uses():
    message = AssistantMessage(
        content=[
            TextBlock("Let me look at the failing test.\nMore detail."),
            ToolUseBlock("1", "Bash", {"command": "pytest -q", "description": "Run tests"}),
            ToolUseBlock("2", "Edit", {"file_path": "/repo/app.py", "old_string": "a", "new_string": "b"}),
            ToolUseBlock("3", "TodoWrite", {"todos": []}),
        ],
        model="claude",
    )
    assert runner.steps(message) == [
        "Let me look at the failing test.",
        "Bash: pytest -q",
        "Edit: /repo/app.py",
        "TodoWrite",
    ]
    assert runner.steps(_result()) == []


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
    assert steps == ["Bash: pytest"] and seen["env"] == {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}
    assert runner.TOKEN_ENV not in os.environ


async def test_an_error_result_fails_the_task_and_no_token_is_refused(monkeypatch):
    async def query(prompt, options):
        yield _result(is_error=True, text="")

    monkeypatch.setattr(runner, "query", query)
    monkeypatch.setenv(runner.TOKEN_ENV, "sk-ant-api03-x")
    outcome = await runner.run(_task(), lambda _: None)
    assert not outcome.ok and outcome.text == "Claude Code ended with error_during_execution."
    with pytest.raises(RuntimeError, match="No Claude token"):
        await runner.run(_task(), lambda _: None)
