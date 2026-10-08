"""The worker's side of lo9i's task folder: each step a JSON line in progress.log, and how it ended."""

import json
import subprocess
import sys

from claude_code_tasks.task_folder import Outcome, Step, Task, note, run

_TASK = Task("t1", "/repo", "fix it", "s1", "", "2026-10-08T00:00:00+00:00")


async def test_steps_are_json_lines_and_the_outcome_says_when_it_ended(tmp_path):
    async def runner(task, progress):
        progress(note("Using Claude on your GitHub Copilot plan"))
        progress(Step("tool", tool="Bash", detail="pytest", id="1"))
        return Outcome(ok=True, text="Fixed.", cost_usd=0.5)

    outcome = await run(tmp_path, _TASK, runner)
    lines = [json.loads(line) for line in (tmp_path / "progress.log").read_text().splitlines()]
    assert lines == [
        {"kind": "note", "text": "Using Claude on your GitHub Copilot plan"},
        {"kind": "tool", "tool": "Bash", "detail": "pytest", "id": "1"},
    ]
    assert (outcome.ok, outcome.text, outcome.cost_usd) == (True, "Fixed.", 0.5) and outcome.finished_at


async def test_an_error_ends_the_task_as_failed(tmp_path):
    async def runner(task, progress):
        raise RuntimeError("no login")

    outcome = await run(tmp_path, _TASK, runner)
    assert not outcome.ok and outcome.text == "RuntimeError: no login"


def test_the_worker_writes_its_pid_and_stops_at_once_when_already_stopped(tmp_path):
    (tmp_path / "task.json").write_text(json.dumps(_TASK.__dict__))
    (tmp_path / "stopped").write_text("now")
    subprocess.run([sys.executable, "-m", "claude_code_tasks.worker", str(tmp_path)], check=True)
    assert (tmp_path / "pid").read_text().isdigit() and not (tmp_path / "result.json").exists()
