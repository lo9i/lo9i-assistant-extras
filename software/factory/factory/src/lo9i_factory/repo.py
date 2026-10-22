"""SQL only. Validation and rules live in `service`."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .models import Approval, Checkpoint, Decision, Message, Stage, Task, TestRun


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def now_precise() -> str:
    """To tell apart events in the same second."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


# --- Tasks ---


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[Task]:
    rows = conn.execute("SELECT * FROM tasks WHERE (?1 IS NULL OR status = ?1) ORDER BY id DESC", (status,))
    return [Task.from_row(r) for r in rows]


def get_task(conn: sqlite3.Connection, id: int) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
    return Task.from_row(row) if row else None


def insert_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    repo: str,
    slug: str,
    include_context: bool,
    run_mode: str | None,
) -> int:
    """The task, with its branch named after the id it gets."""
    ts = now()
    cur = conn.execute(
        "INSERT INTO tasks (title, description, repo, slug, branch, include_context, run_mode, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)",
        (title, description, repo, slug, int(include_context), run_mode, ts, ts),
    )
    id = cur.lastrowid
    conn.execute("UPDATE tasks SET branch = ? WHERE id = ?", (f"factory/{id}-{slug}", id))
    return id


def update_task(conn: sqlite3.Connection, id: int, fields: dict[str, Any]) -> None:
    """UPDATE the given columns. Column names come from code, never input."""
    fields = {**fields, "updated_at": now()}
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*fields.values(), id])


def delete_task(conn: sqlite3.Connection, id: int) -> bool:
    """Its stages, messages, decisions, checkpoints and costs go with it."""
    return conn.execute("DELETE FROM tasks WHERE id = ?", (id,)).rowcount > 0


# --- Stages ---


def list_stages(conn: sqlite3.Connection, task_id: int) -> list[Stage]:
    rows = conn.execute("SELECT * FROM stages WHERE task_id = ? ORDER BY id", (task_id,))
    return [Stage.from_row(r) for r in rows]


def get_stage(conn: sqlite3.Connection, id: int) -> Stage:
    return Stage.from_row(conn.execute("SELECT * FROM stages WHERE id = ?", (id,)).fetchone())


def current_stage(conn: sqlite3.Connection, task_id: int) -> Stage | None:
    """The latest run of a stage: the one the task is in."""
    row = conn.execute("SELECT * FROM stages WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return Stage.from_row(row) if row else None


def insert_stage(conn: sqlite3.Connection, task_id: int, kind: str) -> int:
    """The next attempt of `kind`: 1, or one more than the last."""
    cur = conn.execute(
        "INSERT INTO stages (task_id, kind, attempt, created_at)"
        " SELECT ?1, ?2, COALESCE(MAX(attempt), 0) + 1, ?3 FROM stages WHERE task_id = ?1 AND kind = ?2",
        (task_id, kind, now()),
    )
    return cur.lastrowid


def update_stage(conn: sqlite3.Connection, id: int, fields: dict[str, Any]) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE stages SET {sets} WHERE id = ?", [*fields.values(), id])


def cancel_stages(conn: sqlite3.Connection, task_id: int, status: str = "cancelled") -> None:
    """Stop the stages not finished yet, as cancelled or failed."""
    conn.execute(
        "UPDATE stages SET status = ?, finished_at = ?"
        " WHERE task_id = ? AND status IN ('pending', 'running', 'waiting')",
        (status, now(), task_id),
    )


# --- Checkpoints ---


def get_checkpoint(conn: sqlite3.Connection, id: int) -> Checkpoint:
    return Checkpoint.from_row(conn.execute("SELECT * FROM checkpoints WHERE id = ?", (id,)).fetchone())


def stage_checkpoint(conn: sqlite3.Connection, stage_id: int) -> Checkpoint | None:
    """The checkpoint a run of a stage ended at, if it got there."""
    row = conn.execute("SELECT * FROM checkpoints WHERE stage_id = ? ORDER BY id DESC LIMIT 1", (stage_id,)).fetchone()
    return Checkpoint.from_row(row) if row else None


def pending_checkpoint(conn: sqlite3.Connection, task_id: int) -> Checkpoint | None:
    row = conn.execute("SELECT * FROM checkpoints WHERE task_id = ? AND status = 'pending'", (task_id,)).fetchone()
    return Checkpoint.from_row(row) if row else None


def insert_checkpoint(conn: sqlite3.Connection, task_id: int, stage_id: int, kind: str, decision_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO checkpoints (task_id, stage_id, kind, decision_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, stage_id, kind, decision_id, now()),
    )
    return cur.lastrowid


def decide_checkpoint(conn: sqlite3.Connection, id: int, status: str, comment: str = "") -> None:
    conn.execute(
        "UPDATE checkpoints SET status = ?, comment = ?, decided_at = ? WHERE id = ?", (status, comment, now(), id)
    )


# --- Messages ---


def insert_comment(conn: sqlite3.Connection, task_id: int, stage_id: int, body: str, round: int = 0) -> int:
    """The user's comment in a stage's thread, which every agent sees. Round
    0 is context, from before the debate started; a later one gets answered."""
    return insert_message(
        conn, task_id=task_id, stage_id=stage_id, round=round, role="user", type="comment", body={"text": body}
    )


def insert_message(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    stage_id: int,
    round: int,
    role: str,
    type: str,
    body: dict,
    model: str | None = None,
    to_role: str | None = None,
    reply_to: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO messages (task_id, stage_id, round, role, model, type, to_role, reply_to, body, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, stage_id, round, role, model, type, to_role, reply_to, json.dumps(body), now()),
    )
    return cur.lastrowid


def stage_messages(conn: sqlite3.Connection, stage_id: int) -> list[Message]:
    """A stage's thread, in the order it was written."""
    rows = conn.execute("SELECT * FROM messages WHERE stage_id = ? ORDER BY id", (stage_id,))
    return [Message.from_row(r) for r in rows]


# --- Decisions ---


def insert_decision(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    stage_id: int,
    round: int,
    model: str,
    kind: str,
    rationale: str,
    body: dict | None = None,
    winner_message_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (task_id, stage_id, round, model, kind, winner_message_id, rationale, body, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, stage_id, round, model, kind, winner_message_id, rationale, json.dumps(body or {}), now()),
    )
    return cur.lastrowid


def get_decision(conn: sqlite3.Connection, id: int) -> Decision | None:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (id,)).fetchone()
    return Decision.from_row(row) if row else None


