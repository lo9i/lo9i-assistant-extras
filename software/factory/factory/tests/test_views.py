"""What the task's page says is going on, read from the task's saved state:
the progress steps, the "now" sentence, where a debate stands."""

from datetime import UTC, datetime, timedelta

import pytest

from lo9i_factory import build, db, repo, service
from lo9i_factory.web import views


@pytest.fixture
def conn(tmp_path, monkeypatch):
    # A database in a folder, so builds' logs have one to go in.
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "data" / "factory.db"))
    (tmp_path / "data").mkdir()
    conn = db.connect()
    yield conn
    conn.close()


def at(conn, git_repo, kind: str, status: str = "running", **stage):
    """A task at a running stage of `kind`, the stages before it approved."""
    t = service.create_task(conn, str(git_repo), "Add a search cache")
    with conn:
        stage_id = repo.list_stages(conn, t.id)[0].id
        for later in ("plan", "build", "review")[: ("spec", "plan", "build", "review").index(kind)]:
            repo.update_stage(conn, stage_id, {"status": "approved", "finished_at": repo.now()})
            stage_id = repo.insert_stage(conn, t.id, later)
        repo.update_stage(conn, stage_id, {"status": "running", "started_at": repo.now(), **stage})
        repo.update_task(conn, t.id, {"stage": kind, "status": status})
    return service.get_task(conn, t.id), stage_id


def say(conn, t, stage_id, role, type, body, round=1, **kw) -> int:
    with conn:
        return repo.insert_message(
            conn, task_id=t.id, stage_id=stage_id, round=round, role=role, type=type, body=body, **kw
        )


def now(conn, t, **kw) -> views.Now:
    t = service.get_task(conn, t.id)
    return views.now(
        t, views.stages(conn, t), service.pending_checkpoint(conn, t.id), service.approvals(conn, t.id), **kw
    )


def steps(conn, t) -> list[tuple[str, str]]:
    t = service.get_task(conn, t.id)
    found = views.steps(
        t, views.stages(conn, t), service.pending_checkpoint(conn, t.id), len(service.approvals(conn, t.id))
    )
    return [(s.key, s.state) for s in found]


PROPOSAL = {"proposal": "## Cache\n\nAn **LRU** in `cache.py`.", "risks": []}


def test_one_line_is_the_first_line_that_says_something():
    assert views.one_line("## Cache\n\nAn **LRU** in `cache.py`, see [the docs](http://x).") == (
        "An LRU in cache.py, see the docs."
    )
    assert views.one_line("# Only a heading") == "Only a heading"
    assert views.one_line("```python\nx = 1\n```") == "x = 1"
    assert views.one_line("- a point\n- another") == "a point"
    assert views.one_line("word " * 50, limit=20) == "word word word word…"
    assert views.one_line(None) == "" and views.one_line("---\n\n") == ""


def test_elapsed_reads_roughly():
    start = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    after = lambda **kw: (start + timedelta(**kw)).isoformat()  # noqa: E731
    assert views.elapsed(start.isoformat(), after(seconds=45)) == "45 s"
    assert views.elapsed(start.isoformat(), after(minutes=4, seconds=59)) == "4 min"
    assert views.elapsed(start.isoformat(), after(hours=1, minutes=5)) == "1 h 5 min"
    assert views.elapsed(start.isoformat(), after(hours=2)) == "2 h"
    assert views.elapsed(start.isoformat(), after(days=1, hours=3)) == "1 d 3 h"
    assert views.elapsed(None) == ""


def test_the_steps_follow_the_task(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "Add a search cache")
    assert steps(conn, t) == [
        ("spec", "queued"), ("plan", "upcoming"), ("build", "upcoming"), ("review", "upcoming"), ("diff", "upcoming")
    ]  # fmt: skip

    t, stage_id = at(conn, git_repo, "plan")
    assert steps(conn, t)[:3] == [("spec", "done"), ("plan", "running"), ("build", "upcoming")]
    with conn:
        decision = repo.insert_decision(
            conn, task_id=t.id, stage_id=stage_id, round=1, model="m", kind="winner", rationale="Simplest"
        )
    service.open_checkpoint(conn, t.id, decision)
    assert steps(conn, t)[1] == ("plan", "waiting")


