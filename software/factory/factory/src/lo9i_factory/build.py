"""The build and review stages (SPEC §2 BUILD, §6, §7, §9).

BUILD, in steps the stage keeps (so a restart or a resume goes on from the
last one done):

1. agent: the coding agent implements the approved plan in the task's
   worktree, or the review's points, or what the user said rejecting the
   diff; it reports what it did and answers each point. The factory commits
   what it changed.
2. tests: the tester names the command that runs only the change's own
   tests, and the factory runs it (in local mode once the user allows it).
3. full: the repository's test command (Settings, per repo), when its last
   full run took less than full_test_minutes.

REVIEW: the reviewer, security and tester review the diff in a debate; the
judge accepts it or sends back points to fix. Points go back to the agent,
a new build, up to max_fix_loops times; then the full tests run (always,
before the final checkpoint) and the task waits at CHECKPOINT 3 with the
diff, the tests and the judge's word, open points included.
"""

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import agents, approvals, db, git, meter, prices, repo, service, settings
from .agents import Agent, Job
from .engine import Brief, StoppedError, debate, start
from .errors import FactoryError, ValidationError
from .models import Stage, Task
from .providers import KEYS, ProviderError, Providers, Request, Turn
from .sandbox import Sandbox, check_docker, ensure_image

TEST_TIMEOUT_SECONDS = 3600
DIFF_CHARS = 60_000
# Of each test run's output, in the review.
OUTPUT_IN_REVIEW = 4_000
REVIEWERS = ("reviewer", "security", "tester")
REVIEW_GOAL = (
    "a review of the diff that builds the approved plan: whether it does what the spec and plan ask, correctly,"
    " safely and with tests, and if not, the points the implementer must fix"
)
SHORT_TESTS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command, run from the repository's root; empty when the change has no tests",
        },
        "why": {"type": "string", "description": "What it runs"},
    },
    "required": ["command", "why"],
    "additionalProperties": False,
}

# Makes the agent Settings names, with its key; tests hand in their own.
MakeAgent = Callable[[str, str], Agent]


class AgentError(FactoryError):
    """The coding agent failed or ran out of time."""


def log_path(task_id: int, stage: Stage) -> Path:
    return db.data_dir() / "logs" / f"task-{task_id}" / f"{stage.kind}-{stage.attempt}.jsonl"


def read_log(task_id: int, stage: Stage, last: int = 300) -> list[dict[str, Any]]:
    """The stage's last events, oldest first; none before it has a log."""
    path = log_path(task_id, stage)
    if not path.is_file():
        return []
    lines = path.read_text(errors="replace").splitlines()[-last:]
    return [json.loads(line) for line in lines if line.strip()]


class Log:
    """The stage's log, a JSON line per event, for the task's page to show
    (data folder/logs/task-<id>/<stage>-<attempt>.jsonl)."""

    def __init__(self, task_id: int, stage: Stage) -> None:
        self.path = log_path(task_id, stage)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: dict[str, Any]) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), **event}) + "\n")

    def output(self, text: str) -> None:
        if text.strip():
            self({"type": "output", "text": text.rstrip()})


# --- Build ---


async def build(
    conn: sqlite3.Connection, providers: Providers, task: Task, stage: Stage, make_agent: MakeAgent = agents.make
) -> None:
    """Run the build stage to its end, from the step it got to."""
    start(conn, stage)
    s = settings.load(conn)
    log = Log(task.id, stage)
    task = _worktree(conn, task, s)
    sandbox = await _sandbox(task, stage, s, log)
    if stage.step is None:
        await _implement(conn, task, stage, s, sandbox, log, make_agent)
        stage = _step(conn, stage, "agent")
    if stage.step == "agent":
        await _short_tests(conn, providers, task, stage, s, sandbox, log)
        stage = _step(conn, stage, "tests")
    if stage.step == "tests":
        await _full_tests(conn, task, stage, s, sandbox, log, always=False)
        stage = _step(conn, stage, "full")
    with conn:
        repo.update_stage(conn, stage.id, {"status": "done", "finished_at": repo.now()})
        repo.insert_stage(conn, task.id, "review")
        repo.update_task(conn, task.id, {"stage": "review"})


def _step(conn: sqlite3.Connection, stage: Stage, step: str) -> Stage:
    with conn:
        repo.update_stage(conn, stage.id, {"step": step})
    return repo.get_stage(conn, stage.id)


