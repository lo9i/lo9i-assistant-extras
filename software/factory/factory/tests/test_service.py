import json

import pytest

from lo9i_factory import service
from lo9i_factory.errors import ConflictError, NotFoundError, ValidationError


def stage_runs(conn, id):
    return [(s.kind, s.attempt, s.status) for s in service.stages(conn, id)]


def thread(conn, id):
    return [
        (r["role"], r["type"], json.loads(r["body"]))
        for r in conn.execute("SELECT * FROM messages WHERE task_id = ?", (id,))
    ]


def test_a_new_task_is_queued_at_its_spec_stage(conn, git_repo):
    t = service.create_task(conn, f"{git_repo}/", "Añadir caché a la búsqueda\n\nThe search is slow.")
    assert (t.id, t.title, t.slug, t.branch) == (
        1,
        "Añadir caché a la búsqueda",
        "anadir-cache-a-la-busqueda",
        "factory/1-anadir-cache-a-la-busqueda",
    )
    assert (t.repo, t.stage, t.status, t.column, t.worktree) == (str(git_repo), "spec", "queued", "spec", None)
    assert (t.include_context, t.run_mode) == (False, None)
    assert stage_runs(conn, t.id) == [("spec", 1, "pending")]
    assert service.costs(conn, t.id) == {"usd": 0, "requests": 0, "by_stage": {}, "by_role": {}}


def test_a_task_takes_a_title_context_and_run_mode(conn, git_repo):
    t = service.create_task(
        conn, str(git_repo), "Long description", title="Cache " + "x" * 100, include_context=True, run_mode="local"
    )
    assert len(t.title) == service.TITLE_LENGTH and len(t.slug) <= service.SLUG_LENGTH
    assert (t.include_context, t.run_mode) == (True, "local")
    assert service.slugify("¿?") == "task"


@pytest.mark.parametrize(
    ("repo", "description", "run_mode", "error"),
    [
        ("{repo}/missing/inside", "x", None, "no folder"),
        ("{tmp}", "x", None, "has files but isn't a git repository: run git init there"),
        ("relative/path", "x", None, "absolute path"),
        ("{repo}", "  \n ", None, "describe the task"),
        ("{repo}", "x", "vm", "run_mode must be one of docker, local"),
    ],
)
def test_bad_tasks_say_why(conn, git_repo, tmp_path, repo, description, run_mode, error):
    with pytest.raises(ValidationError, match=error):
        service.create_task(conn, repo.format(repo=git_repo, tmp=tmp_path), description, run_mode=run_mode)
    assert service.list_tasks(conn) == []


def test_approving_each_checkpoint_walks_the_task_to_done(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")

    c = service.open_checkpoint(conn, t.id)
    assert (c.kind, c.status) == ("spec", "pending")
    assert (service.get_task(conn, t.id).status, service.get_task(conn, t.id).column) == ("waiting", "waiting")
    service.approve(conn, t.id, "Looks right")
    assert (service.get_task(conn, t.id).stage, service.get_task(conn, t.id).status) == ("plan", "queued")

    service.open_checkpoint(conn, t.id)
    service.approve(conn, t.id)
    assert service.get_task(conn, t.id).column == "build"
    # The runner moves a task from its build to its review.
    conn.execute("INSERT INTO stages (task_id, kind, created_at) VALUES (?, 'review', '')", (t.id,))
    assert service.open_checkpoint(conn, t.id).kind == "diff"
    done = service.approve(conn, t.id)

    assert (done.stage, done.status, done.column) == ("done", "done", "done") and done.finished_at
    assert stage_runs(conn, t.id) == [
        ("spec", 1, "approved"),
        ("plan", 1, "approved"),
        ("build", 1, "pending"),
        ("review", 1, "approved"),
    ]
    assert conn.execute("SELECT comment FROM checkpoints WHERE kind = 'spec'").fetchone()[0] == "Looks right"


def test_rejecting_runs_the_stage_again_with_the_comment(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    service.open_checkpoint(conn, t.id)
    again = service.reject(conn, t.id, "  Keep the old API  ")

    assert (again.stage, again.status) == ("spec", "queued")
    assert stage_runs(conn, t.id) == [("spec", 1, "rejected"), ("spec", 2, "pending")]
    assert thread(conn, t.id) == [("user", "comment", {"text": "Keep the old API"})]
    second = service.stages(conn, t.id)[1].id
    assert conn.execute("SELECT stage_id FROM messages").fetchone()[0] == second


def test_a_rejected_diff_goes_back_to_the_build(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    conn.execute("UPDATE tasks SET stage = 'review' WHERE id = ?", (t.id,))
    conn.execute(
        "INSERT INTO stages (task_id, kind, created_at) VALUES (?, 'build', ''), (?, 'review', '')", (t.id, t.id)
    )
    service.open_checkpoint(conn, t.id)
    assert service.reject(conn, t.id, "Tests fail on Linux").stage == "build"
    assert stage_runs(conn, t.id)[-1] == ("build", 2, "pending")


def test_only_a_waiting_task_is_approved_or_rejected(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    with pytest.raises(ConflictError, match="task 1 has no checkpoint waiting for you; it's queued, at spec"):
        service.approve(conn, t.id)
    service.open_checkpoint(conn, t.id)
    with pytest.raises(ValidationError, match="say what to change"):
        service.reject(conn, t.id, " ")
    with pytest.raises(ConflictError, match="can't wait"):
        service.open_checkpoint(conn, t.id)


def test_the_build_has_no_checkpoint(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    conn.execute("INSERT INTO stages (task_id, kind, created_at) VALUES (?, 'build', '')", (t.id,))
    with pytest.raises(ConflictError, match="the build stage has no checkpoint"):
        service.open_checkpoint(conn, t.id)


def test_cancelling_stops_the_task_and_its_checkpoint(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    service.open_checkpoint(conn, t.id)
    cancelled = service.cancel(conn, t.id)

    assert (cancelled.status, cancelled.column) == ("cancelled", "done") and cancelled.finished_at
    assert stage_runs(conn, t.id) == [("spec", 1, "cancelled")]
    assert service.pending_checkpoint(conn, t.id) is None
    with pytest.raises(ConflictError, match="already cancelled"):
        service.cancel(conn, t.id)
    with pytest.raises(ConflictError):
        service.approve(conn, t.id)


def test_deleting_a_task(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    assert service.delete_task(conn, t.id).title == "x"
    with pytest.raises(NotFoundError, match="no task 1"):
        service.get_task(conn, t.id)
    with pytest.raises(NotFoundError):
        service.delete_task(conn, t.id)


def test_listing_tasks(conn, git_repo):
    first = service.create_task(conn, str(git_repo), "first")
    second = service.create_task(conn, str(git_repo), "second")
    service.cancel(conn, first.id)
    assert [t.id for t in service.list_tasks(conn)] == [second.id, first.id]
    assert [t.id for t in service.list_tasks(conn, "queued")] == [second.id]
    with pytest.raises(ValidationError, match="status must be one of queued"):
        service.list_tasks(conn, "stuck")
