"""Runs one task. lo9i starts it detached with the task's folder (lo9i's docs/agent-plugins.md)."""

from copilot_tasks import runner, task_folder


def main() -> None:
    task_folder.main(runner.run)


if __name__ == "__main__":
    main()