def stage_decisions(conn: sqlite3.Connection, stage_id: int) -> list[Decision]:
    rows = conn.execute("SELECT * FROM decisions WHERE stage_id = ? ORDER BY id", (stage_id,))
    return [Decision.from_row(r) for r in rows]


# --- Costs ---


def cost_totals(conn: sqlite3.Connection, task_id: int) -> dict[str, float | int]:
    """USD (direct APIs) and requests (Copilot) a task has spent."""
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) AS usd, COALESCE(SUM(requests), 0) AS requests FROM costs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return {"usd": round(row["usd"], 4), "requests": row["requests"]}


def spent_usd(conn: sqlite3.Connection, task_id: int) -> float:
    """Unrounded, to hold against the task's limit."""
    return conn.execute("SELECT COALESCE(SUM(usd), 0) FROM costs WHERE task_id = ?", (task_id,)).fetchone()[0]


def cost_breakdown(conn: sqlite3.Connection, task_id: int, by: str) -> dict[str, dict[str, float | int]]:
    """USD and requests per stage (`by` "stage") or per role ("role")."""
    column = {"stage": "COALESCE(stages.kind, '')", "role": "costs.role"}[by]
    rows = conn.execute(
        f"SELECT {column} AS key, SUM(costs.usd) AS usd, SUM(costs.requests) AS requests"
        " FROM costs LEFT JOIN stages ON stages.id = costs.stage_id WHERE costs.task_id = ?"
        " GROUP BY key ORDER BY MIN(costs.id)",
        (task_id,),
    )
    return {r["key"]: {"usd": round(r["usd"], 4), "requests": r["requests"]} for r in rows}


def insert_cost(conn: sqlite3.Connection, **fields: Any) -> int:
    """A call's cost; fields are costs' columns, named by code."""
    fields = {**fields, "created_at": now()}
    cur = conn.execute(
        f"INSERT INTO costs ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})", list(fields.values())
    )
    return cur.lastrowid


def link_cost(conn: sqlite3.Connection, cost_id: int, message_id: int) -> None:
    """The message a call wrote, for the cost per message."""
    conn.execute("UPDATE costs SET message_id = ? WHERE id = ?", (message_id, cost_id))


# --- Test runs ---


def insert_test_run(conn: sqlite3.Connection, **fields: Any) -> int:
    fields = {**fields, "created_at": now()}
    cur = conn.execute(
        f"INSERT INTO test_runs ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})", list(fields.values())
    )
    return cur.lastrowid