def _worktree(conn: sqlite3.Connection, task: Task, s: settings.Settings) -> Task:
    """The task's worktree, made the first time (and again if it's gone)."""
    if task.worktree and Path(task.worktree).is_dir():
        return task
    path = db.data_dir() / "worktrees" / f"task-{task.id}"
    base_commit = git.add_worktree(task.repo, path, task.branch, s.repo(task.repo).base_branch or "HEAD")
    with conn:
        repo.update_task(conn, task.id, {"worktree": str(path), "base_commit": task.base_commit or base_commit})
    return service.get_task(conn, task.id)


async def _sandbox(task: Task, stage: Stage, s: settings.Settings, log: Log) -> Sandbox:
    mode = task.run_mode or s.run_mode
    if mode == "docker":
        await check_docker()
        await ensure_image(log.output)
    return Sandbox.of(mode, task.worktree, task.id, stage.id)


def _stopped(conn: sqlite3.Connection, task_id: int) -> bool:
    """The user cancelled or paused the task (or deleted it)."""
    task = repo.get_task(conn, task_id)
    return task is None or task.status != "running"


async def _implement(
    conn: sqlite3.Connection,
    task: Task,
    stage: Stage,
    s: settings.Settings,
    sandbox: Sandbox,
    log: Log,
    make_agent: MakeAgent,
) -> None:
    name = s.agent
    provider, _, model = s.models["implementer"].partition(":")
    if provider != agents.PROVIDERS[name]:
        wanted = agents.PROVIDERS[name]
        raise ValidationError(f"{name} runs {wanted}'s models: set models.implementer to {wanted}:<model>")
    key = os.environ.get(KEYS[provider], "")
    if not key:
        raise ProviderError(
            f"{KEYS[provider]} isn't set, and {name} needs it: enter it in the factory plugin's details"
        )
    # Claude Code says what it spent; Codex's tokens are priced here.
    price = None if name == "claude-code" else prices.price(s.models["implementer"], s)
    budget = meter.left(conn, task.id, s)
    job = Job(
        task_id=task.id,
        stage_id=stage.id,
        prompt=_prompt(conn, task, stage),
        system=_system(s, sandbox),
        model=model,
        budget_usd=budget if price is None else None,
        sandbox=sandbox,
    )
    log({"type": "start", "agent": name, "model": model, "mode": sandbox.mode})
    result = await make_agent(name, key).run(job, log, lambda: _stopped(conn, task.id))
    usd = result.usd
    if usd is None:
        # Priced from its tokens: Codex's always; Claude Code's when it didn't say.
        known = price or prices.find(s.models["implementer"], s)
        tokens = (result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens)
        usd = prices.usd(known, *tokens) if known else 0.0
    with conn:
        cost_id = repo.insert_cost(
            conn,
            task_id=task.id,
            stage_id=stage.id,
            role="implementer",
            provider=provider,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            usd=usd,
        )
        message_id = repo.insert_message(
            conn,
            task_id=task.id,
            stage_id=stage.id,
            round=0,
            role="implementer",
            model=f"{provider}:{model}",
            type="report",
            body={"ok": result.ok, "summary": result.summary, "points": result.points, "error": result.error},
        )
        repo.link_cost(conn, cost_id, message_id)
        _answer_points(conn, task.id, result.points)
    # What it did so far is kept, even when it failed: a resume builds on it.
    git.commit(task.worktree, f"{task.title} (build {stage.attempt})")
    if result.stopped and _stopped(conn, task.id):
        raise StoppedError(f"task {task.id} stopped while the agent worked")
    if not result.ok:
        raise AgentError(f"the coding agent failed: {result.error or 'no reason given'}")


def _answer_points(conn: sqlite3.Connection, task_id: int, answers: list[dict]) -> None:
    """The agent's word on each open review point it was given."""
    open_ids = {p["id"] for p in repo.review_points(conn, task_id, "open")}
    for a in answers:
        if a.get("id") in open_ids and a.get("status") in ("resolved", "declined"):
            reason = str(a.get("reason") or "").strip() or "no reason given"
            repo.answer_review_point(conn, a["id"], a["status"], reason)


