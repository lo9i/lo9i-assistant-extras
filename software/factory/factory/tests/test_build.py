"""The build and review stages on a real git repository, in local mode, with
the team and the coding agent faked. A helper answers the approvals the
user would."""

import json

import pytest
from conftest import run
from fakes import FakeAgent, Team, fixes, on_fakes, once

from lo9i_factory import approvals, build, db, git, notify, repo, service, settings
from lo9i_factory.agents import Result
from lo9i_factory.mcp_server import _status
from lo9i_factory.providers import Providers
from lo9i_factory.runner import Runner
from lo9i_factory.sandbox import SandboxError


@pytest.fixture
def conn(tmp_path, monkeypatch, project):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "data" / "factory.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(approvals, "POLL_SECONDS", 0.01)
    (tmp_path / "data").mkdir()
    conn = db.connect()
    on_fakes(conn)
    settings.update(
        conn,
        {
            "run_mode": "local",
            "models.implementer": "anthropic:claude-sonnet-5-5",
            f"repos.{project}": {"test_command": "python3 -c 'print(\"full ok\")'"},
        },
    )
    yield conn
    conn.close()


def to_build(conn, project, team: Team, agent: FakeAgent):
    """A task with its spec and plan approved, and its runner."""
    r = Runner(Providers(env={}, clients={"copilot": team}), make_agent=agent)
    t = service.create_task(conn, str(project), "Add a search cache")
    for _ in ("spec", "plan"):
        once(r, conn)
        service.approve(conn, t.id)
    return t, r


def test_the_agent_builds_the_plan_its_tests_run_and_the_review_accepts_it(conn, project):
    team, agent = Team(), FakeAgent()
    t, r = to_build(conn, project, team, agent)
    once(r, conn)

    task = service.get_task(conn, t.id)
    assert (task.status, task.stage) == ("waiting", "review")
    assert service.pending_checkpoint(conn, t.id).kind == "diff"
    # The agent got the plan, in the task's own worktree, on its branch.
    job = agent.jobs[0]
    assert "# The approved plan\n\nFinal spec" in job.prompt and "Implement the approved plan" in job.prompt
    assert job.sandbox.mode == "local" and job.sandbox.worktree == task.worktree
    assert job.model == "claude-sonnet-5-5" and job.budget_usd == pytest.approx(10)
    assert agent.made == ("claude-code", "sk-ant-test")
    assert "Commands and network access wait for the user" in job.system
    # Its work is committed on the branch, as the factory.
    log = run("git", "log", "--format=%an %s", task.branch, cwd=project).splitlines()
    assert log[0] == "lo9i factory Add a search cache (build 1)"
    assert {c.path for c in service.changes(conn, t.id)} == {"cache.py", "test_cache.py"}
    # The tester's short command ran once the user allowed it; the full tests before the checkpoint.
    runs = [(r.kind, r.passed, r.output.strip()) for r in service.test_runs(conn, t.id)]
    assert runs == [("short", True, "short ok"), ("full", True, "full ok")]
    # The review saw the diff and the tests.
    review = next(req for role, k, req in team.calls if k == "proposal" and role == "reviewer")
    context = review.turns[0].text
    assert "+CACHE = {}" in context and "## Tests (short): `python3 -c" in context and "passed" in context
    assert "## The implementer's report\n\nAdded the cache" in context
    # What it cost, and the log of what the agent did.
    assert service.costs(conn, t.id)["by_role"]["implementer"]["usd"] == 0.5
    lines = [json.loads(line) for line in (db.data_dir() / "logs" / f"task-{t.id}" / "build-1.jsonl").open()]
    assert lines[0]["type"] == "start" and {"type", "tool", "detail", "at"} <= set(lines[1])

    (notice,) = notify.due(conn, task)
    assert notice.text.startswith("Task 1, Add a search cache: the final diff waits for you. 2 files changed, +4 -0.")
    assert "Tests pass." in notice.text and notice.follow_up

    status = _status(conn, t.id)
    assert status["open_in_editor"] == f"vscode://file/{task.worktree}"
    assert {c["path"] for c in status["changes"]} == {"cache.py", "test_cache.py"}
    assert [(r["kind"], r["exit_code"]) for r in status["tests"]] == [("short", 0), ("full", 0)]
    assert status["approvals"] == [] and status["review_points"] == []

    done = service.approve(conn, t.id)
    assert (done.status, done.worktree) == ("done", None)
    assert git.has_branch(str(project), task.branch)
    assert {c.path for c in service.changes(conn, t.id)} == {"cache.py", "test_cache.py"}


def test_the_reviews_points_go_back_to_the_agent_who_answers_each(conn, project):
    team = Team({("judge", "review_verdict"): [fixes("Add a TTL", "Bound the size")]})
    agent = FakeAgent()
    t, r = to_build(conn, project, team, agent)
    once(r, conn)

    assert len(agent.jobs) == 2
    second = agent.jobs[1].prompt
    assert "# Review points to address" in second and "- [1] Add a TTL\n- [2] Bound the size" in second
    points = service.review_points(conn, t.id)
    assert [(p["text"], p["status"], p["reason"]) for p in points] == [
        ("Add a TTL", "resolved", "Done"),
        ("Bound the size", "resolved", "Done"),
    ]
    assert [(s.kind, s.attempt) for s in service.stages(conn, t.id)][2:] == [
        ("build", 1),
        ("review", 1),
        ("build", 2),
        ("review", 2),
    ]
    log = run("git", "log", "--format=%s", service.get_task(conn, t.id).branch, cwd=project).splitlines()
    assert log[:2] == ["Add a search cache (build 2)", "Add a search cache (build 1)"]


