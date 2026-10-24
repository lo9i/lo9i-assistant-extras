"""The debate a stage runs (SPEC §2, §7, §9), and the judge's decision.

A round: each member writes a proposal on its own model (round 1 only),
then critiques the others' latest, then revises and votes for the best of
the others'. The judge then says whether the team agrees: if so it decides,
picking the best proposal or merging several; if not, another round. At the
last round (max_rounds), or with a single member, it decides regardless.

Questions and comments: a proposal, critique or revision may ask another
member questions, and the user may comment while the debate runs
(service.comment). Before each step, every question gets its addressee's
answer, and every comment each member's, so the debate waits for them.

Resuming: each message is saved as it comes, and the stage keeps its round
and the last step finished in it, so a debate started again (after a
restart, a pause, a failed call) goes on from there: members who already
wrote in a step aren't asked again.

Stopping: the task must be running. One cancelled or paused by the user
stops at the next call. A call that fails pauses the task with the reason,
as reaching max_usd does (meter.py); the members that answered keep their
messages.
"""

import asyncio
import sqlite3
from dataclasses import dataclass

from .. import meter, repo, service, settings
from ..errors import ConflictError
from ..models import Decision, Message, Stage
from ..providers import ProviderError, Providers, Request, Turn
from . import schemas, thread

# The steps of round 1; later rounds skip the proposals. A single member
# only proposes, and the judge decides on it.
STEPS = ("proposals", "critiques", "revisions", "judge")
SOLO_STEPS = ("proposals", "judge")
# What each step writes.
TYPES = {"proposals": "proposal", "critiques": "critique", "revisions": "revision"}


class StoppedError(ConflictError):
    """The task isn't running any more: paused or cancelled."""


@dataclass(frozen=True)
class Brief:
    """What a stage debates (stages.py and build.py write one per stage)."""

    # What the debate settles, as in "the spec for this task".
    goal: str
    # The task, earlier stages' results, what the user said.
    context: str
    # The members who debate; the judge decides.
    roles: tuple[str, ...]
    # A review of a diff: the members review it, and the judge accepts it or
    # sends back the points to fix, instead of picking a winner.
    review: bool = False


async def debate(conn: sqlite3.Connection, providers: Providers, stage_id: int, brief: Brief) -> Decision:
    """Run (or go on with) a stage's debate until the judge decides. Raises
    StoppedError, LimitReachedError or ProviderError when it stops before."""
    stage = repo.get_stage(conn, stage_id)
    decided = [d for d in repo.stage_decisions(conn, stage_id) if d.kind != "continue"]
    if decided:
        return decided[-1]
    start(conn, stage)
    return await _Debate(conn, providers, stage, brief).run()


def next_step(stage: Stage, members: int) -> tuple[int, str]:
    """The round and step a debate of `members` runs now, from where its
    stage got to; ("judge") once the judge has decided too. The task's page
    reads it to say what's going on."""
    steps = SOLO_STEPS if members < 2 else STEPS if stage.round <= 1 else STEPS[1:]
    if stage.round == 0:
        return 1, steps[0]
    if stage.step is None:
        return stage.round, steps[0]
    if stage.step == "judge":
        return stage.round, "judge"
    return stage.round, steps[steps.index(stage.step) + 1]


def start(conn: sqlite3.Connection, stage: Stage) -> None:
    """The task is running, in this stage. Raises StoppedError for a task
    that isn't queued or running already."""
    task = service.get_task(conn, stage.task_id)
    if task.status not in ("queued", "running"):
        raise StoppedError(f"task {task.id} is {task.status}")
    if stage.status not in ("pending", "running"):
        raise ConflictError(f"the {stage.kind} stage is {stage.status}")
    with conn:
        repo.update_task(conn, task.id, {"status": "running"})
        if stage.status == "pending":
            repo.update_stage(conn, stage.id, {"status": "running", "started_at": repo.now()})


