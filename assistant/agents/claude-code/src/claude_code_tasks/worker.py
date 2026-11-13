"""Runs one task. lo9i starts it detached with the task's folder (lo9i's docs/agent-plugins.md)."""

from claude_code_tasks import claude_cli, runner, task_folder


def main() -> None:
    # lo9i has no step after installing a plugin, so the SDK's copy of Claude Code goes at the first task.
    claude_cli.drop_bundled(claude_cli.installed())
    task_folder.main(runner.run)


if __name__ == "__main__":
    main()