def _prompt(conn: sqlite3.Connection, task: Task, stage: Stage) -> str:
    parts = [f"# The task\n\n## {task.title}\n\n{task.description}"]
    if spec := service.approved(conn, task.id, "spec"):
        parts.append(f"# The approved spec\n\n{spec}")
    if plan := service.approved(conn, task.id, "plan"):
        parts.append(f"# The approved plan\n\n{plan}")
    rejected = [m.body["text"] for m in repo.stage_messages(conn, stage.id) if m.type == "comment" and m.round == 0]
    if rejected:
        parts.append(
            "# The user rejected the last diff\n\nThey said:\n\n" + "\n\n".join(rejected) + "\n\nChange it accordingly."
        )
    points = repo.review_points(conn, task.id, "open")
    if points:
        listed = "\n".join(f"- [{p['id']}] {p['text']}" for p in points)
        parts.append(
            "# Review points to address\n\nThe review sent these back. Resolve each one, or decline it with a"
            f" reason, and answer every one in your report by its id:\n\n{listed}"
        )
    elif stage.attempt == 1:
        parts.append("# Your job\n\nImplement the approved plan, with the tests it names.")
    return "\n\n".join(parts)


def _system(s: settings.Settings, sandbox: Sandbox) -> str:
    where = (
        "You're in a container with only this folder: install what you need. git doesn't work here (its metadata"
        " is outside), and you don't need it."
        if sandbox.mode == "docker"
        else "Commands and network access wait for the user to allow them, which can take a while; one denied"
        " won't run: find another way, or say in your report what you needed."
    )
    return (
        f"{s.prompts['implementer']}\n\nYou work in the task's own copy of the repository ({sandbox.folder}), on its"
        " own branch. Don't commit, push or switch branches: the factory commits what you change. Read the"
        f" repository's CLAUDE.md or AGENTS.md first, if it has one. {where} Run the tests you write. When you're"
        " done, report what you did and answer each review point you were given."
    )


async def _short_tests(
    conn: sqlite3.Connection,
    providers: Providers,
    task: Task,
    stage: Stage,
    s: settings.Settings,
    sandbox: Sandbox,
    log: Log,
) -> None:
    """The change's own tests (SPEC §6): the tester names the command."""
    diff = git.diff(task.worktree, task.base_commit, DIFF_CHARS)
    if not diff.strip():
        log.output("Nothing changed, so there are no tests of the change to run.")
        return
    full = s.repo(task.repo).test_command or "not set"
    request = Request(
        system=s.prompts["tester"],
        turns=[
            Turn("user", f"# The change\n\n```diff\n{diff}\n```"),
            Turn(
                "user",
                "Give the shell command that runs only the tests of this change (those it added or touched),"
                f" from the repository's root, or an empty command if it has none. The repository's full test"
                f" command, for reference: {full}",
            ),
        ],
        schema=SHORT_TESTS,
    )
    answer = await meter.call(conn, providers, task.id, stage.id, "tester", request)
    command = answer.data["command"].strip()
    if not command:
        log.output(f"The tester found no tests of the change to run: {answer.data['why']}")
        return
    if sandbox.mode == "local":
        allowed, reason = await approvals.ask(task.id, stage.id, "test command", command)
        if not allowed:
            log.output(f"The user didn't allow the test command {command}: {reason}")
            return
    await _run_tests(conn, task, stage, sandbox, log, "short", command)


async def _full_tests(
    conn: sqlite3.Connection,
    task: Task,
    stage: Stage,
    s: settings.Settings,
    sandbox: Sandbox,
    log: Log,
    *,
    always: bool,
) -> None:
    """The repository's test command: always before the final checkpoint;
    after a build only when its last run was quick (SPEC §9)."""
    command = s.repo(task.repo).test_command
    if not command:
        log.output("No test command is set for this repository (repos.<path> in Settings).")
        return
    sha = git.head(task.worktree)
    if any(r.kind == "full" and r.commit_sha == sha for r in repo.task_test_runs(conn, task.id)):
        return
    if not always:
        last = repo.last_full_run(conn, task.repo)
        if last is None or last.seconds > s.full_test_minutes * 60:
            log.output("The full tests wait for the final checkpoint: their last run was long, or there was none.")
            return
    await _run_tests(conn, task, stage, sandbox, log, "full", command)


