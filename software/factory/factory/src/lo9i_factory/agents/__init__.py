"""The coding agents that build the plan in the worktree (SPEC §1, §3):
Claude Code or Codex, as Settings says, run headless in the task's sandbox
(sandbox.py). They stream what they do as JSON lines, which go to the task's
log as they come, and end with a report: what they did, and for each review
point the review sent back, whether they resolved or declined it, and why
(SPEC §7).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..sandbox import Sandbox

# Long enough for a big change; a stuck agent is stopped after it.
TIMEOUT_SECONDS = 2 * 3600

REPORT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What you did, and anything left undone"},
        "points": {
            "type": "array",
            "description": "One for each review point you were given; [] when none",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["resolved", "declined"]},
                    "reason": {"type": "string", "description": "How you resolved it, or why you declined"},
                },
                "required": ["id", "status", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "points"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Job:
    task_id: int
    stage_id: int
    # What to build, and the review points to answer.
    prompt: str
    # The implementer's prompt (Settings), with how to work here.
    system: str
    # The model, without its provider.
    model: str
    # What's left of the task's max_usd; None when the agent can't be held to it.
    budget_usd: float | None
    sandbox: Sandbox
    # Whose model: Claude Code runs Anthropic's, or Copilot's Claude models through a relay (relay.py).
    provider: str = "anthropic"


@dataclass(frozen=True)
class Result:
    ok: bool
    summary: str
    # The review points' answers: id, status, reason.
    points: list[dict] = field(default_factory=list)
    # What it cost when the agent says (Claude Code does); else priced from the tokens.
    usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # The premium requests Copilot counted, on Copilot.
    requests: int = 0
    # Why it failed.
    error: str = ""
    # Stopped by the factory: the task was cancelled or paused, or ran out of time.
    stopped: bool = False


Log = Callable[[dict[str, Any]], None]


class Agent(Protocol):
    async def run(self, job: Job, log: Log, should_stop: Callable[[], bool]) -> Result: ...


def report(text: str) -> dict | None:
    """The report in an agent's last words: all of them as JSON, or the last
    JSON object in them (in a ```json fence or not)."""
    for candidate in (text, *reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL))):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and "summary" in data:
            return data
    start = text.rfind('{"summary"')
    if start >= 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            pass
    return None


def make(name: str, key: str) -> Agent:
    if name == "claude-code":
        from .claude_code import ClaudeCode

        return ClaudeCode(key)
    from .codex import Codex

    return Codex(key)


# Whose models each agent runs; the key it takes is that provider's (providers.KEYS).
PROVIDERS = {"claude-code": ("anthropic", "copilot"), "codex": ("openai",)}
