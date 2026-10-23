"""Codex, headless (`codex exec --json`).

- docker: it runs without its own sandbox or approvals
  (--dangerously-bypass-approvals-and-sandbox): the container is the sandbox.
- local: Codex's own sandbox (--sandbox workspace-write): it writes only in
  the worktree and has no network. Codex's headless mode can't ask the user
  before each command, as SPEC §11 wants of local mode, so its sandbox stands
  in: commands run unasked, but can't reach the network or write elsewhere.

It can't be held to a budget as it runs: the task's limit is checked before
it starts, and its tokens are priced after.
"""

import json
from collections.abc import Callable
from typing import Any

from ..sandbox import find
from . import REPORT, TIMEOUT_SECONDS, Job, Log, Result, report


class Codex:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def argv(self, job: Job) -> list[str]:
        docker = job.sandbox.mode == "docker"
        args = [
            "codex" if docker else find("codex"),
            "exec",
            "--json",
            "--model",
            job.model,
            "--skip-git-repo-check",
            "--ephemeral",
            "--cd",
            job.sandbox.folder,
        ]
        if docker:
            return [*args, "--dangerously-bypass-approvals-and-sandbox", "-"]
        return [*args, "--sandbox", "workspace-write", "-"]

    def env(self, job: Job) -> dict[str, str]:
        return {"CODEX_API_KEY": self.api_key}

    def prompt(self, job: Job) -> str:
        """Codex takes no system prompt of ours: the implementer's comes first."""
        return (
            f"{job.system}\n\n{job.prompt}\n\nYour final message must be only a JSON object of this shape,"
            f" nothing else:\n{json.dumps(REPORT)}"
        )

    async def run(self, job: Job, log: Log, should_stop: Callable[[], bool]) -> Result:
        said: list[str] = []
        usage: dict[str, int] = {}
        failures: list[str] = []

        def line(text: str) -> None:
            try:
                event = json.loads(text)
            except ValueError:
                return
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                said.append(str(item.get("text") or ""))
            if kind == "turn.completed":
                for k, v in (event.get("usage") or {}).items():
                    usage[k] = usage.get(k, 0) + int(v or 0)
            if kind in ("turn.failed", "error"):
                failures.append(str((event.get("error") or {}).get("message") or event.get("message") or kind))
            for step in steps(event):
                log(step)

        done = await job.sandbox.run(
            self.argv(job),
            self.env(job),
            stdin=self.prompt(job),
            on_line=line,
            should_stop=should_stop,
            timeout=TIMEOUT_SECONDS,
        )
        data = report(said[-1]) if said else None
        failed = done.exit_code != 0 or bool(failures) or done.stopped
        cached = usage.get("cached_input_tokens", 0)
        return Result(
            ok=not failed,
            summary=str((data or {}).get("summary") or (said[-1] if said else "")),
            points=list((data or {}).get("points") or []),
            input_tokens=usage.get("input_tokens", 0) - cached,
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=cached,
            # Its last failure says it best: those before are retries.
            error=(failures[-1][:1000] if failures else done.output[-1000:].strip() if failed else ""),
            stopped=done.stopped,
        )


def steps(event: dict) -> list[dict[str, Any]]:
    """What a Codex event shows of its work, for the task's log."""
    item = event.get("item") or {}
    if event.get("type") != "item.completed":
        return []
    match item.get("type"):
        case "agent_message":
            return [{"type": "text", "text": str(item.get("text") or "")}]
        case "command_execution":
            return [{"type": "tool", "tool": "command", "detail": str(item.get("command") or "")[:500]}]
        case "file_change":
            paths = [str(c.get("path")) for c in item.get("changes") or []]
            return [{"type": "tool", "tool": "edit", "detail": ", ".join(paths)[:500]}]
    return []
