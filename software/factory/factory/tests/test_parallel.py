"""Several tasks at once, two runners sharing the queue, resuming orphaned
tasks, and cleaning up after finished ones."""

import asyncio
import time

import pytest
from conftest import run
from fakes import Team, on_fakes
from fastapi.testclient import TestClient

from lo9i_factory import cleanup, db, git, repo, runner, service, settings
from lo9i_factory.providers import Providers, Reply, Request
from lo9i_factory.runner import Runner


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "data" / "factory.db"))
    (tmp_path / "data").mkdir()
    conn = db.connect()
    on_fakes(conn)
    yield conn
    conn.close()


class SlowTeam(Team):
    """A team whose every answer takes a moment, counting the tasks it works
    for at once (each task has a stage of its own)."""

    def __init__(self) -> None:
        super().__init__()
        self.now: set[str] = set()
        self.most = 0

    async def complete(self, model: str, request: Request) -> Reply:
        task = request.turns[0].text.split("\n\n")[1]
        self.now.add(task)
        self.most = max(self.most, len(self.now))
        await asyncio.sleep(0.05)
        self.now.discard(task)
        return await super().complete(model, request)


def runner_for(team, **kw) -> Runner:
    return Runner(Providers(env={}, clients={"copilot": team}), poll=0.01, **kw)


