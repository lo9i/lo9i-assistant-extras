"""Runs a task with the Copilot CLI in non-interactive mode: it works without asking (`--allow-all-tools`)
in the task's repository, in a session named after the task, which a follow-up resumes."""

import asyncio
import os
from pathlib import Path

from agent_tasks import Outcome, Progress, Task

from copilot_tasks import cli

# The plugin's field. Copilot reads it as its login and, given in --secret-env-vars, keeps it out of the
# shells it starts and redacts it from their output.
TOKEN_ENV = "COPILOT_GITHUB_TOKEN"
# Where the downloaded CLI is kept, in the plugin's data folder.
CLI_DIR_ENV = "COPILOT_CLI_DIR"
# Lines of its output kept as the result: its final answer and the summary after it.
_RESULT_LINES = 60


async def run(task: Task, progress: Progress) -> Outcome:
    _require_token()
    binary = await cli.ensure(Path(os.environ[CLI_DIR_ENV]), progress)
    process = await asyncio.create_subprocess_exec(
        *command(binary, task),
        cwd=task.repository,
        env=environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines = await _follow(process, progress)
    code = await process.wait()
    text = "\n".join(lines[-_RESULT_LINES:]).strip() or f"Copilot ended with exit code {code} and no output."
    return Outcome(ok=code == 0, text=text)


def command(binary: Path, task: Task) -> list[str]:
    return [
        str(binary),
        "--prompt",
        task.prompt,
        "--allow-all-tools",
        "--session-id",
        task.session,
        f"--secret-env-vars={TOKEN_ENV}",
    ]


def environment() -> dict[str, str]:
    """The CLI stays at the version this plugin downloaded; colors would only clutter the log."""
    return {**os.environ, "COPILOT_AUTO_UPDATE": "false", "NO_COLOR": "1"}


async def _follow(process: asyncio.subprocess.Process, progress: Progress) -> list[str]:
    assert process.stdout is not None
    lines: list[str] = []
    async for raw in process.stdout:
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        if line.strip():
            progress(line)
    return lines


def _require_token() -> None:
    if not os.environ.get(TOKEN_ENV):
        raise RuntimeError("No GitHub token. Set it in the plugin's fields in Plugins.")