def task_test_runs(conn: sqlite3.Connection, task_id: int) -> list[TestRun]:
    rows = conn.execute("SELECT * FROM test_runs WHERE task_id = ? ORDER BY id", (task_id,))
    return [TestRun.from_row(r) for r in rows]


def last_full_run(conn: sqlite3.Connection, repo: str) -> TestRun | None:
    """The latest full test run of any task in the repository."""
    row = conn.execute(
        "SELECT test_runs.* FROM test_runs JOIN tasks ON tasks.id = test_runs.task_id"
        " WHERE tasks.repo = ? AND test_runs.kind = 'full' ORDER BY test_runs.id DESC LIMIT 1",
        (repo,),
    ).fetchone()
    return TestRun.from_row(row) if row else None


# --- Approvals ---


def insert_approval(conn: sqlite3.Connection, task_id: int, stage_id: int, tool: str, detail: str) -> int:
    cur = conn.execute(
        "INSERT INTO approvals (task_id, stage_id, tool, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, stage_id, tool, detail, now()),
    )
    return cur.lastrowid


def get_approval(conn: sqlite3.Connection, id: int) -> Approval | None:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (id,)).fetchone()
    return Approval.from_row(row) if row else None


def pending_approvals(conn: sqlite3.Connection, task_id: int) -> list[Approval]:
    rows = conn.execute("SELECT * FROM approvals WHERE task_id = ? AND status = 'pending' ORDER BY id", (task_id,))
    return [Approval.from_row(r) for r in rows]


def decide_approval(conn: sqlite3.Connection, id: int, status: str, reason: str) -> None:
    conn.execute(
        "UPDATE approvals SET status = ?, reason = ?, decided_at = ? WHERE id = ?", (status, reason, now(), id)
    )


# --- Review points ---


def insert_review_point(conn: sqlite3.Connection, task_id: int, stage_id: int, role: str, text: str) -> int:
    ts = now()
    cur = conn.execute(
        "INSERT INTO review_points (task_id, stage_id, role, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, stage_id, role, text, ts, ts),
    )
    return cur.lastrowid


def review_points(conn: sqlite3.Connection, task_id: int, status: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT id, stage_id, role, text, status, reason FROM review_points"
        " WHERE task_id = ?1 AND (?2 IS NULL OR status = ?2) ORDER BY id",
        (task_id, status),
    )
    return [dict(r) for r in rows]


def answer_review_point(conn: sqlite3.Connection, id: int, status: str, reason: str) -> None:
    conn.execute(
        "UPDATE review_points SET status = ?, reason = ?, updated_at = ? WHERE id = ?", (status, reason, now(), id)
    )


# --- Notices ---


def told(conn: sqlite3.Connection, task_id: int) -> set[str]:
    """The keys of what the user was told of a task."""
    return {r[0] for r in conn.execute("SELECT key FROM notices WHERE task_id = ?", (task_id,))}


def tell(conn: sqlite3.Connection, task_id: int, key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO notices (task_id, key, created_at) VALUES (?, ?, ?)", (task_id, key, now()))


def latest_origin(conn: sqlite3.Connection) -> dict | None:
    """The conversation the user last drove any task from."""
    row = conn.execute(
        "SELECT origin FROM tasks WHERE origin IS NOT NULL ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row[0]) if row else None


# --- Runners ---


def claim(conn: sqlite3.Connection, task_id: int, runner: str, most: int) -> bool:
    """Take a queued task to run, unless `most` tasks run already, all
    runners counted. Atomic: of two runners, one gets it."""
    ts = now()
    cur = conn.execute(
        "UPDATE tasks SET status = 'running', runner = ?, heartbeat = ?, updated_at = ?"
        " WHERE id = ? AND status = 'queued' AND (SELECT COUNT(*) FROM tasks WHERE status = 'running') < ?",
        (runner, ts, ts, task_id, most),
    )
    return cur.rowcount > 0


def beat(conn: sqlite3.Connection, runner: str, task_ids: list[int]) -> None:
    """The runner still works on these."""
    conn.executemany(
        "UPDATE tasks SET heartbeat = ? WHERE id = ? AND runner = ?", [(now(), id, runner) for id in task_ids]
    )


def requeue_stale(conn: sqlite3.Connection, before: str) -> list[int]:
    """Running tasks whose runner went quiet before `before` (it died, or
    lo9i restarted) go back in the queue."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'running' AND (heartbeat IS NULL OR heartbeat < ?)", (before,)
    ).fetchall()
    ids = [r[0] for r in rows]
    conn.executemany("UPDATE tasks SET status = 'queued', runner = NULL WHERE id = ?", [(i,) for i in ids])
    return ids
