"""The tools over an in-process MCP session, with real detached workers running tests/fake_worker.py."""

import asyncio
import contextlib
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


async def _converse(store, conversation, folder, message):
    async with Client(build("agent", "the agent", "", _WORKER, store)) as client:
        return await client.call_tool(
            "converse", {"conversation": conversation, "folder": str(folder), "message": message}
        )


async def test_a_conversation_continues_its_session_in_the_same_folder(store, repo, tmp_path):
    first = await _converse(store, "owner:abc", repo, "finish")
    second = await _converse(store, "owner:abc", repo, "finish")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    third = await _converse(store, "owner:abc", elsewhere, "finish")
    assert not first.is_error and first.content[0].text.startswith("Did it in session")
    sessions = [r.content[0].text for r in (first, second, third)]
    assert sessions[0] == sessions[1] != sessions[2]
    newest, middle, _ = store.recent()
    assert middle.follow_up_of and not newest.follow_up_of


async def test_each_step_is_sent_as_progress_while_the_agent_works(store, repo):
    steps: list[str | None] = []

    async def progress(progress: float, total: float | None, message: str | None) -> None:
        steps.append(message)

    async with Client(build("agent", "the agent", "", _WORKER, store)) as client:
        arguments = {"conversation": "c", "folder": str(repo), "message": "finish"}
        result = await client.call_tool("converse", arguments, progress_callback=progress)
    assert not result.is_error and steps == [f"Read: {repo}/README.md"]


async def test_a_failing_turn_or_a_missing_folder_is_an_error(store, repo):
    failed = await _converse(store, "owner:abc", repo, "fail")
    assert failed.is_error and "the agent gave up" in failed.content[0].text
    missing = await _converse(store, "owner:abc", repo / "missing", "finish")
    assert missing.is_error and "isn't an existing folder" in missing.content[0].text


async def test_cancelling_the_call_stops_the_task(store, repo):
    async with Client(build("agent", "the agent", "", _WORKER, store)) as client:
        call = asyncio.create_task(
            client.call_tool("converse", {"conversation": "c", "folder": str(repo), "message": "sleep"})
        )
        while not store.recent() or store.pid(store.recent()[0].id) is None:
            await asyncio.sleep(0.05)
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call
        task_id = store.recent()[0].id
        for _ in range(100):
            if store.state(task_id).status == "stopped":
                break
            await asyncio.sleep(0.05)
    assert store.state(task_id).status == "stopped"
