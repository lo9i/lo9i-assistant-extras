"""Runs a task with Claude Code through the Claude Agent SDK, which ships the Claude Code CLI, so nothing
else needs installing."""

import os
from dataclasses import replace
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import ThinkingConfig

from claude_code_tasks.relay import Relay
from claude_code_tasks.task_folder import Outcome, Progress, Step, Task, note

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
# A tool's output is shown shortened: the whole of a file read or a long log would flood the chat.
_OUTPUT = 2_000
# Opus 4.7 and later leave their thinking out unless asked for a summary of it.
_SHOWN_THINKING: ThinkingConfig = {"type": "adaptive", "display": "summarized"}
# Each message from Claude Code is one JSON line, and a file it reads comes whole in one: a screenshot
# the user sent is a few MB as base64, past the SDK's default of 1 MB.
_MESSAGE_BYTES = 64 * 1024 * 1024
# Claude Code runs under lo9i's daemon: a conversation handed to it (/claude) waits inside a daemon run for
# its reply, and stopping the daemon cancels that run, which stops this task with everything it started.
_UNDER_LO9I = (
    "lo9i, the user's assistant, started this session, and its daemon waits for your reply. Never stop or "
    "kill the daemon's process (`assistant.daemon`, `stop_daemon` in lo9i's scripts): that ends this session "
    "before you reply, and any daemon you start from it. To restart it, for example so lo9i code changes "
    "take effect, run `curl -s -X POST --unix-socket ~/.lo9i/daemon.sock -H 'X-Lo9i-Client: claude-code' "
    "http://lo9i/system/restart`: it restarts once your reply is in, so tell the user the change applies "
    "from their next message."
)


async def run(task: Task, progress: Progress) -> Outcome:
    """With the Claude token when there is one, else on the user's Copilot plan."""
    token, github_token = _take(TOKEN_ENV), _take(COPILOT_ENV)
    if token:
        return await _run(task, progress, options(task, auth_env(token)))
    if not github_token:
        raise RuntimeError(_NO_LOGIN)
    async with Relay(github_token).running() as relay:
        progress(note("Using Claude on your GitHub Copilot plan"))
        outcome = await _run(task, progress, options(task, relay.claude_env()))
    # Claude Code prices its calls at Anthropic's API rates, which isn't what Copilot charges.
    return replace(outcome, cost_usd=None)


async def _run(task: Task, progress: Progress, settings: ClaudeAgentOptions) -> Outcome:
    result: ResultMessage | None = None
    steps = Steps()
    async for message in query(prompt=task.prompt, options=settings):
        for step in steps.of(message):
            progress(step)
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        return Outcome(ok=False, text="Claude Code ended without a result.")
    text = result.result or f"Claude Code ended with {result.subtype}."
    return Outcome(ok=not result.is_error, text=text, cost_usd=result.total_cost_usd)


def options(task: Task, env: dict[str, str]) -> ClaudeAgentOptions:
    """Claude Code as the user runs it: its own system prompt and the user's and the project's settings
    and CLAUDE.md, with its thinking shown, on a Claude token as on Copilot. It works without asking: the
    user approved the task when it started."""
    follow_up = bool(task.follow_up_of)
    return ClaudeAgentOptions(
        cwd=task.repository,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code", "append": _UNDER_LO9I},
        setting_sources=["user", "project", "local"],
        env=env,
        resume=task.session if follow_up else None,
        session_id=None if follow_up else task.session,
        max_buffer_size=_MESSAGE_BYTES,
        thinking=_SHOWN_THINKING,
    )


def auth_env(token: str) -> dict[str, str]:
    key = "CLAUDE_CODE_OAUTH_TOKEN" if token.startswith(_OAUTH_PREFIX) else "ANTHROPIC_API_KEY"
    return {key: token}


class Steps:
    """The steps in Claude Code's messages: its thinking, what it says, each tool call and its result. A
    result names only its call's id, so the calls' names are kept until their results come."""

    def __init__(self) -> None:
        self._tools: dict[str, str] = {}

    def of(self, message: object) -> list[Step]:
        if isinstance(message, AssistantMessage):
            within = message.parent_tool_use_id or ""
            return [step for block in message.content if (step := self._said(block, within))]
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            within = message.parent_tool_use_id or ""
            return [self._result(block, within) for block in message.content if isinstance(block, ToolResultBlock)]
        return []

    def _said(self, block: object, within: str) -> Step | None:
        if isinstance(block, ToolUseBlock):
            self._tools[block.id] = block.name
            return Step("tool", tool=block.name, detail=_detail(block.input), id=block.id, within=within)
        if isinstance(block, TextBlock) and block.text.strip():
            return Step("text", block.text.strip(), within=within)
        if isinstance(block, ThinkingBlock) and block.thinking.strip():
            return Step("thinking", block.thinking.strip(), within=within)
        return None

    def _result(self, block: ToolResultBlock, within: str) -> Step:
        text = _output(block.content)
        tool = self._tools.pop(block.tool_use_id, "")
        return Step("result", f"Error: {text}" if block.is_error else text, tool, id=block.tool_use_id, within=within)


def _output(content: str | list[dict[str, Any]] | None) -> str:
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "[image]")) for part in content)
    text = (content or "").strip()
    return text if len(text) <= _OUTPUT else text[:_OUTPUT] + "\n…"


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
