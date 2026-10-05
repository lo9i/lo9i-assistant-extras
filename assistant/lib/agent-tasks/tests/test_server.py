"""The tools over an in-process MCP session, with real detached workers running tests/fake_worker.py."""

import re
import sys
from pathlib import Path

import pytest
from mcp import Client

from agent_tasks import TaskStore, build

_WORKER = [sys.executable, str(Path(__file__).with_name("fake_worker.py"))]


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "tasks")


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    return folder


async def _call(store, name, **args):
    async with Client(build("agent", "the agent", "", _WORKER, store)) as client:
        result = await client.call_tool(name, args)
    return result.is_error, result.content[0].text


async def _start(store, repo, task, **args):
    error, text = await _call(store, "start_task", repository=str(repo), task=task, **args)
    assert not error, text
    return re.search(r"Started task (\w+)", text).group(1)


async def test_a_task_runs_in_the_background_and_its_result_is_shown(store, repo):
    task_id = await _start(store, repo, "finish")
    error, text = await _call(store, "task_status", task_id=task_id, wait_seconds=30)
    assert not error
    assert f"{task_id}: done" in text and "Did it in session" in text and "Cost: $0.25" in text
    assert f"- Read: {repo}/README.md" in text and f'follow_up_of="{task_id}"' in text


async def test_a_follow_up_continues_the_same_session(store, repo):
    first = await _start(store, repo, "finish")
    await _call(store, "task_status", task_id=first, wait_seconds=30)
    second = await _start(store, repo, "finish", follow_up_of=first)
    await _call(store, "task_status", task_id=second, wait_seconds=30)
    assert store.get(second).session == store.get(first).session
    _, listed = await _call(store, "list_tasks")
    assert listed.index(second) < listed.index(first)


async def test_a_failing_agent_ends_the_task_as_failed(store, repo):
    task_id = await _start(store, repo, "fail")
    _, text = await _call(store, "task_status", task_id=task_id, wait_seconds=30)
    assert f"{task_id}: failed" in text and "RuntimeError: the agent gave up" in text


async def test_a_running_task_can_be_stopped_and_not_followed_up_meanwhile(store, repo):
    task_id = await _start(store, repo, "sleep")
    error, text = await _call(store, "start_task", repository=str(repo), task="more", follow_up_of=task_id)
    assert error and "still running" in text
    await _call(store, "task_status", task_id=task_id, wait_seconds=2)  # let the worker write its pid
    _, text = await _call(store, "stop_task", task_id=task_id)
    assert text.startswith(f"Stopped task {task_id}")
    _, text = await _call(store, "task_status", task_id=task_id)
    assert f"{task_id}: stopped" in text


async def test_a_folder_that_doesnt_exist_or_a_relative_one_is_refused(store, repo):
    for repository in ["relative/path", str(repo / "missing")]:
        error, text = await _call(store, "start_task", repository=repository, task="finish")
        assert error and "isn't an existing folder" in text
    error, text = await _call(store, "task_status", task_id="nope")
    assert error and "No task nope." in text
