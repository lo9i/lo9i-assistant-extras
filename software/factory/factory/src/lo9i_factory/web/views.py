"""What the page shows, put together from the service: the board's cards; a
task's progress across its stages and, in a sentence, what it's doing now;
each stage's outcome in short (the judge's decision, or where the debate
stands), and its whole debate as a chat (SPEC §8): one bubble per message,
with the questions and votes it raised and the answers they got nested
under it, the judge's word after each round, the decision it ended in.

What's going on is read from what the task has saved (its stage's round and
step, its messages, the agent's log, what waits for the user), so it costs
no model call."""

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import build, repo, service
from ..engine import next_step
from ..models import COLUMNS, Approval, Checkpoint, Decision, Message, Stage, Task
from ..stages import ROLES as DEBATERS

CHECKPOINT_LABELS = {"spec": "the spec", "plan": "the plan", "diff": "the final diff"}
# The steps across the top of a task's page: its stages, then the final diff.
STEPS = (("spec", "Spec"), ("plan", "Plan"), ("build", "Build"), ("review", "Review & tests"), ("diff", "Final diff"))
# Who debates in each stage (stages.py, build.py); the judge decides.
MEMBERS = {**DEBATERS, "review": build.REVIEWERS}
AGENT_NAMES = {"claude-code": "Claude Code", "codex": "Codex"}
# A role, as a sentence names it.
NAMES = {"security": "security agent", "user": "you"}


@dataclass
class Card:
    task: Task
    usd: float
    requests: int
    # Steps its agent waits for the user to allow.
    approvals: int


def board(conn: sqlite3.Connection) -> dict[str, list[Card]]:
    """The cards in each column, newest first."""
    columns: dict[str, list[Card]] = {key: [] for key, _ in COLUMNS}
    for t in repo.list_tasks(conn):
        spent = repo.cost_totals(conn, t.id)
        card = Card(t, spent["usd"], spent["requests"], len(repo.pending_approvals(conn, t.id)))
        columns["waiting" if card.approvals else t.column].append(card)
    return columns


@dataclass
class Bubble:
    message: Message
    # What answers it, asks about it, or votes from it.
    replies: list["Bubble"] = field(default_factory=list)


@dataclass
class Round:
    number: int
    bubbles: list[Bubble]
    # The judge's "not yet" after it, with why.
    verdict: Decision | None = None


@dataclass
class Point:
    """A line of where a debate stands: what the judge says is still open,
    a critique, a question, the user's comment."""

    # verdict, comment, question or critique.
    kind: str
    # The role that raised it, or "user".
    who: str
    text: str
    # open, or how it was dealt with: answered, or revised (its target wrote a revision since).
    status: str
    # The role it's about or asks.
    about: str | None = None


@dataclass
class StageView:
    stage: Stage
    rounds: list[Round]
    # How the stage ended: the winner, accept or fixes.
    decision: Decision | None
    checkpoint: Checkpoint | None
    # The agent's log, for a build; the full tests', for a review.
    log: list[dict[str, Any]]
    messages: list[Message] = field(default_factory=list)
    # The judge's "not yet"s, in order.
    verdicts: list[Decision] = field(default_factory=list)

    @property
    def title(self) -> str:
        again = f" (run {self.stage.attempt})" if self.stage.attempt > 1 else ""
        return {"spec": "Spec", "plan": "Plan", "build": "Build", "review": "Review"}[self.stage.kind] + again

    @property
    def members(self) -> tuple[str, ...]:
        return MEMBERS.get(self.stage.kind, ())

    @property
    def proposals(self) -> list[Message]:
        """Each member's latest proposal (or review), in the team's order."""
        latest = {m.role: m for m in self.messages if m.type in ("proposal", "revision")}
        return [latest[r] for r in self.members if r in latest]

    @property
    def points(self) -> list[Point]:
        return points(self.messages, self.verdicts, self.members)

    @property
    def report(self) -> Message | None:
        """The coding agent's last report, for a build."""
        reports = [m for m in self.messages if m.type == "report"]
        return reports[-1] if reports else None

    @property
    def clock(self) -> str:
        """How long it took, or has taken so far."""
        if not self.stage.started_at:
            return ""
        if self.stage.finished_at:
            return f"took {elapsed(self.stage.started_at, self.stage.finished_at)}"
        return f"{elapsed(self.stage.started_at)} so far" if self.stage.status == "running" else ""


