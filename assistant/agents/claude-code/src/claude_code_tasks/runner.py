"""Runs a task with Claude Code through the Claude Agent SDK, which ships the Claude Code CLI, so nothing
else needs installing."""

import asyncio
import os
from dataclasses import replace
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ThinkingConfig

from claude_code_tasks.relay import Relay
from claude_code_tasks.task_folder import Inbox, Message, Outcome, Progress, Step, Task, note

# The plugin's optional field: a Claude token (`claude setup-token`, from a Pro or Max plan) or an
# Anthropic API key.
TOKEN_ENV = "CLAUDE_TOKEN"
# lo9i's Copilot login when its model is GitHub Copilot (`model_login: copilot` in server.yaml): without
# a Claude token, Claude Code runs on the user's Copilot plan through a relay (relay.py).
COPILOT_ENV = "LO9I_COPILOT_GITHUB_TOKEN"
_OAUTH_PREFIX = "sk-ant-oat"
# Tool inputs that say what a step is about, in the order they're looked for.
_DETAILS = ("command", "file_path", "path", "pattern", "url", "query", "description")
# The whole command or path, up to a safety cap.
_LINE = 100_000
# A tool's whole output, up to a safety cap so a huge one can't freeze the page.
_OUTPUT = 100_000
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
# Shown when the user interrupts it, and as the result of each call it stopped.
_INTERRUPTED = "Interrupted: waiting for your next message"
_INTERRUPTED_CALL = "Interrupted"


async def run(task: Task, progress: Progress, inbox: Inbox) -> Outcome:
    """With the Claude token when there is one, else on the user's Copilot plan."""
    token, github_token = _take(TOKEN_ENV), _take(COPILOT_ENV)
    if token:
        return await _run(task, progress, inbox, options(task, auth_env(token)))
    if not github_token:
        raise RuntimeError(_NO_LOGIN)
    async with Relay(github_token).running() as relay:
        progress(note("Using Claude on your GitHub Copilot plan"))
        outcome = await _run(task, progress, inbox, options(task, relay.claude_env()))
    # Claude Code prices its calls at Anthropic's API rates, which isn't what Copilot charges.
    return replace(outcome, cost_usd=None)


async def _run(task: Task, progress: Progress, inbox: Inbox, settings: ClaudeAgentOptions) -> Outcome:
    async with ClaudeSDKClient(settings) as client:
        result = await Session(client, progress, inbox).answer(task.prompt)
    if result is None:
        return Outcome(ok=False, text="Claude Code ended without a result.")
    text = result.result or f"Claude Code ended with {result.subtype}."
    return Outcome(ok=not result.is_error, text=text, cost_usd=result.total_cost_usd)


class Session:
    """Claude Code's session for the task, kept open so the user can steer it while it works. A message
    from the inbox goes to it at once, and it takes it between its steps, echoing it back as it does
    (`replay-user-messages`); an interrupt stops the step in progress, and the session waits for the
    user's next message. It ends once Claude Code has answered every message, with its last result."""

    def __init__(self, client: ClaudeSDKClient, progress: Progress, inbox: Inbox) -> None:
        self._client, self._progress, self._inbox = client, progress, inbox
        self._steps = Steps()
        # The messages sent that Claude Code hasn't taken yet.
        self._untaken: list[str] = []
        # Interrupted: it waits for the user's next message.
        self._paused = False
        # A turn is in progress: there's something to interrupt.
        self._working = False

    async def answer(self, prompt: str) -> ResultMessage | None:
        await self._say(prompt)
        listening = asyncio.create_task(self._listen())
        answering = asyncio.create_task(self._answers())
        try:
            done, _ = await asyncio.wait({listening, answering}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            listening.cancel()
            answering.cancel()
        if listening in done:
            listening.result()  # it only ends with an error
        return answering.result()

    async def _answers(self) -> ResultMessage | None:
        result = None
        async for message in self._client.receive_messages():
            for step in self._steps.of(message):
                self._progress(self._shown(step))
            self._taken(message)
            if isinstance(message, ResultMessage):
                result = message
                self._working = bool(self._untaken)
                if await self._answered_all():
                    return result
        return result

    async def _answered_all(self) -> bool:
        """It took every message, none waits in the inbox, and it isn't waiting for the user's next one."""
        await self._pass_on(self._inbox.take())
        return not self._untaken and not self._paused

    async def _listen(self) -> None:
        while True:
            await self._pass_on(await self._inbox.next())

    async def _pass_on(self, messages: list[Message]) -> None:
        for message in messages:
            if message.kind == "interrupt":
                await self._interrupt()
            else:
                self._progress(Step("user", message.text, id=message.id))
                self._paused = False
                await self._say(message.text)

    async def _say(self, text: str) -> None:
        self._untaken.append(text.strip())
        self._working = True
        await self._client.query(text)

    async def _interrupt(self) -> None:
        """Once is enough: interrupted, it does nothing until the user's next message."""
        if self._paused:
            return
        self._paused = True
        self._progress(note(_INTERRUPTED))
        if self._working:
            await self._client.interrupt()

    def _shown(self, step: Step) -> Step:
        """The calls an interrupt stopped end as interrupted, not with Claude Code's note to itself."""
        return replace(step, text=_INTERRUPTED_CALL) if self._paused and step.kind == "result" else step

    def _taken(self, message: object) -> None:
        """Claude Code echoes each message it takes; its subagents' prompts come with the call they're in."""
        if isinstance(message, UserMessage) and isinstance(message.content, str) and not message.parent_tool_use_id:
            text = message.content.strip()
            if text in self._untaken:
                self._untaken.remove(text)


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
        # Its words as they're written, so lo9i shows them as they come (Steps._words).
        include_partial_messages=True,
        # Each message it takes, echoed back, so the session knows it has answered them all (Session).
        extra_args={"replay-user-messages": None},
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
        if isinstance(message, StreamEvent):
            return _words(message)
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


def _words(event: StreamEvent) -> list[Step]:
    """The words of a text or a thinking as they stream; the whole block follows in an AssistantMessage."""
    data = event.event
    delta = data.get("delta", {}) if data.get("type") == "content_block_delta" else {}
    within = event.parent_tool_use_id or ""
    if delta.get("type") == "text_delta" and delta.get("text"):
        return [Step("text_delta", delta["text"], within=within)]
    if delta.get("type") == "thinking_delta" and delta.get("thinking"):
        return [Step("thinking_delta", delta["thinking"], within=within)]
    return []


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