def test_the_final_diff_is_a_step_of_its_own_and_a_build_counts_its_runs(conn, git_repo):
    t, _ = at(conn, git_repo, "review")
    with conn:
        # The review sent fixes back: a second build, then a second review.
        repo.insert_stage(conn, t.id, "build")
        stage_id = repo.insert_stage(conn, t.id, "review")
        decision = repo.insert_decision(
            conn, task_id=t.id, stage_id=stage_id, round=1, model="m", kind="accept", rationale="Good"
        )
    service.open_checkpoint(conn, t.id, decision)
    found = views.steps(service.get_task(conn, t.id), views.stages(conn, t), service.pending_checkpoint(conn, t.id))
    assert [(s.key, s.state) for s in found][3:] == [("review", "done"), ("diff", "waiting")]
    assert found[2].runs == 2

    with conn:
        repo.update_task(conn, t.id, {"status": "done", "finished_at": repo.now()})
    assert {state for _, state in steps(conn, t)} == {"done"}


def test_what_the_agent_asks_makes_its_step_wait_for_you(conn, git_repo):
    t, stage_id = at(conn, git_repo, "build")
    with conn:
        repo.insert_approval(conn, t.id, stage_id, "Bash", "rm -rf build")
    assert steps(conn, t)[2] == ("build", "waiting")
    found = now(conn, t)
    assert (found.text, found.detail, found.state) == (
        "Waiting for you to allow the agent's Bash step",
        "rm -rf build",
        "waiting",
    )
    assert found.clock.startswith("waiting for ")


def test_now_follows_a_plan_debate_through_its_steps(conn, git_repo):
    t, stage_id = at(conn, git_repo, "plan")
    first = now(conn, t)
    assert first.text == "Round 1 of the plan: the architect, security agent and tester are each writing a proposal"
    assert first.clock.endswith(" in this stage") and first.state == "running"

    say(conn, t, stage_id, "architect", "proposal", PROPOSAL)
    assert now(conn, t).detail == "1 of 3 done; waiting on the security agent and tester"

    asked = say(conn, t, stage_id, "security", "proposal", PROPOSAL)
    say(conn, t, stage_id, "security", "question", {"question": "Load tests?"}, to_role="tester", reply_to=asked)
    say(conn, t, stage_id, "tester", "proposal", PROPOSAL)
    with conn:
        repo.update_stage(conn, stage_id, {"round": 1, "step": "proposals"})
    found = now(conn, t)
    assert found.text == "Round 1 of the plan: the tester is answering the security agent's question"
    assert found.detail == "Load tests?"

    say(conn, t, stage_id, "tester", "answer", {"answer": "Yes"}, to_role="security", reply_to=asked + 1)
    assert (
        now(conn, t).text
        == "Round 1 of the plan: the architect, security agent and tester are critiquing each other's proposals"
    )
    with conn:
        repo.update_stage(conn, stage_id, {"step": "critiques"})
    assert now(conn, t).text.startswith("Round 1 of the plan: the architect, security agent and tester are revising")
    with conn:
        repo.update_stage(conn, stage_id, {"step": "revisions"})
    assert now(conn, t).text == "Round 1 of the plan: the judge is weighing the proposals"
    with conn:
        repo.update_stage(conn, stage_id, {"round": 2, "step": None})
    assert now(conn, t).text.startswith("Round 2 of the plan: the architect, security agent and tester are critiquing")

    with conn:
        repo.insert_comment(conn, t.id, stage_id, "Only the search page", round=2)
    assert now(conn, t).text == "Round 2 of the plan: the team is answering your comment"


def test_now_for_the_spec_and_the_review(conn, git_repo):
    t, _ = at(conn, git_repo, "spec")
    assert now(conn, t).text == "The architect is writing the spec"
    t, _ = at(conn, git_repo, "review")
    assert (
        now(conn, t).text
        == "Round 1 of the review: the reviewer, security agent and tester are each reviewing the diff"
    )