async def until(condition, seconds: float = 10) -> None:
    for _ in range(int(seconds * 100)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def run_until(r: Runner, condition) -> None:
    async def go():
        working = asyncio.create_task(r.run())
        try:
            await until(condition)
        finally:
            working.cancel()

    asyncio.run(go())


def statuses(conn) -> list[str]:
    return [t.status for t in reversed(service.list_tasks(conn))]


@pytest.mark.parametrize("most", [1, 2])
def test_tasks_run_side_by_side_up_to_max_parallel(conn, git_repo, most):
    settings.update(conn, {"max_parallel": most})
    for title in ("First task", "Second task"):
        service.create_task(conn, str(git_repo), title)
    team = SlowTeam()
    run_until(runner_for(team), lambda: statuses(conn) == ["waiting", "waiting"])
    assert team.most == most


def test_two_runners_never_take_the_same_task(conn, git_repo):
    settings.update(conn, {"max_parallel": 1})
    first = service.create_task(conn, str(git_repo), "x")
    service.create_task(conn, str(git_repo), "y")
    with conn:
        assert repo.claim(conn, first.id, "a", 1)
        assert not repo.claim(conn, first.id, "b", 1)  # Taken.
        assert not repo.claim(conn, 2, "b", 1)  # Full.
    assert service.get_task(conn, first.id).runner == "a"


def test_a_task_whose_runner_died_goes_on_from_where_it_was(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    alive = service.create_task(conn, str(git_repo), "y")
    with conn:
        repo.update_task(conn, t.id, {"status": "running", "runner": "gone", "heartbeat": "2026-01-01T00:00:00+00:00"})
        repo.update_task(conn, alive.id, {"status": "running", "runner": "other", "heartbeat": repo.now()})
    run_until(runner_for(Team()), lambda: service.get_task(conn, t.id).status == "waiting")
    # Another runner's task, still beating, is its own.
    assert service.get_task(conn, alive.id).status == "running"


def test_a_runner_says_its_tasks_still_run(conn, git_repo, monkeypatch):
    monkeypatch.setattr(runner, "BEAT_SECONDS", 0.01)
    t = service.create_task(conn, str(git_repo), "x")
    with conn:
        repo.claim(conn, t.id, "me", 5)
        repo.update_task(conn, t.id, {"heartbeat": "2026-01-01T00:00:00+00:00"})
    r = runner_for(Team(), name="me")
    r.active[t.id] = None

    async def go():
        beating = asyncio.create_task(r._beat())
        await until(lambda: service.get_task(conn, t.id).heartbeat > "2026-01-02")
        beating.cancel()

    asyncio.run(go())


def test_the_pages_runner_leaves_what_it_cant_run_to_the_mcp_servers(conn, git_repo):
    settings.update(conn, {f"models.{role}": "anthropic:claude-opus-5-5" for role in ("architect", "judge")})
    t = service.create_task(conn, str(git_repo), "x")
    page = Runner(Providers(env={}), fallback=True)
    assert not asyncio.run(page.run_once())
    assert service.get_task(conn, t.id).status == "queued"

    mcp = Runner(Providers(env={}))
    assert asyncio.run(mcp.run_once())
    assert "ANTHROPIC_API_KEY isn't set" in service.get_task(conn, t.id).reason


def test_the_page_process_runs_tasks_and_its_actions_wake_it(conn, git_repo, monkeypatch):
    from lo9i_factory.web import app as web

    monkeypatch.setattr(web, "Runner", lambda fallback: Runner(Providers(clients={"copilot": Team()}), poll=60))
    with TestClient(web.create_app(run_tasks=True)) as client:
        client.post("/tasks", headers={"HX-Request": "true"}, data={"repo": str(git_repo), "description": "x"})
        for _ in range(500):
            if service.get_task(conn, 1).status == "waiting":
                break
            time.sleep(0.01)
    assert service.get_task(conn, 1).status == "waiting"


# --- Cleaning up ---


@pytest.fixture
def built(conn, project, monkeypatch):
    """A task with a worktree, a commit on its branch, and an agent home."""
    monkeypatch.setattr(cleanup, "SETTLE", cleanup.SETTLE * 0)
    t = service.create_task(conn, str(project), "Add a cache")
    path = db.data_dir() / "worktrees" / f"task-{t.id}"
    base = git.add_worktree(str(project), path, t.branch, "HEAD")
    (path / "cache.py").write_text("CACHE = {}\n")
    git.commit(str(path), "build 1")
    home = db.data_dir() / "homes" / f"lo9i-factory-{t.id}-3"
    home.mkdir(parents=True)
    (cleanup.logs(t.id)).mkdir(parents=True)
    with conn:
        repo.update_task(conn, t.id, {"worktree": str(path), "base_commit": base})
    return service.get_task(conn, t.id), path, home


def sweep(conn, busy=frozenset()) -> None:
    asyncio.run(cleanup.sweep(conn, set(busy)))


def test_a_finished_task_loses_its_worktree_and_keeps_its_branch(conn, project, built):
    t, path, home = built
    sweep(conn)
    assert path.exists() and home.exists()  # Not finished: it may need them.

    service.cancel(conn, t.id)
    sweep(conn, busy={t.id})
    assert path.exists()  # Still stopping here.
    sweep(conn)
    assert not path.exists() and not home.exists()
    assert service.get_task(conn, t.id).worktree is None
    assert git.has_branch(str(project), t.branch)
    # Its diff is read from the branch now.
    assert [c.path for c in service.changes(conn, t.id)] == ["cache.py"]
    assert str(path) not in run("git", "worktree", "list", cwd=project)


def test_a_task_cancelled_a_moment_ago_is_left_to_stop(conn, built, monkeypatch):
    t, path, _ = built
    monkeypatch.setattr(cleanup, "SETTLE", cleanup.SETTLE.__class__(seconds=30))
    service.cancel(conn, t.id)
    sweep(conn)
    assert path.exists()


def test_deleting_a_task_removes_its_files_and_keeps_its_branch(conn, project, built):
    t, path, home = built
    service.delete_task(conn, t.id)
    assert not path.exists() and not home.exists() and not cleanup.logs(t.id).exists()
    assert git.has_branch(str(project), t.branch)


def test_containers_are_left_alone_without_docker(monkeypatch):
    from lo9i_factory.sandbox import SandboxError

    def missing(name):
        raise SandboxError(f"{name} isn't installed")

    monkeypatch.setattr(cleanup, "find", missing)
    asyncio.run(cleanup.stop_containers(1))
