import asyncio
import json

import pytest
from mcp import Client

from lo9i_factory import db, service, settings
from lo9i_factory.mcp_server import mcp
from lo9i_factory.models import ROLES

NO_COSTS = {"usd": 0, "requests": 0, "by_stage": {}, "by_role": {}}


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "test.db"))


def call(*calls: tuple[str, dict]) -> list:
    """Run tool calls in order over one in-process session."""

    async def run():
        async with Client(mcp) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def at_checkpoint(id: int) -> None:
    """What the runner will do once the judge has decided."""
    conn = db.connect()
    try:
        service.open_checkpoint(conn, id)
    finally:
        conn.close()


def test_a_task_from_new_to_deleted(git_repo):
    created, listed, status, cancelled, deleted, gone = call(
        ("new_task", {"repo": str(git_repo), "description": "Add dark mode", "include_context": True}),
        ("list_tasks", {}),
        ("task_status", {"id": 1}),
        ("cancel", {"id": 1}),
        ("delete_task", {"id": 1}),
        ("list_tasks", {}),
    )
    task = payload(created)
    assert (task["branch"], task["status"], task["column"], task["include_context"]) == (
        "factory/1-add-dark-mode",
        "queued",
        "spec",
        True,
    )
    assert listed.structured_content["result"][0]["title"] == "Add dark mode"
    assert payload(status)["stages"] == [
        {
            "id": 1,
            "kind": "spec",
            "attempt": 1,
            "status": "pending",
            "round": 0,
            "step": None,
            "started_at": None,
            "finished_at": None,
        }
    ]
    assert payload(status)["checkpoint"] is None and payload(status)["costs"] == NO_COSTS
    assert payload(cancelled)["status"] == "cancelled"
    assert deleted.content[0].text == "deleted task 1, Add dark mode"
    assert gone.structured_content["result"] == []


def test_checkpoints_are_approved_and_rejected(git_repo):
    (created,) = call(("new_task", {"repo": str(git_repo), "description": "x"}))
    at_checkpoint(payload(created)["id"])
    (waiting,) = call(("task_status", {"id": 1}))
    assert payload(waiting)["checkpoint"]["kind"] == "spec" and payload(waiting)["column"] == "waiting"

    rejected, approve_early = call(
        ("reject", {"id": 1, "comment": "Support both themes"}),
        ("approve", {"id": 1}),
    )
    assert [(s["kind"], s["attempt"], s["status"]) for s in payload(rejected)["stages"]] == [
        ("spec", 1, "rejected"),
        ("spec", 2, "pending"),
    ]
    assert approve_early.is_error
    assert "ConflictError: task 1 has no checkpoint waiting for you" in approve_early.content[0].text

    at_checkpoint(1)
    (approved,) = call(("approve", {"id": 1, "comment": "Good"}))
    assert (payload(approved)["stage"], payload(approved)["status"]) == ("plan", "queued")


def test_service_errors_reach_the_model(git_repo, tmp_path):
    no_repo, no_task, no_comment = call(
        ("new_task", {"repo": str(tmp_path), "description": "x"}),
        ("task_status", {"id": 7}),
        ("reject", {"id": 7, "comment": ""}),
    )
    assert (
        no_repo.is_error
        and "ValidationError" in no_repo.content[0].text
        and "isn't a git repository" in no_repo.content[0].text
    )
    assert no_task.is_error and "NotFoundError: no task 7" in no_task.content[0].text
    assert no_comment.is_error and "say what to change" in no_comment.content[0].text


def test_settings_are_read_and_changed(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "LO9I_COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    before, changed, bad, with_prompts = call(
        ("settings", {}),
        ("update_settings", {"changes": {"max_rounds": 4, "repos./work/app": {"test_command": "make test"}}}),
        ("update_settings", {"changes": {"agent": "aider"}}),
        ("settings", {"prompts": True}),
    )
    assert payload(before)["max_rounds"] == 3 and "prompts" not in payload(before)
    assert payload(before)["providers"] == {"anthropic": False, "openai": True, "copilot": False}
    after = payload(changed)
    assert after["max_rounds"] == 4 and after["changed"] == ["max_rounds"]
    assert after["repos"] == {"/work/app": {"test_command": "make test", "base_branch": ""}}
    assert bad.is_error and "agent must be one of claude-code, codex" in bad.content[0].text
    assert set(payload(with_prompts)["prompts"]) == set(ROLES)


def test_only_read_tools_are_read_only():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    assert {t.name for t in tools} == {
        "new_task",
        "list_tasks",
        "task_status",
        "approve",
        "reject",
        "answer_approval",
        "comment",
        "resume",
        "cancel",
        "delete_task",
        "settings",
        "update_settings",
    }
    read_only = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
    assert read_only == {"list_tasks", "task_status", "settings"}


def test_a_paused_task_is_resumed(git_repo):
    (created,) = call(("new_task", {"repo": str(git_repo), "description": "x"}))
    conn = db.connect()
    try:
        service.pause(conn, payload(created)["id"], "reached max_usd: spent $10.00 of $10.00")
        settings.update(conn, {"max_usd": 20})
    finally:
        conn.close()
    resumed, again = call(("resume", {"id": 1}), ("resume", {"id": 1}))
    assert (payload(resumed)["status"], payload(resumed)["reason"]) == ("queued", "")
    assert again.is_error and "isn't paused" in again.content[0].text


def test_the_user_steps_into_a_debate(git_repo):
    _, commented, empty = call(
        ("new_task", {"repo": str(git_repo), "description": "x"}),
        ("comment", {"id": 1, "text": "Only the search page"}),
        ("comment", {"id": 1, "text": " "}),
    )
    assert (payload(commented)["role"], payload(commented)["body"]) == ("user", {"text": "Only the search page"})
    assert empty.is_error and "the comment is empty" in empty.content[0].text


def test_task_status_shows_the_judges_decision_at_a_checkpoint(git_repo):
    from fakes import Team, on_fakes

    from lo9i_factory.providers import Providers
    from lo9i_factory.runner import Runner

    (created,) = call(("new_task", {"repo": str(git_repo), "description": "Add dark mode"}))
    conn = db.connect()
    try:
        on_fakes(conn)
    finally:
        conn.close()
    asyncio.run(Runner(Providers(env={}, clients={"copilot": Team()})).run_once())

    (status,) = call(("task_status", {"id": payload(created)["id"]}))
    checkpoint = payload(status)["checkpoint"]
    assert (payload(status)["status"], checkpoint["kind"]) == ("waiting", "spec")
    assert checkpoint["decision"] == {
        "rationale": "Simplest",
        "winner": "architect",
        "result": "Final spec",
        "options": [],
        "agreed": False,
    }


def test_a_task_remembers_the_conversation_it_was_driven_from(git_repo):
    from lo9i_factory.notify import notifier

    telegram = {"user_id": "owner", "thread_id": "t-1", "channel": "telegram"}
    web = {"user_id": "owner", "thread_id": "t-2", "channel": "web"}

    async def run():
        async with Client(mcp) as client:
            await client.call_tool(
                "new_task", {"repo": str(git_repo), "description": "x"}, meta={"lo9i/origin": telegram}
            )
            first = service_origin()
            await client.call_tool("cancel", {"id": 1}, meta={"lo9i/origin": web})
            return first, service_origin()

    assert asyncio.run(run()) == (telegram, web)
    assert notifier.session is not None


def service_origin():
    conn = db.connect()
    try:
        return service.get_task(conn, 1).origin
    finally:
        conn.close()
