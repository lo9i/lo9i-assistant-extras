"""Claude Code, headless (`claude -p`), with stream-json output.

- docker: it skips its permission checks (--dangerously-skip-permissions):
  the container is the sandbox. It reads the repository's own settings.
- local: every step that needs permission goes to the factory's approval
  server (approvals.py, --permission-prompt-tool), which lets file work in
  the worktree through and asks the user for the rest. The repository's
  committed settings aren't read: they could allow commands unasked.

Either way: no MCP servers but the factory's (--strict-mcp-config), no
session saved, the task's remaining budget as its limit (--max-budget-usd),
and its report in the shape asked for (--json-schema).

It runs Anthropic's models with ANTHROPIC_API_KEY, or Copilot's Claude
models through a relay (relay.py) with lo9i's Copilot sign-in. On Copilot
it has no USD limit: Copilot counts requests, which its USD figures don't
measure.
"""

import json
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .. import db
from ..sandbox import find
from . import REPORT, TIMEOUT_SECONDS, Job, Log, Result, report
from .relay import Relay

# How long Claude Code waits for an MCP tool: an approval waits for the user.
_TOOL_TIMEOUT_MS = str(3_700_000)
_DETAILS = ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description")


class ClaudeCode:
    def __init__(self, api_key: str) -> None:
        """`api_key`: Anthropic's, or lo9i's GitHub token for a job on Copilot."""
        self.api_key = api_key

    def argv(self, job: Job) -> list[str]:
        docker = job.sandbox.mode == "docker"
        args = [
            "claude" if docker else find("claude"),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            job.model,
            "--append-system-prompt",
            job.system,
            "--json-schema",
            json.dumps(REPORT),
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if job.budget_usd is not None:
            args += ["--max-budget-usd", f"{job.budget_usd:.2f}"]
        if docker:
            return [*args, "--setting-sources", "project,local", "--dangerously-skip-permissions"]
        approvals = {
            "mcpServers": {
                "factory": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "lo9i_factory.approvals"],
                    "env": {
                        "FACTORY_DB": str(db.path()),
                        "FACTORY_TASK": str(job.task_id),
                        "FACTORY_STAGE": str(job.stage_id),
                        "FACTORY_WORKTREE": job.sandbox.worktree,
                    },
                }
            }
        }
        return [
            *args,
            "--setting-sources",
            "local",
            "--mcp-config",
            json.dumps(approvals),
            "--permission-prompt-tool",
            "mcp__factory__approve",
        ]

    def env(self, job: Job, relay: Relay | None = None) -> dict[str, str]:
        """Its login: the API key, or the relay to Copilot."""
        docker = job.sandbox.mode == "docker"
        login = relay.claude_env(job.model, docker) if relay else {"ANTHROPIC_API_KEY": self.api_key}
        env = {**login, "DISABLE_AUTOUPDATER": "1"}
        if not docker:
            env["MCP_TOOL_TIMEOUT"] = _TOOL_TIMEOUT_MS
        return env

    async def run(self, job: Job, log: Log, should_stop: Callable[[], bool]) -> Result:
        if job.provider != "copilot":
            return await self._run(job, self.env(job), log, should_stop)
        async with Relay(self.api_key).running() as relay:
            job = replace(job, model=relay.model(job.model))
            done = await self._run(job, self.env(job, relay), log, should_stop)
        return replace(done, requests=relay.requests)

    async def _run(self, job: Job, env: dict[str, str], log: Log, should_stop: Callable[[], bool]) -> Result:
        ended: list[dict] = []

        def line(text: str) -> None:
            try:
                event = json.loads(text)
            except ValueError:
                return
            if event.get("type") == "result":
                ended.append(event)
            for step in steps(event):
                log(step)

        done = await job.sandbox.run(
            self.argv(job),
            env,
            stdin=job.prompt,
            on_line=line,
            should_stop=should_stop,
            timeout=TIMEOUT_SECONDS,
        )
        if not ended:
            return Result(False, "", error=f"Claude Code ended without a result: {done.output[-1000:]}".strip(),
                          stopped=done.stopped)  # fmt: skip
        return result(ended[-1], done.stopped)


def result(event: dict, stopped: bool = False) -> Result:
    """The Result of Claude Code's last message."""
    u = event.get("usage") or {}
    structured = event.get("structured_output")
    data = structured if isinstance(structured, dict) else report(str(event.get("result") or "")) or {}
    failed = bool(event.get("is_error")) or event.get("subtype") != "success"
    return Result(
        ok=not failed and not stopped,
        summary=str(data.get("summary") or event.get("result") or ""),
        points=list(data.get("points") or []),
        usd=float(event.get("total_cost_usd") or 0),
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        error=_why(event) if failed else "",
        stopped=stopped,
    )


def _why(event: dict) -> str:
    """Why Claude Code failed: its words, else how it ended (error_max_turns, error_max_budget_usd)."""
    said = str(event.get("result") or "").strip()
    subtype = str(event.get("subtype") or "")
    if subtype and subtype != "success":
        return f"Claude Code ended with {subtype}" + (f": {said}" if said else "")
    return f"Claude Code: {said or 'it failed without saying why'}"


def steps(event: dict) -> list[dict[str, Any]]:
    """What a stream-json event shows of the agent's work: what it says and
    each tool it uses, for the task's log."""
    message = event.get("message") or {}
    if event.get("type") != "assistant" or not isinstance(message.get("content"), list):
        return []
    out = []
    for block in message["content"]:
        if block.get("type") == "text" and block.get("text", "").strip():
            out.append({"type": "text", "text": block["text"].strip()})
        elif block.get("type") == "tool_use":
            given = block.get("input") or {}
            detail = next((str(given[k]) for k in _DETAILS if given.get(k)), "")
            out.append({"type": "tool", "tool": block.get("name", ""), "detail": detail[:500]})
    return out
