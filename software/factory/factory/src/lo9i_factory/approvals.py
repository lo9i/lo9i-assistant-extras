"""The user's say over what the agent does in local mode (SPEC §11): every
command and every network access waits for the user to allow it.

Claude Code asks this MCP server before each step that needs permission
(--permission-prompt-tool; agents/claude_code.py starts it for the task).
Reading and editing files inside the worktree goes ahead; anything else (a
command, the network, a file elsewhere) is put to the user as an approval,
which they answer with the answer_approval tool (or, later, the page or
Telegram), and the agent waits for the answer. The factory asks the same
way before it runs a test command a model wrote.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import db, repo

# The file tools, and where each names its path; None for none.
FILE_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
    "TodoWrite": None,
}
POLL_SECONDS = 1.0
# An approval no one answers is denied after this long.
WAIT_SECONDS = 3600
_DETAILS = ("command", "url", "query", "file_path", "notebook_path", "path", "pattern")


def automatic(tool: str, given: dict[str, Any], worktree: str) -> bool:
    """A file tool working inside the worktree, which goes ahead unasked."""
    if tool not in FILE_TOOLS:
        return False
    key = FILE_TOOLS[tool]
    if key is None:
        return True
    path = Path(str(given.get(key) or worktree))
    full = (Path(worktree) / path).resolve() if not path.is_absolute() else path.resolve()
    return full.is_relative_to(Path(worktree).resolve())


def describe(tool: str, given: dict[str, Any]) -> str:
    """What the step would do, for the user to read."""
    detail = next((str(given[k]) for k in _DETAILS if given.get(k)), "")
    return detail or json.dumps(given)[:2000]


async def ask(task_id: int, stage_id: int, tool: str, detail: str) -> tuple[bool, str]:
    """Put a step to the user and wait for their answer: (allowed, reason).
    Denied without one after WAIT_SECONDS, or when the task stops running."""
    conn = db.connect()
    try:
        with conn:
            id = repo.insert_approval(conn, task_id, stage_id, tool, detail)
        for _ in range(int(WAIT_SECONDS / POLL_SECONDS)):
            approval = repo.get_approval(conn, id)
            if approval.status != "pending":
                return approval.status == "allowed", approval.reason
            task = repo.get_task(conn, task_id)
            if task is None or task.status != "running":
                reason = "the task stopped"
                break
            await asyncio.sleep(POLL_SECONDS)
        else:
            reason = "no answer in time"
        with conn:
            repo.decide_approval(conn, id, "denied", reason)
        return False, reason
    finally:
        conn.close()


mcp = MCPServer("factory-approvals")


@mcp.tool()
async def approve(tool_name: str, input: dict[str, Any], tool_use_id: str | None = None) -> str:
    """Claude Code's permission prompt: allow or deny a step."""
    if automatic(tool_name, input, os.environ["FACTORY_WORKTREE"]):
        return json.dumps({"behavior": "allow", "updatedInput": input})
    task_id, stage_id = int(os.environ["FACTORY_TASK"]), int(os.environ["FACTORY_STAGE"])
    allowed, reason = await ask(task_id, stage_id, tool_name, describe(tool_name, input))
    if allowed:
        return json.dumps({"behavior": "allow", "updatedInput": input})
    return json.dumps({"behavior": "deny", "message": f"The user didn't allow it{': ' + reason if reason else ''}."})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