def test_fix_loops_stop_at_the_limit_and_the_open_points_go_to_the_user(conn, project):
    settings.update(conn, {"max_fix_loops": 1})
    team = Team({("judge", "review_verdict"): [fixes("Add a TTL"), fixes("Still no TTL")]})
    agent = FakeAgent(results=[Result(ok=True, summary="First"), Result(ok=True, summary="Tried")])
    t, r = to_build(conn, project, team, agent)
    once(r, conn)

    assert len(agent.jobs) == 2
    assert service.pending_checkpoint(conn, t.id).kind == "diff"
    assert [(p["text"], p["status"]) for p in service.review_points(conn, t.id)] == [
        ("Add a TTL", "open"),
        ("Still no TTL", "open"),
    ]


def test_a_rejected_diff_goes_back_to_the_agent_with_the_comment(conn, project):
    agent = FakeAgent()
    t, r = to_build(conn, project, Team(), agent)
    once(r, conn)
    service.reject(conn, t.id, "Use Redis, not a dict")
    once(r, conn)

    assert "# The user rejected the last diff\n\nThey said:\n\nUse Redis, not a dict" in agent.jobs[1].prompt
    assert service.pending_checkpoint(conn, t.id).kind == "diff"


def test_a_failed_agent_pauses_the_task_and_keeps_its_work(conn, project):
    agent = FakeAgent(results=[Result(ok=False, summary="", error="out of context", usd=0.2)])
    t, r = to_build(conn, project, Team(), agent)
    once(r, conn)

    paused = service.get_task(conn, t.id)
    assert (paused.status, paused.reason) == ("paused", "the coding agent failed: out of context")
    assert "cache.py" in run("git", "show", "--stat", "--format=", paused.branch, cwd=project)

    service.resume(conn, t.id)
    once(r, conn)
    assert len(agent.jobs) == 2 and service.pending_checkpoint(conn, t.id).kind == "diff"


def test_a_denied_test_command_doesnt_run(conn, project):
    t, r = to_build(conn, project, Team(), FakeAgent())
    once(r, conn, allow=False)
    assert [run.kind for run in service.test_runs(conn, t.id)] == ["full"]
    assert service.pending_checkpoint(conn, t.id).kind == "diff"


def test_a_build_needs_an_agent_that_runs_the_implementers_model_and_its_key(conn, project, monkeypatch):
    t, r = to_build(conn, project, Team(), FakeAgent())
    settings.update(conn, {"agent": "codex"})
    once(r, conn)
    assert "codex runs openai's models: set models.implementer to openai:<model>" in service.get_task(conn, t.id).reason

    settings.update(conn, {"agent": "claude-code"})
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    service.resume(conn, t.id)
    once(r, conn)
    assert "ANTHROPIC_API_KEY isn't set, and claude-code needs it" in service.get_task(conn, t.id).reason


def test_docker_mode_needs_docker(conn, project, monkeypatch):
    async def no_docker():
        raise SandboxError("Docker isn't running: start Docker Desktop, or set run_mode to local")

    t, r = to_build(conn, project, Team(), FakeAgent())
    settings.update(conn, {"run_mode": "docker"})
    monkeypatch.setattr(build, "check_docker", no_docker)
    once(r, conn)
    assert service.get_task(conn, t.id).reason == "Docker isn't running: start Docker Desktop, or set run_mode to local"
    assert repo.task_test_runs(conn, t.id) == []


def test_a_new_project_starts_from_an_empty_folder(conn, tmp_path):
    """No repository yet: the factory makes one, and the task's branch starts
    empty, so the final diff is all the agent wrote."""
    new = tmp_path / "new-app"
    t, r = to_build(conn, new, Team(), FakeAgent())
    assert (new / ".git").is_dir() and git.commit_of(str(new)) is None
    once(r, conn)

    task = service.get_task(conn, t.id)
    assert service.pending_checkpoint(conn, t.id).kind == "diff"
    assert task.base_commit == git.EMPTY_TREE
    assert {c.path for c in service.changes(conn, t.id)} == {"cache.py", "test_cache.py"}
    assert "+CACHE = {}" in service.diff(conn, t.id)
    # Nothing was added to the project's own branch.
    assert git.commit_of(str(new)) is None and git.has_branch(str(new), task.branch)
    service.approve(conn, t.id)
    assert [c.path for c in service.changes(conn, t.id)] == ["cache.py", "test_cache.py"]


def test_a_repository_with_no_commit_yet(conn, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    run("git", "init", "-q", "-b", "main", cwd=fresh)
    t, r = to_build(conn, fresh, Team(), FakeAgent())
    once(r, conn)
    assert service.pending_checkpoint(conn, t.id).kind == "diff"
    assert len(service.changes(conn, t.id)) == 2
