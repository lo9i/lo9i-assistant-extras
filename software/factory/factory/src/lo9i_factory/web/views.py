"""What the page shows, put together from the service: the board's cards, and
each stage of a task as a chat (SPEC §8): one bubble per message, with the
questions and votes it raised and the answers they got nested under it, the
judge's word after each round, the decision it ended in."""

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import build, repo, service
from ..models import COLUMNS, Approval, Checkpoint, Decision, Message, Stage, Task


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
class StageView:
    stage: Stage
    rounds: list[Round]
    # How the stage ended: the winner, accept or fixes.
    decision: Decision | None
    checkpoint: Checkpoint | None
    # The agent's log, for a build.
    log: list[dict[str, Any]]

    @property
    def title(self) -> str:
        again = f" (run {self.stage.attempt})" if self.stage.attempt > 1 else ""
        return {"spec": "Spec", "plan": "Plan", "build": "Build", "review": "Review"}[self.stage.kind] + again


def stages(conn: sqlite3.Connection, task: Task) -> list[StageView]:
    out = []
    for s in repo.list_stages(conn, task.id):
        decisions = repo.stage_decisions(conn, s.id)
        final = [d for d in decisions if d.kind != "continue"]
        out.append(
            StageView(
                stage=s,
                rounds=_rounds(repo.stage_messages(conn, s.id), [d for d in decisions if d.kind == "continue"]),
                decision=final[-1] if final else None,
                checkpoint=repo.stage_checkpoint(conn, s.id),
                log=build.read_log(task.id, s) if s.kind == "build" else [],
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