def stages(conn: sqlite3.Connection, task: Task) -> list[StageView]:
    out = []
    for s in repo.list_stages(conn, task.id):
        decisions = repo.stage_decisions(conn, s.id)
        final = [d for d in decisions if d.kind != "continue"]
        verdicts = [d for d in decisions if d.kind == "continue"]
        messages = repo.stage_messages(conn, s.id)
        out.append(
            StageView(
                stage=s,
                rounds=_rounds(messages, verdicts),
                decision=final[-1] if final else None,
                checkpoint=repo.stage_checkpoint(conn, s.id),
                log=build.read_log(task.id, s) if s.kind in ("build", "review") else [],
                messages=messages,
                verdicts=verdicts,
            )
        )
    return out


def _rounds(messages: list[Message], verdicts: list[Decision]) -> list[Round]:
    bubbles = {m.id: Bubble(m) for m in messages}
    top: list[Bubble] = []
    for m in messages:
        parent = bubbles.get(m.reply_to) if m.reply_to else None
        (parent.replies if parent else top).append(bubbles[m.id])
    numbers = sorted({b.message.round for b in top} | {v.round for v in verdicts})
    by_round = {v.round: v for v in verdicts}
    return [Round(n, [b for b in top if b.message.round == n], by_round.get(n)) for n in numbers]


def points(messages: list[Message], verdicts: list[Decision], members: tuple[str, ...]) -> list[Point]:
    """Where an undecided debate stands, a line each: what the judge last
    said is still open, the user's comments and the questions (answered or
    not), and the latest round's critiques: open, or revised since."""
    debate = [m for m in messages if m.round > 0]
    out = []
    if verdicts:
        out.append(Point("verdict", "judge", one_line(verdicts[-1].rationale, 240), "open"))
    answers = Counter(m.reply_to for m in messages if m.type == "answer")
    for m in debate:
        if m.type == "comment":
            done = answers[m.id] >= max(len(members), 1)
            out.append(Point("comment", "user", one_line(m.body["text"]), "answered" if done else "open"))
    if not debate:
        return out
    last = max(m.round for m in debate)
    for m in debate:
        if m.type == "question" and (m.round == last or not answers[m.id]):
            status = "answered" if answers[m.id] else "open"
            out.append(Point("question", m.role, one_line(m.body["question"]), status, m.to_role))
    revised = {m.role for m in debate if m.round == last and m.type == "revision"}
    for m in debate:
        if m.type == "critique" and m.round == last:
            for c in m.body.get("critiques", []):
                status = "revised" if c["of"] in revised else "open"
                out.append(Point("critique", m.role, one_line(c["weaknesses"]), status, c["of"]))
    return out


@dataclass
class Step:
    key: str
    label: str
    # done, upcoming, or where the task is: running, queued, waiting (for
    # the user), paused, failed, cancelled.
    state: str
    # How many times its stage ran, when more than once: a rejected
    # checkpoint, the review's fixes.
    runs: int = 0


def steps(task: Task, views: list[StageView], checkpoint: Checkpoint | None, asks: int = 0) -> list[Step]:
    """The task's progress, a step per stage and the final diff. `asks` are
    the agent's steps waiting for the user to allow them."""
    keys = [k for k, _ in STEPS]
    if task.status == "done":
        current = len(keys)
    elif checkpoint is not None and checkpoint.kind == "diff":
        current = keys.index("diff")
    else:
        current = keys.index(task.stage) if task.stage in keys else keys.index("diff")
    state = "waiting" if asks and task.status == "running" else task.status
    runs = Counter(v.stage.kind for v in views)
    return [
        Step(key, label, "done" if i < current else state if i == current else "upcoming", runs[key])
        for i, (key, label) in enumerate(STEPS)
    ]


@dataclass
class Now:
    """What the task is doing now, in a sentence."""

    text: str
    # A second line: what the agent did last, what's asked, who's still writing.
    detail: str = ""
    # How long it's been so: "4 min in this stage", "waiting for 2 h".
    clock: str = ""
    # As a step's: running, queued, waiting, paused, failed, cancelled, done.
    state: str = "running"