def test_now_follows_a_build(conn, git_repo):
    t, stage_id = at(conn, git_repo, "build")
    assert now(conn, t).text.startswith("Getting the build ready")

    log = build.Log(t.id, repo.get_stage(conn, stage_id))
    log({"type": "start", "agent": "claude-code", "model": "claude-sonnet-5-5", "mode": "docker"})
    log({"type": "tool", "tool": "Edit", "detail": "cache.py"})
    found = now(conn, t, changed=3)
    assert (found.text, found.detail) == ("Claude Code is building (3 files changed so far)", "Latest: Edit cache.py")
    assert now(conn, t, changed=1).text == "Claude Code is building (1 file changed so far)"

    with conn:
        repo.update_stage(conn, stage_id, {"step": "agent"})
    assert now(conn, t).text == "The tester is picking which tests of the change to run"
    log({"type": "tests", "kind": "short", "command": "pytest tests/test_cache.py"})
    found = now(conn, t)
    assert (found.text, found.detail) == ("Running the new tests: the change's own", "pytest tests/test_cache.py")
    with conn:
        repo.update_stage(conn, stage_id, {"step": "tests"})
    assert now(conn, t).text == "Checking whether to run all the tests now"
    log({"type": "tests", "kind": "full", "command": "make test"})
    assert now(conn, t).text == "Running all the tests"


def test_a_build_sent_back_says_it_fixes_the_review_points(conn, git_repo):
    t, stage_id = at(conn, git_repo, "build", attempt=2)
    build.Log(t.id, repo.get_stage(conn, stage_id))({"type": "start", "agent": "codex", "model": "m", "mode": "local"})
    assert now(conn, t, open_points=2).text == "Codex is fixing the review's 2 points"


def test_now_when_it_waits_or_stops(conn, git_repo):
    t, stage_id = at(conn, git_repo, "plan")
    with conn:
        decision = repo.insert_decision(
            conn, task_id=t.id, stage_id=stage_id, round=1, model="m", kind="winner", rationale="Simplest"
        )
    service.open_checkpoint(conn, t.id, decision)
    found = now(conn, t)
    assert (found.text, found.state) == ("Waiting for your approval of the plan", "waiting")

    t, _ = at(conn, git_repo, "build")
    service.pause(conn, t.id, "reached max_usd")
    found = now(conn, t)
    assert found.text.startswith("Paused at the build stage") and found.state == "paused"
    assert found.clock.startswith("paused for ")

    t = service.create_task(conn, str(git_repo), "x")
    assert now(conn, t).text == "Queued: the spec starts as soon as the factory is free"


def test_where_a_debate_stands(conn, git_repo):
    t, stage_id = at(conn, git_repo, "plan", round=2)
    with conn:
        repo.insert_decision(
            conn, task_id=t.id, stage_id=stage_id, round=1, model="m", kind="continue", rationale="No TTL agreed"
        )
    critique = {
        "critiques": [
            {"of": "architect", "strengths": "simple", "weaknesses": "**no** TTL", "suggestions": ""},
            {"of": "tester", "strengths": "", "weaknesses": "too slow", "suggestions": ""},
        ]
    }
    say(conn, t, stage_id, "security", "critique", critique, round=2)
    say(conn, t, stage_id, "architect", "revision", {**PROPOSAL, "changes": "TTL"}, round=2)
    asked = say(conn, t, stage_id, "tester", "question", {"question": "Which TTL?"}, round=2, to_role="architect")
    say(conn, t, stage_id, "architect", "answer", {"answer": "5 min"}, round=2, reply_to=asked)

    found = [(p.kind, p.who, p.text, p.status, p.about) for p in views.stages(conn, t)[-1].points]
    assert found == [
        ("verdict", "judge", "No TTL agreed", "open", None),
        ("question", "tester", "Which TTL?", "answered", "architect"),
        ("critique", "security", "no TTL", "revised", "architect"),
        ("critique", "security", "too slow", "open", "tester"),
    ]
    assert [m.role for m in views.stages(conn, t)[-1].proposals] == ["architect"]
