"""Tasks and their checkpoints, as the MCP tools and the web page see them.

Every rule lives here so both behave the same. It records tasks and moves
them through their checkpoints; the runner (runner.py) works on them, and is
what opens a checkpoint when a stage's debate is decided.
"""

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from . import cleanup, git, repo, settings
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    AFTER,
    CHECKPOINTS,
    FINISHED,
    REDO,
    RUN_MODES,
    TASK_STATUSES,
    Approval,
    Checkpoint,
    Decision,
    Message,
    Stage,
    Task,
    TestRun,
)

# Longest title made from the description, and branch slug.
TITLE_LENGTH = 80
SLUG_LENGTH = 40


def slugify(text: str) -> str:
    """For the branch name: ASCII letters and digits joined by dashes."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:SLUG_LENGTH].rstrip("-") or "task"


def _git_repo(path: str) -> str:
    """The repository's path. A new project's folder (one that doesn't exist
    yet, or an empty one) is made a repository; a folder with files that
    isn't one is refused."""
    full = settings.repo_path(path)
    try:
        git.ensure_repo(Path(full))
    except git.GitError as e:
        raise ValidationError(str(e)) from None
    return full


def _task(conn: sqlite3.Connection, id: int) -> Task:
    t = repo.get_task(conn, id)
    if t is None:
        raise NotFoundError(f"no task {id}")
    return t


# --- Tasks ---


def create_task(
    conn: sqlite3.Connection,
    repo_path: str,
    description: str,
    *,
    title: str | None = None,
    include_context: bool = False,
    run_mode: str | None = None,
) -> Task:
    """A task in a local repository, queued at its spec stage."""
    description = description.strip()
    if not description:
        raise ValidationError("describe the task")
    path = _git_repo(repo_path)
    if run_mode is not None and run_mode not in RUN_MODES:
        raise ValidationError(f"run_mode must be one of {', '.join(RUN_MODES)}, or absent to follow Settings")
    title = (title or "").strip() or next(line for line in description.splitlines() if line.strip()).strip()
    title = title[:TITLE_LENGTH].rstrip()
    with conn:
        id = repo.insert_task(
            conn,
            title=title,
            description=description,
            repo=path,
            slug=slugify(title),
            include_context=include_context,
            run_mode=run_mode,
        )
        repo.insert_stage(conn, id, "spec")
    return _task(conn, id)


def driven_from(conn: sqlite3.Connection, id: int, origin: dict | None) -> None:
    """The conversation in lo9i the user last drove the task from: its
    notices go there (notify.py). None (the page, say) leaves it as it was."""
    if origin:
        with conn:
            repo.update_task(conn, _task(conn, id).id, {"origin": json.dumps(origin)})


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[Task]:
    """Newest first."""
    if status is not None and status not in TASK_STATUSES:
        raise ValidationError(f"status must be one of {', '.join(TASK_STATUSES)}")
    return repo.list_tasks(conn, status)


def get_task(conn: sqlite3.Connection, id: int) -> Task:
    return _task(conn, id)


def stages(conn: sqlite3.Connection, id: int) -> list[Stage]:
    """Every run of every stage of a task, in order."""
    return repo.list_stages(conn, _task(conn, id).id)


def pending_checkpoint(conn: sqlite3.Connection, id: int) -> Checkpoint | None:
    return repo.pending_checkpoint(conn, _task(conn, id).id)


def costs(conn: sqlite3.Connection, id: int) -> dict:
    """What a task spent (USD for the direct APIs, requests for Copilot), in
    all and per stage and per role."""
    t = _task(conn, id)
    return {
        **repo.cost_totals(conn, t.id),
        "by_stage": repo.cost_breakdown(conn, t.id, "stage"),
        "by_role": repo.cost_breakdown(conn, t.id, "role"),
    }


def pause(conn: sqlite3.Connection, id: int, reason: str) -> Task:
    """Stop a task until the user resumes it: a limit was reached. The stage
    keeps its place, so it goes on from there."""
    t = _task(conn, id)
    if t.status in FINISHED:
        raise ConflictError(f"task {id} is {t.status}")
    with conn:
        repo.update_task(conn, id, {"status": "paused", "reason": reason, "paused_at": repo.now_precise()})
    return _task(conn, id)