class _Debate:
    def __init__(self, conn: sqlite3.Connection, providers: Providers, stage: Stage, brief: Brief) -> None:
        self.conn = conn
        self.providers = providers
        self.task_id = stage.task_id
        self.stage_id = stage.id
        self.brief = brief
        self.roles = list(brief.roles)
        self.max_rounds = settings.load(conn).max_rounds

    async def run(self) -> Decision:
        try:
            return await self._run()
        except ProviderError as e:
            # Paused with the reason, to resume from here once it's fixed.
            if service.get_task(self.conn, self.task_id).status == "running":
                service.pause(self.conn, self.task_id, str(e))
            raise

    async def _run(self) -> Decision:
        while True:
            round, step = self._next()
            self._check()
            await self._answer_pending()
            if step == "judge":
                decision = await self._judge(round)
                if decision.kind != "continue":
                    return decision
            else:
                await self._step(round, step)
                with self.conn:
                    repo.update_stage(self.conn, self.stage_id, {"round": round, "step": step})

    def _next(self) -> tuple[int, str]:
        return next_step(repo.get_stage(self.conn, self.stage_id), len(self.roles))

    def _check(self) -> None:
        status = service.get_task(self.conn, self.task_id).status
        if status != "running":
            raise StoppedError(f"task {self.task_id} is {status}")

    # --- Asking ---

    async def _ask(self, role: str, schema: dict, instruction: str, hide: tuple[int, str] | None = None):
        """One member's (or the judge's) answer, through the meter. `hide`
        (round, type) leaves the others' messages of that kind out of the
        thread: a member writes its proposal on its own."""
        self._check()
        messages = [
            m
            for m in repo.stage_messages(self.conn, self.stage_id)
            if hide is None or (m.round, m.type) != hide or m.role == role
        ]
        decisions = [d for d in repo.stage_decisions(self.conn, self.stage_id) if d.kind == "continue"]
        so_far = thread.render(messages, decisions) or "Nothing yet: this is the first step."
        request = Request(
            system=self._system(role),
            turns=[
                Turn("user", f"# The task\n\n{self.brief.context}\n\n# This debate\n\nIt settles {self.brief.goal}."),
                Turn("user", f"# The debate so far\n\n{so_far}"),
                Turn("user", f"# Your turn\n\n{instruction}"),
            ],
            schema=schema,
        )
        try:
            return await meter.call(self.conn, self.providers, self.task_id, self.stage_id, role, request)
        except ProviderError as e:
            raise ProviderError(f"the {role}'s model failed: {e}") from None

    def _system(self, role: str) -> str:
        prompt = settings.load(self.conn).prompts[role]
        if role == "judge":
            return f"{prompt}\n\nThe team: {', '.join(self.roles)}. Answer in the JSON format asked for."
        others = [r for r in self.roles if r != role]
        team = f"a debate with the {', the '.join(others)}" if others else "this stage on your own"
        return (
            f"{prompt}\n\nYou are the {role}, in {team}; a judge decides. Answer in the JSON format asked for,"
            " in the language the task is written in."
        )

    async def _all(self, calls: list) -> None:
        """Run members' calls at once. Those that answer keep what they wrote,
        even when another fails; the first failure is raised then."""
        results = await asyncio.gather(*calls, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r

    # --- Proposals, critiques, revisions ---

    async def _step(self, round: int, step: str) -> None:
        type = TYPES[step]
        wrote = {m.role for m in repo.stage_messages(self.conn, self.stage_id) if (m.round, m.type) == (round, type)}
        await self._all([self._write(round, role, type) for role in self.roles if role not in wrote])

    async def _write(self, round: int, role: str, type: str) -> None:
        others = [r for r in self.roles if r != role]
        schema = {"proposal": schemas.proposal, "critique": schemas.critique, "revision": schemas.revision}[type]
        key = f"review_{type}" if self.brief.review else type
        answer = await self._ask(role, schema(others), _INSTRUCTIONS[key].format(goal=self.brief.goal), (round, type))
        body = dict(answer.data)
        questions = body.pop("questions", [])
        vote = body.pop("vote", None)
        with self.conn:
            id = repo.insert_message(
                self.conn,
                task_id=self.task_id,
                stage_id=self.stage_id,
                round=round,
                role=role,
                model=answer.model,
                type=type,
                body=body,
            )
            repo.link_cost(self.conn, answer.cost_id, id)
            if vote is not None:
                repo.insert_message(
                    self.conn,
                    task_id=self.task_id,
                    stage_id=self.stage_id,
                    round=round,
                    role=role,
                    model=answer.model,
                    type="vote",
                    body=vote,
                    to_role=vote["for"],
                    reply_to=id,
                )
            for q in questions:
                repo.insert_message(
                    self.conn,
                    task_id=self.task_id,
                    stage_id=self.stage_id,
                    round=round,
                    role=role,
                    model=answer.model,
                    type="question",
                    body={"question": q["question"]},
                    to_role=q["to"],
                    reply_to=id,
                )

    # --- Questions and comments ---

    async def _answer_pending(self) -> None:
        """Each member answers the questions put to it and the user's comments
        made during the debate, before the debate goes on."""
        messages = repo.stage_messages(self.conn, self.stage_id)
        answered = {(m.role, m.reply_to) for m in messages if m.type == "answer"}
        pending: dict[str, list[Message]] = {}
        for m in messages:
            if m.type == "question":
                askees = [m.to_role]
            elif m.type == "comment" and m.round > 0:
                askees = self.roles
            else:
                continue
            for role in askees:
                if role in self.roles and (role, m.id) not in answered:
                    pending.setdefault(role, []).append(m)
        await self._all([self._answer(role, items) for role, items in pending.items()])

    async def _answer(self, role: str, items: list[Message]) -> None:
        listed = "\n\n".join(
            f"[{m.id}] "
            + (f"The user: {m.body['text']}" if m.type == "comment" else f"The {m.role} asks: {m.body['question']}")
            for m in items
        )
        answer = await self._ask(role, schemas.ANSWERS, _INSTRUCTIONS["answers"].format(items=listed))
        by_id = {m.id: m for m in items}
        with self.conn:
            for a in answer.data["answers"]:
                asked = by_id.pop(a["id"], None)
                if asked is None:  # Not one put to it, or answered twice.
                    continue
                id = repo.insert_message(
                    self.conn,
                    task_id=self.task_id,
                    stage_id=self.stage_id,
                    round=asked.round,
                    role=role,
                    model=answer.model,
                    type="answer",
                    body={"answer": a["answer"]},
                    to_role=asked.role,
                    reply_to=asked.id,
                )
                repo.link_cost(self.conn, answer.cost_id, id)

    # --- The judge ---

    async def _judge(self, round: int) -> Decision:
        last = len(self.roles) < 2 or round >= self.max_rounds
        if self.brief.review:
            return await self._judge_review(round, last)
        if last:
            schema, instruction = schemas.decision(self.roles), _INSTRUCTIONS["decision"]
        else:
            schema, instruction = schemas.verdict(self.roles), _INSTRUCTIONS["verdict"]
        answer = await self._ask(
            "judge", schema, instruction.format(goal=self.brief.goal, round=round, max_rounds=self.max_rounds)
        )
        data = answer.data
        if not last and not (data["consensus"] and data["winner"]):
            return self._decide(round, answer.model, "continue", data["reason"])
        body = {
            "winner": data["winner"],
            "result": data["result"],
            "options": data["options"],
            # The team agreed, rather than the rounds running out.
            "agreed": not last,
        }
        return self._decide(round, answer.model, "winner", data["rationale"], body, self._latest(data["winner"]))

    async def _judge_review(self, round: int, last: bool) -> Decision:
        schema = schemas.review_decision() if last else schemas.review_verdict()
        instruction = _INSTRUCTIONS["review_decision" if last else "review_verdict"]
        answer = await self._ask("judge", schema, instruction.format(round=round, max_rounds=self.max_rounds))
        data = answer.data
        if not last and not data["consensus"]:
            return self._decide(round, answer.model, "continue", data["reason"])
        points = [p for p in data["points"] if p.strip()]
        kind = "accept" if data["accept"] and not points else "fixes"
        return self._decide(round, answer.model, kind, data["rationale"], {"points": points, "agreed": not last})

    def _decide(
        self, round: int, model: str, kind: str, rationale: str, body: dict | None = None, winner: int | None = None
    ) -> Decision:
        """Save the judge's word: on to the next round, or the end."""
        with self.conn:
            id = repo.insert_decision(
                self.conn,
                task_id=self.task_id,
                stage_id=self.stage_id,
                round=round,
                model=model,
                kind=kind,
                rationale=rationale,
                body=body,
                winner_message_id=winner,
            )
            if kind == "continue":
                repo.update_stage(self.conn, self.stage_id, {"round": round + 1, "step": None})
            else:
                repo.update_stage(self.conn, self.stage_id, {"round": round, "step": "judge"})
        return repo.get_decision(self.conn, id)

    def _latest(self, role: str) -> int | None:
        """The winner's latest proposal; None for a merge."""
        mine = [
            m.id
            for m in repo.stage_messages(self.conn, self.stage_id)
            if m.role == role and m.type in ("proposal", "revision")
        ]
        return mine[-1] if mine else None


_INSTRUCTIONS = {
    "proposal": "Write your proposal: {goal}. Say what could go wrong with it. Ask the others anything you need.",
    "critique": (
        "Critique each other member's latest proposal: what it gets right, what it misses or gets wrong, and what"
        " to change. Be specific; leave out matters of taste."
    ),
    "revision": (
        "Revise your proposal in light of the critiques and answers: write it whole again, and say what you"
        " changed and which critiques you didn't take, and why. Then vote for the best proposal other than yours."
    ),
    "answers": "Answer each of these, with its id:\n\n{items}",
    "review_proposal": (
        "Write your review: {goal}. Put it as the points to fix, each specific (the file, what's wrong, what to"
        " do), or say it's good to go. Leave out matters of taste. Ask the others anything you need."
    ),
    "review_critique": (
        "Critique each other member's latest review: which points are right, which are wrong or don't matter,"
        " and what they missed."
    ),
    "review_revision": (
        "Revise your review in light of the critiques and answers: write it whole again, and say what you"
        " changed and why. Then vote for the best review other than yours."
    ),
    "review_verdict": (
        "Round {round} of at most {max_rounds} of reviewing has ended. Do the reviewers agree on the diff? If so,"
        " decide: accept it, or list the points the implementer must fix, merged from the reviews, and say why."
        " If not, say what's still open: another round follows."
    ),
    "review_decision": (
        "The review has ended. Decide: accept the diff, or list the points the implementer must fix, merged from"
        " the reviews, and say why."
    ),
    "verdict": (
        "Round {round} of at most {max_rounds} has ended. Does the team agree enough to settle {goal}? If so,"
        " decide: pick the best proposal, or merge the best of several, write the result whole, say why, and say"
        " what each losing proposal got wrong. If not, say what's still open, and leave the decision empty:"
        " another round follows."
    ),
    "decision": (
        "The debate has ended. Decide {goal}: pick the best proposal, or merge the best of several, write the"
        " result whole, say why, and say what each losing proposal got wrong."
    ),
}