async def _run_tests(
    conn: sqlite3.Connection, task: Task, stage: Stage, sandbox: Sandbox, log: Log, kind: str, command: str
) -> None:
    log({"type": "tests", "kind": kind, "command": command})
    done = await sandbox.run(
        ["sh", "-lc", command], should_stop=lambda: _stopped(conn, task.id), timeout=TEST_TIMEOUT_SECONDS
    )
    if done.stopped and _stopped(conn, task.id):
        raise StoppedError(f"task {task.id} stopped while its tests ran")
    log.output(done.output)
    with conn:
        repo.insert_test_run(
            conn,
            task_id=task.id,
            stage_id=stage.id,
            kind=kind,
            command=command,
            commit_sha=git.head(task.worktree),
            exit_code=done.exit_code,
            seconds=round(done.seconds, 2),
            output=done.output,
        )


# --- Review ---


async def review(conn: sqlite3.Connection, providers: Providers, task: Task, stage: Stage) -> bool:
    """Run the review stage; True when the task now waits at the final
    checkpoint, False when its points went back to a new build."""
    start(conn, stage)
    s = settings.load(conn)
    decision = await debate(conn, providers, stage.id, _review_brief(conn, task))
    loops = _fix_loops(conn, task.id)
    with conn:
        if decision.kind == "fixes" and not any(p["stage_id"] == stage.id for p in repo.review_points(conn, task.id)):
            for point in decision.body["points"]:
                repo.insert_review_point(conn, task.id, stage.id, "judge", point)
        if decision.kind == "fixes" and loops < s.max_fix_loops:
            repo.update_stage(conn, stage.id, {"status": "done", "finished_at": repo.now()})
            repo.insert_stage(conn, task.id, "build")
            repo.update_task(conn, task.id, {"stage": "build"})
            return False
    log = Log(task.id, stage)
    await _full_tests(conn, task, stage, s, await _sandbox(task, stage, s, log), log, always=True)
    service.open_checkpoint(conn, task.id, decision.id)
    return True


def _fix_loops(conn: sqlite3.Connection, task_id: int) -> int:
    """The builds since the user last decided a checkpoint, but the first."""
    stages = repo.list_stages(conn, task_id)
    decided = [s.id for s in stages if (c := repo.stage_checkpoint(conn, s.id)) is not None and c.status != "pending"]
    since = max(decided, default=0)
    return max(0, len([s for s in stages if s.kind == "build" and s.id > since]) - 1)


def _review_brief(conn: sqlite3.Connection, task: Task) -> Brief:
    builds = [s for s in repo.list_stages(conn, task.id) if s.kind == "build"]
    parts = [f"## {task.title}\n\n{task.description}"]
    if spec := service.approved(conn, task.id, "spec"):
        parts.append(f"## The approved spec\n\n{spec}")
    if plan := service.approved(conn, task.id, "plan"):
        parts.append(f"## The approved plan\n\n{plan}")
    changed = git.changed(task.worktree, task.base_commit)
    summary = "\n".join(f"- {c.path} (+{c.added if c.added is not None else '?'} -{c.deleted or 0})" for c in changed)
    parts.append(f"## What changed\n\n{summary or 'Nothing.'}")
    parts.append(f"## The diff\n\n```diff\n{git.diff(task.worktree, task.base_commit, DIFF_CHARS)}\n```")
    reports = [m for m in repo.stage_messages(conn, builds[-1].id) if m.type == "report"] if builds else []
    if reports:
        parts.append(f"## The implementer's report\n\n{reports[-1].body['summary']}")
    runs = [r for r in repo.task_test_runs(conn, task.id) if builds and r.stage_id == builds[-1].id]
    for r in runs:
        out = r.output[-OUTPUT_IN_REVIEW:]
        verdict = "passed" if r.passed else f"failed (exit {r.exit_code})"
        parts.append(f"## Tests ({r.kind}): `{r.command}` {verdict} in {r.seconds:.0f} s\n\n```\n{out}\n```")
    if not runs:
        parts.append("## Tests\n\nNo test run for this build.")
    points = repo.review_points(conn, task.id)
    if points:
        listed = "\n".join(
            f"- [{p['id']}] {p['text']} — {p['status']}" + (f": {p['reason']}" if p["reason"] else "") for p in points
        )
        parts.append(f"## Review points so far, and the implementer's answers\n\n{listed}")
    return Brief(goal=REVIEW_GOAL, context="\n\n".join(parts), roles=REVIEWERS, review=True)
