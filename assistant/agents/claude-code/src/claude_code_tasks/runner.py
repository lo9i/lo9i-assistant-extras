"""Runs a task with Claude Code through the Claude Agent SDK, which ships the Claude Code CLI, so nothing
else needs installing."""

import os
from typing import Any

from agent_tasks import Outcome, Progress, Task
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

# The plugin's field: a Claude token (`claude setup-token`, from a Pro or Max plan) or an Anthropic API key.
TOKEN_ENV = "CLAUDE_TOKEN"
_OAUTH_PREFIX = "sk-ant-oat"
# Tool inputs that say what a step is about, in the order they're looked for.
_DETAILS = ("command", "file_path", "path", "pattern", "url", "query", "description")
_LINE = 200


async def run(task: Task, progress: Progress) -> Outcome:
    result: ResultMessage | None = None
    async for message in query(prompt=task.prompt, options=options(task, _take_token())):
        for step in steps(message):
            progress(step)
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        return Outcome(ok=False, text="Claude Code ended without a result.")
    text = result.result or f"Claude Code ended with {result.subtype}."
    return Outcome(ok=not result.is_error, text=text, cost_usd=result.total_cost_usd)


def options(task: Task, token: str) -> ClaudeAgentOptions:
    """Claude Code as the user runs it: its own system prompt and the user's and the project's settings
    and CLAUDE.md. It works without asking: the user approved the task when it started."""
    follow_up = bool(task.follow_up_of)
    return ClaudeAgentOptions(
        cwd=task.repository,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        env=auth_env(token),
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


def _take_token() -> str:
    """Out of the environment the agent's commands inherit: it reaches Claude Code only as its login."""
    token = os.environ.pop(TOKEN_ENV, "")
    if not token:
        raise RuntimeError("No Claude token. Set it in the plugin's fields in Plugins.")
    return token