def fail(conn: sqlite3.Connection, id: int, reason: str) -> Task:
    """Stop a task for good: something went wrong that resuming wouldn't fix."""
    with conn:
        repo.cancel_stages(conn, id, "failed")
        repo.update_task(conn, id, {"status": "failed", "reason": reason, "finished_at": repo.now()})
    return _task(conn, id)


def resume(conn: sqlite3.Connection, id: int) -> Task:
    """A paused task goes back in the queue, once it's within its limit again."""
    t = _task(conn, id)
    if t.status != "paused":
        raise ConflictError(f"task {id} isn't paused; it's {t.status}")
    limit = settings.load(conn).max_usd
    if (spent := repo.spent_usd(conn, id)) >= limit:
        raise ConflictError(f"task {id} has spent ${spent:.2f} of max_usd ${limit:.2f}: raise max_usd first")
    with conn:
        repo.update_task(conn, id, {"status": "queued", "reason": ""})
    return _task(conn, id)


def comment(conn: sqlite3.Connection, id: int, text: str) -> Message:
    """The user steps into the task's current stage. Once its debate has
    started, every member answers the comment before the debate goes on;
    before, it's part of what the debate starts from."""
    text = text.strip()
    if not text:
        raise ValidationError("the comment is empty")
    t = _task(conn, id)
    if t.status in FINISHED:
        raise ConflictError(f"task {id} is {t.status}")
    if t.status == "waiting":
        raise ConflictError(f"task {id} is at its checkpoint: approve it, or reject it with the comment")
    stage = repo.current_stage(conn, id)
    with conn:
        message_id = repo.insert_comment(conn, id, stage.id, text, round=stage.round)
    return next(m for m in repo.stage_messages(conn, stage.id) if m.id == message_id)


def cancel(conn: sqlite3.Connection, id: int) -> Task:
    """Stop a task. Its history stays until it's deleted."""
    t = _task(conn, id)
    if t.status in FINISHED:
        raise ConflictError(f"task {id} is already {t.status}")
    with conn:
        if (c := repo.pending_checkpoint(conn, id)) is not None:
            repo.decide_checkpoint(conn, c.id, "cancelled")
        repo.cancel_stages(conn, id)
        repo.update_task(conn, id, {"status": "cancelled", "finished_at": repo.now()})
    return _task(conn, id)


def delete_task(conn: sqlite3.Connection, id: int) -> Task:
    """A task and its whole debate, its worktree and logs. Its branch, if it
    has one, stays in the repo. One that's running stops at its next step."""
    t = _task(conn, id)
    with conn:
        repo.delete_task(conn, id)
    cleanup.forget(t)
    return t


# --- Checkpoints ---


def open_checkpoint(conn: sqlite3.Connection, id: int, decision_id: int | None = None) -> Checkpoint:
    """The task's current stage waits for the user: the runner calls this
    when the judge has decided a spec, a plan or a final diff."""
    t = _task(conn, id)
    stage = repo.current_stage(conn, id)
    if t.status in FINISHED or t.status == "waiting" or stage is None:
        raise ConflictError(f"task {id} is {t.status}, so it can't wait for a checkpoint")
    if stage.kind not in CHECKPOINTS:
        raise ConflictError(f"the {stage.kind} stage has no checkpoint; {', '.join(CHECKPOINTS)} do")
    with conn:
        checkpoint_id = repo.insert_checkpoint(conn, id, stage.id, CHECKPOINTS[stage.kind], decision_id)
        repo.update_stage(conn, stage.id, {"status": "waiting"})
        repo.update_task(conn, id, {"status": "waiting"})
    return repo.get_checkpoint(conn, checkpoint_id)


def result(conn: sqlite3.Connection, stage: Stage) -> str | None:
    """What the judge decided in a run of a spec or plan stage, as its
    checkpoint showed it."""
    checkpoint = repo.stage_checkpoint(conn, stage.id)
    found = decision(conn, checkpoint) if checkpoint else None
    return found.body.get("result") if found else None


def approved(conn: sqlite3.Connection, task_id: int, kind: str) -> str | None:
    """The approved spec or plan."""
    runs = [s for s in repo.list_stages(conn, task_id) if s.kind == kind and s.status == "approved"]
    return result(conn, runs[-1]) if runs else None


def rejected(conn: sqlite3.Connection, task_id: int, stage: Stage) -> list[str]:
    """The results the user rejected before this run of a stage, oldest first."""
    runs = [s for s in repo.list_stages(conn, task_id) if s.kind == stage.kind and s.status == "rejected"]
    return [r for s in runs if s.attempt < stage.attempt and (r := result(conn, s))]