def now(
    task: Task,
    views: list[StageView],
    checkpoint: Checkpoint | None,
    approvals: list[Approval],
    changed: int = 0,
    open_points: int = 0,
) -> Now:
    """What's going on, from the task's state: what waits for the user, the
    debate's round and step, the agent's log. `changed` is how many files the
    branch changed so far; `open_points` the review's points not yet fixed."""
    view = views[-1] if views else None
    stage = task.stage
    if task.status == "done":
        took = elapsed(task.created_at, task.finished_at)
        return Now(f"Done: the change is on its branch, {task.branch}.", clock=f"took {took} in all", state="done")
    if task.status == "cancelled":
        return Now(f"Cancelled at the {stage} stage. What it did so far is below.", state="cancelled")
    if task.status == "failed":
        return Now(f"Stopped: the {stage} stage failed.", state="failed")
    if task.status == "paused":
        clock = f"paused for {elapsed(task.paused_at)}" if task.paused_at else ""
        return Now(
            f"Paused at the {stage} stage. Resume it to go on from where it stopped.", clock=clock, state="paused"
        )
    if approvals:
        a = approvals[0]
        text = (
            f"Waiting for you to allow the agent's {a.tool} step"
            if len(approvals) == 1
            else f"Waiting for you to allow {len(approvals)} steps the agent asks for"
        )
        return Now(text, one_line(a.detail), f"waiting for {elapsed(a.created_at)}", "waiting")
    if checkpoint is not None:
        return Now(
            f"Waiting for your approval of {CHECKPOINT_LABELS[checkpoint.kind]}",
            "Approve it to go on, or reject it with what to change.",
            f"waiting for {elapsed(checkpoint.created_at)}",
            "waiting",
        )
    if task.status == "waiting" or view is None:
        return Now("Waiting for you", state="waiting")
    if task.status == "queued":
        text = f"Queued: the {stage} " + (
            "starts as soon as the factory is free" if view.stage.status == "pending" else "goes on in a moment"
        )
        return Now(text, clock=f"queued for {elapsed(task.updated_at)}", state="queued")
    doing = _building(view, changed, open_points) if view.stage.kind == "build" else _debating(view)
    if view.stage.started_at:
        doing.clock = f"{elapsed(view.stage.started_at)} in this stage"
    return doing


def _debating(view: StageView) -> Now:
    """Where a spec, plan or review debate is: its round, and its step."""
    kind = view.stage.kind
    members = view.members
    solo = len(members) < 2
    if view.decision is not None:
        # A review the judge decided: the full tests run before the final checkpoint.
        tests = _last_tests(view.log)
        if tests and tests.get("kind") == "full":
            return Now("Running all the tests before your final check", tests.get("command", ""))
        return Now("Getting the final diff ready for you")
    round, step = next_step(view.stage, len(members))
    prefix = "" if solo else f"Round {round} of the {kind}: "
    team = _names(members)
    writes = {"proposals": "proposal", "critiques": "critique", "revisions": "revision"}.get(step)
    wrote = [r for r in members if any((m.round, m.type, m.role) == (round, writes, r) for m in view.messages)]
    if not wrote:
        # Before each step, the members answer what was put to them.
        waiting = _unanswered(view.messages, members)
        if waiting:
            m = waiting[0]
            if m.type == "comment":
                who = f"the {team}" if solo else "the team"
                return Now(_sentence(f"{prefix}{who} is answering your comment"), one_line(m.body["text"]))
            text = f"{prefix}the {_name(m.to_role)} is answering the {_name(m.role)}'s question"
            return Now(_sentence(text), one_line(m.body["question"]))
    things = "reviews" if kind == "review" else "proposals"
    if solo:
        member = _name(members[0]) if members else "team"
        text = (
            f"The {member} is writing the {kind}"
            if step == "proposals"
            else f"The judge is checking the {member}'s {kind}"
        )
        return Now(text)
    if step == "proposals":
        text = f"{prefix}the {team} are each " + ("reviewing the diff" if kind == "review" else "writing a proposal")
    elif step == "critiques":
        text = f"{prefix}the {team} are critiquing each other's {things}"
    elif step == "revisions":
        text = f"{prefix}the {team} are revising their {things} in light of the critiques, and voting for the best"
    elif kind == "review":
        text = f"{prefix}the judge is deciding whether the diff is good to go"
    else:
        text = f"{prefix}the judge is weighing the proposals"
    detail = ""
    left = [r for r in members if r not in wrote]
    if wrote and left:
        detail = f"{len(wrote)} of {len(members)} done; waiting on the {_names(left)}"
    return Now(text, detail)


