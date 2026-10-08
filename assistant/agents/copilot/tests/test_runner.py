"""The runner with a fake `copilot`: a script that prints its arguments and what it would do."""

import pytest

from copilot_tasks import cli, runner
from copilot_tasks.task_folder import Task

_FAKE = """#!/bin/sh
echo "args: $*"
echo "token in env: ${COPILOT_GITHUB_TOKEN:+yes} auto update: $COPILOT_AUTO_UPDATE"
echo "cwd: $(pwd)"
echo
echo "Done: fixed the tests."
exit "${FAKE_EXIT:-0}"
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    binary = tmp_path / "copilot"
    binary.write_text(_FAKE)
    binary.chmod(0o755)

    async def ensure(root, progress, version=cli.VERSION):
        return binary

    monkeypatch.setattr(cli, "ensure", ensure)
    monkeypatch.setenv(runner.CLI_DIR_ENV, str(tmp_path / "cli"))
    monkeypatch.setenv(runner.TOKEN_ENV, "github_pat_x")
    return binary


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    return folder


def _task(repo, follow_up_of=""):
    return Task("t1", str(repo), "fix the tests", "6f1c8e2a-0000-4000-8000-000000000000", follow_up_of, "2026-10-05")


async def test_a_task_runs_headless_in_its_session_and_repository(fake_cli, repo):
    notes = []
    outcome = await runner.run(_task(repo), notes.append)
    steps = [step.text for step in notes if step.kind == "note"]
    assert outcome.ok and outcome.text.endswith("Done: fixed the tests.") and len(steps) == len(notes)
    args = steps[0]
    assert "--prompt fix the tests --allow-all-tools" in args
    assert "--session-id 6f1c8e2a-0000-4000-8000-000000000000" in args
    assert "--secret-env-vars=COPILOT_GITHUB_TOKEN" in args
    assert "token in env: yes auto update: false" in steps and f"cwd: {repo.resolve()}" in steps
    assert "" not in steps  # blank lines aren't steps


async def test_a_nonzero_exit_fails_the_task(fake_cli, repo, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "1")
    outcome = await runner.run(_task(repo), lambda _: None)
    assert not outcome.ok and "Done: fixed the tests." in outcome.text


async def test_without_a_token_nothing_runs(fake_cli, repo, monkeypatch):
    monkeypatch.delenv(runner.TOKEN_ENV)
    with pytest.raises(RuntimeError, match="No GitHub token"):
        await runner.run(_task(repo), lambda _: None)
