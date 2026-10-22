import json
import sqlite3

import pytest

from lo9i_factory import db, service
from lo9i_factory.models import CHECKPOINT_STATUSES, ROLES, STAGE_STATUSES, STAGES, TASK_STATUSES

TABLES = {"tasks", "stages", "messages", "decisions", "checkpoints", "review_points", "costs", "settings"}
# What a task leaves in each table, deleted with it.
DEBATE = TABLES - {"tasks", "settings"}


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def debate(conn, task_id):
    """A stage with a bit of everything: a proposal, a critique of it, the
    judge's decision, a checkpoint, a review point and a cost."""
    stage = service.stages(conn, task_id)[0].id
    ts = "2026-10-19T12:00:00+00:00"
    with conn:
        proposal = conn.execute(
            "INSERT INTO messages (task_id, stage_id, round, role, model, type, body, created_at)"
            " VALUES (?, ?, 1, 'architect', 'anthropic:claude-opus-5-5', 'proposal', ?, ?)",
            (task_id, stage, json.dumps({"proposal": "Add a cache", "risks": ["stale reads"]}), ts),
        ).lastrowid
        critique = conn.execute(
            "INSERT INTO messages (task_id, stage_id, round, role, model, type, reply_to, body, created_at)"
            " VALUES (?, ?, 1, 'security', 'openai:gpt-5', 'critique', ?, '{}', ?)",
            (task_id, stage, proposal, ts),
        ).lastrowid
        decision = conn.execute(
            "INSERT INTO decisions (task_id, stage_id, round, model, kind, winner_message_id, rationale, created_at)"
            " VALUES (?, ?, 1, 'anthropic:claude-opus-5-5', 'winner', ?, 'Simplest', ?)",
            (task_id, stage, proposal, ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO review_points (task_id, stage_id, message_id, role, text, created_at, updated_at)"
            " VALUES (?, ?, ?, 'security', 'Bound the cache', ?, ?)",
            (task_id, stage, critique, ts, ts),
        )
        conn.execute(
            "INSERT INTO costs (task_id, stage_id, message_id, role, provider, model, usd, created_at)"
            " VALUES (?, ?, ?, 'architect', 'anthropic', 'claude-opus-5-5', 0.25, ?)",
            (task_id, stage, proposal, ts),
        )
    service.open_checkpoint(conn, task_id, decision)


def test_the_schema_is_created_once_and_versioned(tmp_path):
    path = str(tmp_path / "factory.db")
    db.connect(path).close()
    conn = db.connect(path)  # Again, over the existing tables.
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names >= TABLES
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_database_comes_from_factory_db(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_DB", raising=False)
    with pytest.raises(db.MissingDatabaseError):
        db.connect()
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "env.db"))
    db.connect().close()
    assert (tmp_path / "env.db").exists()


def test_the_checks_take_exactly_the_models_values(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    for status in TASK_STATUSES:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, t.id))
    for stage in (*STAGES, "done"):
        conn.execute("UPDATE tasks SET stage = ? WHERE id = ?", (stage, t.id))
    for status in STAGE_STATUSES:
        conn.execute("UPDATE stages SET status = ? WHERE task_id = ?", (status, t.id))
    for kind in STAGES[1:]:
        conn.execute("INSERT INTO stages (task_id, kind, created_at) VALUES (?, ?, '')", (t.id, kind))
    stage = service.stages(conn, t.id)[0].id
    for role in (*ROLES, "user"):
        conn.execute(
            "INSERT INTO messages (task_id, stage_id, role, type, created_at) VALUES (?, ?, ?, 'comment', '')",
            (t.id, stage, role),
        )
    for status in CHECKPOINT_STATUSES:
        conn.execute(
            "INSERT INTO checkpoints (task_id, stage_id, kind, status, comment, created_at)"
            " VALUES (?, ?, 'spec', ?, 'why', '')",
            (t.id, stage, status),
        )
    for sql in (
        "UPDATE tasks SET status = 'sleeping'",
        "UPDATE tasks SET stage = 'deploy'",
        "UPDATE stages SET status = 'paused'",
        "UPDATE messages SET role = 'manager'",
        "UPDATE messages SET body = 'not json'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


def test_deleting_a_task_deletes_its_whole_debate(conn, git_repo):
    kept = service.create_task(conn, str(git_repo), "Keep me")
    gone = service.create_task(conn, str(git_repo), "Delete me")
    debate(conn, kept.id)
    debate(conn, gone.id)
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('max_rounds', '4', '')")
    before = {t: count(conn, t) for t in TABLES}

    service.delete_task(conn, gone.id)

    assert {t: count(conn, t) for t in DEBATE} == {t: before[t] // 2 for t in DEBATE}
    assert count(conn, "settings") == 1
    assert [t.id for t in service.list_tasks(conn)] == [kept.id]


def test_a_task_has_one_pending_checkpoint_at_a_time(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    debate(conn, t.id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO checkpoints (task_id, stage_id, kind, created_at) VALUES (?, ?, 'spec', '')",
            (t.id, service.stages(conn, t.id)[0].id),
        )


def test_rejections_and_closed_review_points_need_a_reason(conn, git_repo):
    t = service.create_task(conn, str(git_repo), "x")
    debate(conn, t.id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE checkpoints SET status = 'rejected'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_points SET status = 'declined'")
    conn.execute("UPDATE review_points SET status = 'declined', reason = 'Out of scope'")


def test_a_restart_finds_the_task_where_it_was(tmp_path, git_repo):
    """What the runner resumes from: the task's status and stage, and the
    stage's round and step, all saved before it goes on."""
    path = str(tmp_path / "factory.db")
    conn = db.connect(path)
    t = service.create_task(conn, str(git_repo), "x")
    with conn:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (t.id,))
        conn.execute("UPDATE stages SET status = 'running', round = 2, step = 'critiques' WHERE task_id = ?", (t.id,))
    conn.close()

    conn = db.connect(path)
    (stage,) = service.stages(conn, t.id)
    assert service.get_task(conn, t.id).status == "running"
    assert (stage.kind, stage.status, stage.round, stage.step) == ("spec", "running", 2, "critiques")
    assert service.list_tasks(conn, "running")[0].id == t.id