def decision(conn: sqlite3.Connection, checkpoint: Checkpoint) -> Decision | None:
    """The judge's decision a checkpoint shows the user."""
    return repo.get_decision(conn, checkpoint.decision_id) if checkpoint.decision_id else None


def _waiting(conn: sqlite3.Connection, id: int) -> tuple[Checkpoint, Stage]:
    t = _task(conn, id)
    c = repo.pending_checkpoint(conn, id)
    if c is None or t.status != "waiting":
        raise ConflictError(f"task {id} has no checkpoint waiting for you; it's {t.status}, at {t.stage}")
    return c, repo.get_stage(conn, c.stage_id)


def approve(conn: sqlite3.Connection, id: int, comment: str = "") -> Task:
    """Approve the checkpoint the task waits at: it goes on to the next
    stage, or is done after the final diff."""
    c, stage = _waiting(conn, id)
    following = AFTER[stage.kind]
    with conn:
        repo.decide_checkpoint(conn, c.id, "approved", comment.strip())
        repo.update_stage(conn, stage.id, {"status": "approved", "finished_at": repo.now()})
        if following is None:
            repo.update_task(conn, id, {"stage": "done", "status": "done", "finished_at": repo.now()})
        else:
            repo.insert_stage(conn, id, following)
            repo.update_task(conn, id, {"stage": following, "status": "queued"})
    if following is None:
        # Done: the worktree goes now, the branch stays (SPEC §10).
        cleanup.remove_worktree(conn, _task(conn, id))
    return _task(conn, id)


def reject(conn: sqlite3.Connection, id: int, comment: str) -> Task:
    """Reject the checkpoint the task waits at: the stage runs again, with
    the comment in its thread. A rejected diff goes back to the build."""
    comment = comment.strip()
    if not comment:
        raise ValidationError("say what to change: the comment goes to the team")
    c, stage = _waiting(conn, id)
    again = REDO[stage.kind]
    with conn:
        repo.decide_checkpoint(conn, c.id, "rejected", comment)
        repo.update_stage(conn, stage.id, {"status": "rejected", "finished_at": repo.now()})
        stage_id = repo.insert_stage(conn, id, again)
        repo.insert_comment(conn, id, stage_id, comment)
        repo.update_task(conn, id, {"stage": again, "status": "queued"})
    return _task(conn, id)


def changes(conn: sqlite3.Connection, id: int) -> list[git.Changed]:
    """What the task's branch changed: in its worktree while it has one,
    then in the repository, where the branch stays."""
    t = _task(conn, id)
    if not t.base_commit:
        return []
    try:
        return git.changed(t.worktree, t.base_commit) if t.worktree else git.changed(t.repo, t.base_commit, t.branch)
    except git.GitError:
        return []


def diff(conn: sqlite3.Connection, id: int, limit: int = 300_000) -> str:
    """The task's diff, from its worktree or, once that's gone, its branch."""
    t = _task(conn, id)
    if not t.base_commit:
        return ""
    try:
        if t.worktree:
            return git.diff(t.worktree, t.base_commit, limit)
        return git.diff(t.repo, t.base_commit, limit, t.branch)
    except git.GitError:
        return ""


def test_runs(conn: sqlite3.Connection, id: int) -> list[TestRun]:
    return repo.task_test_runs(conn, _task(conn, id).id)


def review_points(conn: sqlite3.Connection, id: int) -> list[dict]:
    """The review's points and the agent's answers to them."""
    return repo.review_points(conn, _task(conn, id).id)


# --- Approvals ---


def approvals(conn: sqlite3.Connection, id: int) -> list[Approval]:
    """What the task's agent waits for the user to allow (local mode)."""
    return repo.pending_approvals(conn, _task(conn, id).id)


def answer_approval(conn: sqlite3.Connection, approval_id: int, allow: bool, reason: str = "") -> Approval:
    """Allow or deny what the agent asked to do; it goes on at once."""
    approval = repo.get_approval(conn, approval_id)
    if approval is None:
        raise NotFoundError(f"no approval {approval_id}")
    if approval.status != "pending":
        raise ConflictError(f"approval {approval_id} is already {approval.status}")
    with conn:
        repo.decide_approval(conn, approval_id, "allowed" if allow else "denied", reason.strip())
    return repo.get_approval(conn, approval_id)
