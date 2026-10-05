"""Runs one task; the MCP server starts it detached with the task's folder (agent_tasks/launch.py)."""

from agent_tasks import worker

from claude_code_tasks import runner


def main() -> None:
    worker.main(runner.run)


if __name__ == "__main__":
    main()
