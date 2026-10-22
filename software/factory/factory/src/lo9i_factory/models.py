"""What the factory works with: roles, stages, statuses, and the rows the
service hands out. The database's CHECKs (db.py) use the same values."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# The team (SPEC §1), and the judge that picks the winner of each debate.
ROLES = ("architect", "implementer", "reviewer", "tester", "security", "judge")
PROVIDERS = ("anthropic", "openai", "copilot")
# The coding agent that implements the plan in the worktree.
AGENTS = ("claude-code", "codex")
# Docker: the agent runs in a container with only the worktree. Local: every
# command and network access asks the user first (SPEC §11).
RUN_MODES = ("docker", "local")

# A task's stages in order. review is BUILD's critique of the diff (SPEC §2),
# a stage of its own so the board can show it; it ends in the final checkpoint.
STAGES = ("spec", "plan", "build", "review")
# What a stage's checkpoint is called: the spec, the plan, the final diff.
CHECKPOINTS = {"spec": "spec", "plan": "plan", "review": "diff"}
# The stage after an approved checkpoint; None when the task is done.
AFTER = {"spec": "plan", "plan": "build", "review": None}
# The stage a rejected checkpoint runs again: the diff goes back to the agent.
REDO = {"spec": "spec", "plan": "plan", "review": "build"}

# queued: waiting for the runner (a new task, or a stage approved or rejected).
# waiting: at a checkpoint, for the user. paused: a limit was reached.
TASK_STATUSES = ("queued", "running", "waiting", "paused", "done", "failed", "cancelled")
FINISHED = ("done", "failed", "cancelled")
STAGE_STATUSES = ("pending", "running", "waiting", "approved", "rejected", "done", "failed", "cancelled")
CHECKPOINT_STATUSES = ("pending", "approved", "rejected", "cancelled")

# The board (SPEC §8): one column per stage, plus what waits for the user.
COLUMNS = (
    ("waiting", "Waiting for you"),
    ("spec", "Spec"),
    ("plan", "Plan"),
    ("build", "Build"),
    ("review", "Review"),
    ("done", "Done"),
)


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    description: str
    # The local repository it works in, an absolute path.
    repo: str
    slug: str
    # factory/<id>-<slug>: the branch it leaves in the repo.
    branch: str
    # Its git worktree while it runs; None before and after.
    worktree: str | None
    # The commit its branch started from.
    base_commit: str | None
    # The conversation in lo9i it was last driven from, where its notices go.
    origin: dict | None
    # The repo's README, tree and CLAUDE.md/AGENTS.md go to the team.
    include_context: bool
    # docker or local for this task; None follows Settings.
    run_mode: str | None
    # The stage it's in (STAGES), or "done".
    stage: str
    status: str
    # Why it's paused or failed.
    reason: str
    # When it last paused.
    paused_at: str | None
    # The runner working on it, and when it last said so.
    runner: str | None
    heartbeat: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    @property
    def column(self) -> str:
        """Its column on the board."""
        if self.status in ("waiting", "paused"):
            return "waiting"
        return "done" if self.status in FINISHED else self.stage

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        values = dict(row)
        origin = json.loads(values["origin"]) if values["origin"] else None
        return cls(**{**values, "include_context": bool(values["include_context"]), "origin": origin})


@dataclass(frozen=True)
class Stage:
    id: int
    task_id: int
    kind: str
    # 1, then one more each time it runs again (a rejected checkpoint, a fix loop).
    attempt: int
    status: str
    # Where the runner is, saved before it goes on, so a restart resumes here:
    # the debate round, and the last step finished in it.
    round: int
    step: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Stage":
        return cls(**dict(row))


@dataclass(frozen=True)
class Checkpoint:
    id: int
    task_id: int
    stage_id: int
    # spec, plan or diff.
    kind: str
    # The judge's decision the user approves or rejects, once there is one.
    decision_id: int | None
    status: str
    # The user's, with the approval or the rejection.
    comment: str
    created_at: str
    decided_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Checkpoint":
        return cls(**dict(row))


@dataclass(frozen=True)
class Message:
    """One turn of a debate: a role's proposal, critique, revision, vote,
    question or answer, or the user's comment."""

    id: int
    task_id: int
    stage_id: int
    round: int
    # A role of the team, or "user".
    role: str
    # provider:model it ran on; None for the user.
    model: str | None
    type: str
    # A question's addressee.
    to_role: str | None
    # The question an answer answers.
    reply_to: int | None
    # The message itself, in the shape of its type (engine/schemas.py).
    body: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        values = dict(row)
        return cls(**{**values, "body": json.loads(values["body"])})


@dataclass(frozen=True)
class Decision:
    """The judge's verdict after a round: continue, or the winner."""

    id: int
    task_id: int
    stage_id: int
    round: int
    model: str
    kind: str
    winner_message_id: int | None
    rationale: str
    # For a winner: the winning role (or "merged"), the result, the losing options.
    body: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Decision":
        values = dict(row)
        return cls(**{**values, "body": json.loads(values["body"])})


@dataclass(frozen=True)
class TestRun:
    # Not a test class, for pytest.
    __test__ = False

    id: int
    task_id: int
    stage_id: int
    # short: the change's own tests, picked by the tester. full: the repo's test command.
    kind: str
    command: str
    commit_sha: str
    exit_code: int
    seconds: float
    output: str
    created_at: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TestRun":
        return cls(**dict(row))


@dataclass(frozen=True)
class Approval:
    """Something the agent asks to do in local mode, for the user to allow."""

    id: int
    task_id: int
    stage_id: int
    tool: str
    detail: str
    # pending, allowed or denied.
    status: str
    reason: str
    created_at: str
    decided_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Approval":
        return cls(**dict(row))