def _building(view: StageView, changed: int, open_points: int) -> Now:
    """Where a build is: the agent at work, or the tests after it."""
    start = next((e for e in view.log if e.get("type") == "start"), None)
    tests = _last_tests(view.log)
    if view.stage.step is None:
        if start is None:
            return Now("Getting the build ready: the branch's own folder, and the sandbox the agent works in")
        agent = AGENT_NAMES.get(start.get("agent", ""), "The coding agent")
        if view.stage.attempt > 1 and open_points:
            doing = f"fixing the review's {_count(open_points, 'point')}"
        elif view.stage.attempt > 1:
            doing = "reworking the change"
        else:
            doing = "building"
        files = f" ({_count(changed, 'file')} changed so far)" if changed else ""
        latest = next((e for e in reversed(view.log) if e.get("type") in ("tool", "text")), None)
        detail = ""
        if latest is not None:
            said = f"{latest['tool']} {latest.get('detail', '')}" if latest["type"] == "tool" else latest["text"]
            detail = f"Latest: {one_line(said)}"
        return Now(f"{agent} is {doing}{files}", detail)
    if view.stage.step == "agent":
        if tests and tests.get("kind") == "short":
            return Now("Running the new tests: the change's own", tests.get("command", ""))
        return Now("The tester is picking which tests of the change to run")
    if tests and tests.get("kind") == "full":
        return Now("Running all the tests", tests.get("command", ""))
    return Now("Checking whether to run all the tests now")


def _unanswered(messages: list[Message], members: tuple[str, ...]) -> list[Message]:
    """Questions and the user's comments some member hasn't answered yet."""
    answered = {(m.role, m.reply_to) for m in messages if m.type == "answer"}
    out = []
    for m in messages:
        if m.type == "question":
            askees = [m.to_role]
        elif m.type == "comment" and m.round > 0:
            askees = list(members)
        else:
            continue
        if any(r in members and (r, m.id) not in answered for r in askees):
            out.append(m)
    return out


def _last_tests(log: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((e for e in reversed(log) if e.get("type") == "tests"), None)


def _name(role: str | None) -> str:
    return NAMES.get(role or "", role or "")


def _names(roles: list[str] | tuple[str, ...]) -> str:
    """architect, security agent and tester."""
    names = [_name(r) for r in roles]
    return names[0] if len(names) < 2 else f"{', '.join(names[:-1])} and {names[-1]}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


def one_line(text: str | None, limit: int = 160) -> str:
    """The first line of a model's Markdown that says something, as plain
    text, cut to `limit`: a heading only when there's nothing else."""
    lines = [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip() and not line.strip().startswith("```") and not set(line.strip()) <= set("-=*_|: ")
    ]
    if not lines:
        return ""
    line = next((li for li in lines if not li.startswith("#")), lines[0])
    line = re.sub(r"^(#+|[-*+>]|\d+[.)])\s+", "", line)
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"\*\*|__|`", "", line)
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def elapsed(start: str | None, end: str | None = None) -> str:
    """How long from `start` to `end` (or now), roughly: 45 s, 4 min, 1 h 5 min."""
    if not start:
        return ""
    until = datetime.fromisoformat(end) if end else datetime.now(UTC)
    seconds = max(0, int((until - datetime.fromisoformat(start)).total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


@dataclass
class Overview:
    """The task page's facts beside the debate."""

    costs: dict
    approvals: list[Approval]
    points: list[dict]
    tests: list
    changes: list
    diff: str
    editor: str | None


def overview(conn: sqlite3.Connection, task: Task, editor: str | None) -> Overview:
    started = task.base_commit is not None
    return Overview(
        costs=service.costs(conn, task.id),
        approvals=service.approvals(conn, task.id),
        points=service.review_points(conn, task.id),
        tests=service.test_runs(conn, task.id),
        changes=service.changes(conn, task.id) if started else [],
        diff=service.diff(conn, task.id) if started and task.status in ("waiting", "done") else "",
        editor=editor,
    )
