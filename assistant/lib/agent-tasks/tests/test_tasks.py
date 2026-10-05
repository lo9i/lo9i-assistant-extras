import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from agent_tasks import Outcome, TaskStore
from agent_tasks.tasks import lost


def test_a_task_runs_until_its_worker_writes_how_it_ended(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("/repo", "fix the tests")
    assert store.state(task.id).status == "running"  # its worker hasn't written its pid yet
    store.append_progress(task.id, "Bash:   pytest\n -q")
    store.write_outcome(task.id, Outcome(ok=True, text="All green."))
    state = store.state(task.id)
    assert state.status == "done" and state.outcome.text == "All green." and state.progress == ["Bash: pytest -q"]


def test_a_worker_that_died_without_a_result_is_lost(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("/repo", "fix it")
    store.write_pid(task.id, os.getpid())  # a live process, but not the one started for this task
    state = store.state(task.id)
    assert state.status == "failed" and lost(state)


def test_a_worker_that_never_started_is_lost_after_a_while(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("/repo", "fix it")
    old = replace(task, started_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    (store.folder(task.id) / "task.json").write_text(json.dumps(asdict(old)))
    assert lost(store.state(task.id))


def test_stopped_wins_over_everything_else(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("/repo", "fix it")
    store.mark_stopped(task.id)
    store.write_outcome(task.id, Outcome(ok=False, text="killed"))
    assert store.state(task.id).status == "stopped"


def test_a_follow_up_keeps_the_session_and_recent_lists_newest_first(tmp_path):
    store = TaskStore(tmp_path)
    first = store.create("/repo", "one")
    second = store.create("/repo", "two", session=first.session, follow_up_of=first.id)
    assert second.session == first.session and second.follow_up_of == first.id
    assert [t.id for t in store.recent()] == [second.id, first.id]
