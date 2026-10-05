"""MCP server over stdio. lo9i starts it (see server.yaml) with AGENT_TASKS_DIR and CLAUDE_TOKEN set."""

import os
import sys
from pathlib import Path

from agent_tasks import TaskStore, build

INSTRUCTIONS = """\
Claude Code works on coding tasks on its own, in the background: it reads the code, edits files and runs
commands in the repository it's given, then reports. Start one with start_task and a complete brief,
follow it with task_status (wait_seconds to wait for it), and continue its work with follow_up_of.
"""

WORKER = [sys.executable, "-m", "claude_code_tasks.worker"]


def main() -> None:
    store = TaskStore(Path(os.environ["AGENT_TASKS_DIR"]))
    build("claude-code", "Claude Code", INSTRUCTIONS, WORKER, store).run()


if __name__ == "__main__":
    main()
