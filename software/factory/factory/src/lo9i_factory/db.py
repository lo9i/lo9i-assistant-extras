"""SQLite connection and schema.

Everything a task is lives here, written before the runner goes on, so a
restart resumes each task from its last saved step (SPEC §10): the task's
stage and status, each stage's round and step, and every message. Deleting a
task deletes its whole debate with it (ON DELETE CASCADE).
"""

import os
import sqlite3
from pathlib import Path

# Raised with each change to SCHEMA; connect() will migrate older databases.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    -- The local repository, an absolute path.
    repo TEXT NOT NULL,
    slug TEXT NOT NULL,
    -- factory/<id>-<slug>, left in the repo when the task is done.
    branch TEXT NOT NULL,
    -- Its git worktree while it runs.
    worktree TEXT,
    -- The commit the branch started from, which the diff is against.
    base_commit TEXT,
    -- The conversation in lo9i it was last driven from (JSON, lo9i's
    -- "lo9i/origin"): where its notices go.
    origin TEXT,
    -- Send the repo's README, tree and CLAUDE.md/AGENTS.md to the team.
    include_context INTEGER NOT NULL DEFAULT 0 CHECK (include_context IN (0, 1)),
    -- NULL follows Settings.
    run_mode TEXT CHECK (run_mode IN ('docker', 'local')),
    stage TEXT NOT NULL DEFAULT 'spec' CHECK (stage IN ('spec', 'plan', 'build', 'review', 'done')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'waiting', 'paused', 'done', 'failed', 'cancelled')),
    -- Why it's paused (a limit reached) or failed.
    reason TEXT NOT NULL DEFAULT '',
    -- When it last paused: each pause is told once.
    paused_at TEXT,
    -- While it runs: the runner working on it (runner.py), and when that
    -- runner last said it still is. A stale one is taken to be dead.
    runner TEXT,
    heartbeat TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

-- Each run of a stage. A rejected checkpoint or a fix loop runs it again as
-- the next attempt; the earlier ones stay, with their messages.
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('spec', 'plan', 'build', 'review')),
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'waiting', 'approved', 'rejected', 'done', 'failed', 'cancelled')),
    -- Where the runner is: the debate round, and the last step it finished
    -- in it (proposals, critiques, revisions, judge...).
    round INTEGER NOT NULL DEFAULT 0,
    step TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (task_id, kind, attempt)
);
CREATE INDEX IF NOT EXISTS stages_task ON stages (task_id);

-- The debate: each proposal, critique, revision, vote, directed question and
-- answer, and the user's comments. body is the message as JSON, in the shape
-- of its type.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    round INTEGER NOT NULL DEFAULT 0,
    -- A role of the team, or the user.
    role TEXT NOT NULL
        CHECK (role IN ('architect', 'implementer', 'reviewer', 'tester', 'security', 'judge', 'user')),
    -- provider:model it ran on; NULL for the user.
    model TEXT,
    type TEXT NOT NULL
        CHECK (type IN (
            'proposal', 'critique', 'revision', 'vote', 'question', 'answer', 'comment', 'review', 'report'
        )),
    -- A directed question's addressee.
    to_role TEXT,
    -- The question an answer answers, the message a critique is about.
    reply_to INTEGER REFERENCES messages (id) ON DELETE CASCADE,
    body TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(body)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_stage ON messages (stage_id, id);
CREATE INDEX IF NOT EXISTS messages_task ON messages (task_id);

-- The judge's word on a debate: after a round, continue (no consensus yet,
-- rationale says why); at the end, the winner (picked or merged) of a spec or
-- plan, or accept or fixes for a diff. body holds the result and the losing
-- options.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    round INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('continue', 'winner', 'accept', 'fixes')),
    winner_message_id INTEGER REFERENCES messages (id) ON DELETE SET NULL,
    rationale TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(body)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_stage ON decisions (stage_id);

-- Where the task waits for the user: the spec, the plan, the final diff.
-- One pending at a time per task.
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('spec', 'plan', 'diff')),
    decision_id INTEGER REFERENCES decisions (id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    -- A rejection says what to change.
    CHECK (status <> 'rejected' OR comment <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS checkpoints_pending ON checkpoints (task_id) WHERE status = 'pending';

-- The review's feedback to the implementer, one point each: resolved or
-- declined, always with a reason.
CREATE TABLE IF NOT EXISTS review_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    -- The critique it came from.
    message_id INTEGER REFERENCES messages (id) ON DELETE SET NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'declined')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (status = 'open' OR reason <> '')
);
CREATE INDEX IF NOT EXISTS review_points_stage ON review_points (stage_id);

-- What each call cost: USD for the direct APIs, requests for Copilot. Summed
-- per task, stage and role.
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER REFERENCES stages (id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages (id) ON DELETE SET NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    -- The model that answered, which a refusal fallback may change.
    model TEXT NOT NULL,
    -- input_tokens leaves out the prompt cache's, priced apart.
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS costs_task ON costs (task_id);

-- Each run of a test command: the tester's short run of the change's own
-- tests, or the repository's full test command.
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('short', 'full')),
    command TEXT NOT NULL,
    -- The commit it ran on.
    commit_sha TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    seconds REAL NOT NULL,
    -- Its last lines.
    output TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS test_runs_task ON test_runs (task_id);

-- In local mode, what the agent asks to do beyond editing its worktree (a
-- command, the network, a file elsewhere), waiting for the user's answer.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    stage_id INTEGER NOT NULL REFERENCES stages (id) ON DELETE CASCADE,
    -- The agent's tool, like Bash or WebFetch, or "test command".
    tool TEXT NOT NULL,
    -- What it would do: the command, the URL, the path.
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'allowed', 'denied')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS approvals_pending ON approvals (task_id) WHERE status = 'pending';

-- What the user was told of a task (notify.py), by key, like checkpoint:3:
-- told once, across restarts.
CREATE TABLE IF NOT EXISTS notices (
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, key)
);

-- Settings the user changed, by key (settings.py); the rest are its defaults.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (json_valid(value)),
    updated_at TEXT NOT NULL
);
"""


class MissingDatabaseError(RuntimeError):
    """FACTORY_DB isn't set."""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database (`path`, or FACTORY_DB) and make sure the schema
    exists. There's no default: lo9i points FACTORY_DB at the server's data
    folder, outside its code, which updates replace. A connection may move
    between threads (the web page's handlers run in a thread pool), but is
    used by one at a time."""
    path = path or os.environ.get("FACTORY_DB")
    if not path:
        raise MissingDatabaseError("FACTORY_DB must point at the database file")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    # Off by default in SQLite, per connection: deleting a task needs it.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def path() -> Path:
    """The database file, from FACTORY_DB."""
    found = os.environ.get("FACTORY_DB")
    if not found:
        raise MissingDatabaseError("FACTORY_DB must point at the database file")
    return Path(found).resolve()


def data_dir() -> Path:
    """The plugin's data folder ({data}), where its database is: worktrees,
    agent homes and logs go next to it."""
    return path().parent
