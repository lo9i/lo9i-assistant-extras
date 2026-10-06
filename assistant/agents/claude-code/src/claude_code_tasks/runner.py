"""Runs a task with Claude Code through the Claude Agent SDK, which ships the Claude Code CLI, so nothing
else needs installing."""

import os
from dataclasses import replace
from typing import Any

from agent_tasks import Outcome, Progress, Task
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from claude_code_tasks.relay import Relay

# The plugin's optional field: a Claude token (`claude setup-token`, from a Pro or Max plan) or an
# Anthropic API key.
TOKEN_ENV = "CLAUDE_TOKEN"
# lo9i's Copilot login when its model is GitHub Copilot (`model_login: copilot` in server.yaml): without
# a Claude token, Claude Code runs on the user's Copilot plan through a relay (relay.py).
COPILOT_ENV = "LO9I_COPILOT_GITHUB_TOKEN"
_OAUTH_PREFIX = "sk-ant-oat"
# Tool inputs that say what a step is about, in the order they're looked for.
_DETAILS = ("command", "file_path", "path", "pattern", "url", "query", "description")
_LINE = 200


async def run(task: Task, progress: Progress) -> Outcome:
    """With the Claude token when there is one, else on the user's Copilot plan."""
    token, github_token = _take(TOKEN_ENV), _take(COPILOT_ENV)
    if token:
        return await _run(task, progress, auth_env(token))
    if not github_token:
        raise RuntimeError(_NO_LOGIN)
    async with Relay(github_token).running() as relay:
        progress("Using Claude on your GitHub Copilot plan")
        outcome = await _run(task, progress, relay.claude_env())
    # Claude Code prices its calls at Anthropic's API rates, which isn't what Copilot charges.
    return replace(outcome, cost_usd=None)


async def _run(task: Task, progress: Progress, env: dict[str, str]) -> Outcome:
    result: ResultMessage | None = None
    async for message in query(prompt=task.prompt, options=options(task, env)):
        for step in steps(message):
            progress(step)
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        return Outcome(ok=False, text="Claude Code ended without a result.")
    text = result.result or f"Claude Code ended with {result.subtype}."
    return Outcome(ok=not result.is_error, text=text, cost_usd=result.total_cost_usd)


def options(task: Task, env: dict[str, str]) -> ClaudeAgentOptions:
    """Claude Code as the user runs it: its own system prompt and the user's and the project's settings
    and CLAUDE.md. It works without asking: the user approved the task when it started."""
    follow_up = bool(task.follow_up_of)
    return ClaudeAgentOptions(
        cwd=task.repository,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        env=env,
        resume=task.session if follow_up else None,
        session_id=None if follow_up else task.session,
    )


def auth_env(token: str) -> dict[str, str]:
    key = "CLAUDE_CODE_OAUTH_TOKEN" if token.startswith(_OAUTH_PREFIX) else "ANTHROPIC_API_KEY"
    return {key: token}


def steps(message: object) -> list[str]:
    """Progress lines for a message: what Claude Code says and each tool it uses."""
    if not isinstance(message, AssistantMessage):
        return []
    return [line for block in message.content if (line := _step(block))]


def _step(block: object) -> str:
    if isinstance(block, ToolUseBlock):
        return f"{block.name}: {_detail(block.input)}".rstrip(": ")
    if isinstance(block, TextBlock) and block.text.strip():
        return _short(block.text.strip().splitlines()[0])
    return ""


def _detail(tool_input: dict[str, Any]) -> str:
    return next((_short(str(tool_input[key])) for key in _DETAILS if key in tool_input), "")


def _short(text: str) -> str:
    return text if len(text) <= _LINE else text[:_LINE] + "…"


_NO_LOGIN = (
    "No login for Claude Code: set a Claude token in the plugin's fields in Plugins, or use GitHub Copilot "
    "as the assistant's model (Settings → Model) to run it on your Copilot plan."
)


def _take(name: str) -> str:
    """Out of the environment the agent's commands inherit: logins reach Claude Code only as its login."""
    return os.environ.pop(name, "").strip()
