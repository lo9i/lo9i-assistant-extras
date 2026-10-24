"""The worker's side of lo9i's task folder: each step a JSON line in progress.log, and how it ended."""

import asyncio
import json
import os
import signal
import subprocess
import sys

from claude_code_tasks.task_folder import Context, Inbox, Message, Outcome, Step, Task, _write_outcome, note, run

_TASK = Task("t1", "/repo", "fix it", "s1", "", "2026-10-08T00:00:00+00:00")


async def test_steps_are_json_lines_and_the_outcome_says_when_it_ended(tmp_path):
    async def runner(task, progress, inbox):
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
    async def runner(task, progress, inbox):
        raise RuntimeError("no login")

    outcome = await run(tmp_path, _TASK, runner)
    assert not outcome.ok and outcome.text == "RuntimeError: no login"


def test_the_context_goes_in_a_file_of_its_own_so_an_older_lo9i_still_reads_the_result(tmp_path):
    answered, failed = tmp_path / "answered", tmp_path / "failed"
    answered.mkdir(), failed.mkdir()
    _write_outcome(answered, Outcome(ok=True, text="Fixed.", finished_at="now", context=Context(42_000, 200_000)))
    assert json.loads((answered / "context.json").read_text()) == {"tokens": 42_000, "window": 200_000}
    result = json.loads((answered / "result.json").read_text())
    assert result == {"ok": True, "text": "Fixed.", "finished_at": "now", "cost_usd": None}
    _write_outcome(failed, Outcome(ok=False, text="no login"))
    assert not (failed / "context.json").exists() and (failed / "result.json").exists()


def test_the_worker_writes_its_pid_and_stops_at_once_when_already_stopped(tmp_path):
    (tmp_path / "task.json").write_text(json.dumps(_TASK.__dict__))
    (tmp_path / "stopped").write_text("now")
    subprocess.run([sys.executable, "-m", "claude_code_tasks.worker", str(tmp_path)], check=True)
    assert (tmp_path / "pid").read_text().isdigit() and not (tmp_path / "result.json").exists()


async def test_the_inbox_opens_once_it_listens_and_gives_each_message_once_in_order(tmp_path):
    inbox = Inbox(tmp_path)
    inbox.open()
    try:
        assert (tmp_path / "inbox").is_dir()  # lo9i sends messages only once it's there
        waiting = asyncio.create_task(inbox.next())
        await asyncio.sleep(0)
        assert not waiting.done()
        (tmp_path / "inbox" / "2.json").write_text(json.dumps({"kind": "interrupt"}))
        (tmp_path / "inbox" / "1.json").write_text(json.dumps({"kind": "message", "text": "use the fixture"}))
        (tmp_path / "inbox" / "3.partial").write_text("{")  # still being written
        os.kill(os.getpid(), signal.SIGUSR1)
        assert await asyncio.wait_for(waiting, 5) == [
            Message("1", "message", "use the fixture"),
            Message("2", "interrupt"),
        ]
        assert inbox.take() == []
    finally:
        inbox.close()
