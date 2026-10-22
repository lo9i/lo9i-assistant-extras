"""The runner taking tasks through the spec and plan stages and their
checkpoints, on fake models."""

import asyncio

import pytest
from fakes import Team, on_fakes

from lo9i_factory import db, repo, service, settings
from lo9i_factory.providers import Providers
from lo9i_factory.runner import Runner


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A database file: the runner opens its own connections."""
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "factory.db"))
    conn = db.connect()
    on_fakes(conn)
    yield conn
    conn.close()


def runner(team: Team, env: dict | None = None) -> Runner:
    return Runner(Providers(env=env or {}, clients={"copilot": team}), poll=60)


def once(r: Runner) -> bool:
    return asyncio.run(r.run_once())


def context(request) -> str:
    return request.turns[0].text


def test_a_task_goes_through_the_spec_and_plan_checkpoints(conn, git_repo):
    team = Team()
    r = runner(team)
    t = service.create_task(conn, str(git_repo), "Add a cache to the search")

    assert once(r)
    assert team.kinds() == [("architect", "proposal"), ("judge", "decision")]
    waiting = service.get_task(conn, t.id)
    assert (waiting.status, waiting.column) == ("waiting", "waiting")
    checkpoint = service.pending_checkpoint(conn, t.id)
    assert checkpoint.kind == "spec"
    assert service.decision(conn, checkpoint).body["result"] == "Final spec"

    service.approve(conn, t.id)
    team.calls.clear()
    assert once(r)
    assert [k for _, k in team.kinds()] == ["proposal"] * 3 + ["critique"] * 3 + ["revision"] * 3 + ["verdict"]
    assert {role for role, _ in team.kinds()} == {"architect", "security", "tester", "judge"}
    assert "## The approved spec\n\nFinal spec" in context(team.calls[0][2])
    assert service.pending_checkpoint(conn, t.id).kind == "plan"

    service.reject(conn, t.id, "Keep the old API")
    team.calls.clear()
    assert once(r)
    first = team.calls[0][2]
    assert "## An earlier plan, which the user rejected\n\nFinal spec" in context(first)
    assert "## Before the debate\n\n" in first.turns[1].text and "The user: Keep the old API" in first.turns[1].text
    assert "answers" not in [k for _, k in team.kinds()]
    assert [(s.kind, s.attempt, s.status) for s in service.stages(conn, t.id)] == [
        ("spec", 1, "approved"),
        ("plan", 1, "rejected"),
        ("plan", 2, "waiting"),
    ]

    assert not once(r)


def test_the_repositorys_context_goes_to_the_team_when_the_task_asks(conn, git_repo):
    (git_repo / "README.md").write_text("# Shop\nA shop.")
    team = Team()
    service.create_task(conn, str(git_repo), "Add a cache", include_context=True)
    service.create_task(conn, str(git_repo), "Add a log")
    r = runner(team)
    once(r)
    once(r)
    with_context, without = (context(req) for _, k, req in team.calls if k == "proposal")
    assert "## README.md\n\n# Shop\nA shop." in with_context and "## Files in the repository" in with_context
    assert "README" not in without
    assert f"The repository: {git_repo}" in without


def test_the_oldest_queued_task_goes_first(conn, git_repo):
    team = Team()
    service.create_task(conn, str(git_repo), "First")
    service.create_task(conn, str(git_repo), "Second")
    once(runner(team))
    assert [t.status for t in service.list_tasks(conn)] == ["queued", "waiting"]


def test_a_model_without_a_key_or_a_price_pauses_the_task(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    settings.update(conn, {"models.architect": "anthropic:claude-opus-5-5"})
    once(runner(Team()))
    paused = service.get_task(conn, t.id)
    assert paused.status == "paused" and "ANTHROPIC_API_KEY isn't set" in paused.reason

    settings.update(conn, {"models.architect": "openai:gpt-9"})
    service.resume(conn, t.id)
    once(runner(Team(), env={"OPENAI_API_KEY": "sk"}))
    assert "no price for openai:gpt-9" in service.get_task(conn, t.id).reason


def test_a_bug_fails_the_task_instead_of_looping(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    once(runner(Team({("architect", "proposal"): [RuntimeError("boom")]})))
    failed = service.get_task(conn, t.id)
    assert (failed.status, failed.reason) == ("failed", "the factory failed: RuntimeError: boom")
    assert service.stages(conn, t.id)[0].status == "failed"


def test_after_a_restart_running_tasks_go_on_and_new_work_wakes_the_runner(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    with conn:
        repo.update_task(conn, t.id, {"status": "running"})
    team = Team()
    r = runner(team)

    async def scenario():
        worker = asyncio.create_task(r.run())
        try:
            await until(lambda: service.get_task(conn, t.id).status == "waiting")
            service.approve(conn, t.id)
            r.wake()  # Polling every 60 s: only the wake gets the plan going now.
            await until(lambda: service.pending_checkpoint(conn, t.id) is not None)
        finally:
            worker.cancel()

    asyncio.run(scenario())
    assert service.pending_checkpoint(conn, t.id).kind == "plan"


async def until(condition, seconds: float = 5) -> None:
    for _ in range(int(seconds * 100)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def test_a_comment_at_a_checkpoint_says_to_reject_instead(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    once(runner(Team()))
    with pytest.raises(Exception, match="reject it with the comment"):
        service.comment(conn, t.id, "Smaller")
