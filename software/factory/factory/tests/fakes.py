"""A team on fake models (copilot:<role>, so no prices are needed) that
answers each kind of debate message as a test scripts it."""

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from lo9i_factory import settings
from lo9i_factory.providers import Reply, Request

ROLES = ("architect", "implementer", "reviewer", "tester", "security", "judge")


def on_fakes(conn) -> None:
    """Every role on its fake model."""
    settings.update(conn, {f"models.{role}": f"copilot:{role}" for role in ROLES})


def kind(schema: dict) -> str:
    fields = set(schema["properties"])
    if "accept" in fields:
        return "review_verdict" if "consensus" in fields else "review_decision"
    if "command" in fields:
        return "tests"
    for field, name in (("consensus", "verdict"), ("winner", "decision"), ("answers", "answers")):
        if field in fields:
            return name
    if "critiques" in fields:
        return "critique"
    return "revision" if "changes" in fields else "proposal"


def others(schema: dict, key: str) -> list[str]:
    """The roles a schema lets a member address."""
    props = schema["properties"]
    if key == "questions":
        return props["questions"]["items"]["properties"]["to"]["enum"] if "questions" in props else []
    return props["critiques"]["items"]["properties"]["of"]["enum"]


def default(role: str, kind: str, request: Request) -> dict:
    schema = request.schema
    asks = {"questions": []} if "questions" in schema["properties"] else {}
    match kind:
        case "proposal":
            return {"proposal": f"{role}'s plan", "risks": ["stale results"], **asks}
        case "critique":
            return {
                "critiques": [
                    {"of": o, "strengths": "simple", "weaknesses": "no TTL", "suggestions": "add one"}
                    for o in others(schema, "critiques")
                ],
                **asks,
            }
        case "revision":
            vote = schema["properties"]["vote"]["properties"]["for"]["enum"][0]
            return {
                "proposal": f"{role}'s plan v2",
                "risks": [],
                "changes": "added a TTL",
                "vote": {"for": vote, "reason": "clearest"},
                **asks,
            }
        case "answers":
            ids = re.findall(r"^\[(\d+)\]", request.turns[-1].text, re.MULTILINE)
            return {"answers": [{"id": int(i), "answer": f"{role} answers {i}"} for i in ids]}
        case "tests":
            return {"command": "python3 -c 'print(\"short ok\")'", "why": "the change's test"}
        case "review_verdict":
            return {"consensus": True, "reason": "All agree", "accept": True, "rationale": "Good to go", "points": []}
        case "review_decision":
            return {"accept": True, "rationale": "Good to go", "points": []}
    decided = {"winner": "architect", "result": "Final spec", "rationale": "Simplest", "options": []}
    return {"consensus": True, "reason": "All agree", **decided} if kind == "verdict" else decided


def fixes(*points: str) -> dict:
    """The judge's verdict sending points back."""
    return {"consensus": True, "reason": "All agree", "accept": False, "rationale": "Not yet", "points": list(points)}


class Team:
    """Every role's model (copilot:<role>). `script` maps (role, kind) to the
    answers to give in turn: a dict, a callable of the request, or an
    exception to raise; once they run out, the default answer."""

    def __init__(self, script: dict[tuple[str, str], list[Any]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, str, Request]] = []

    async def complete(self, model: str, request: Request) -> Reply:
        k = kind(request.schema)
        self.calls.append((model, k, request))
        planned = self.script.get((model, k)) or []
        answer: Any = planned.pop(0) if planned else default(model, k, request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Callable):
            answer = answer(request)
        return Reply(json.dumps(answer), model)

    def kinds(self) -> list[tuple[str, str]]:
        return [(role, k) for role, k, _ in self.calls]


class FakeAgent:
    """The coding agent: writes `files` into the worktree, answers every
    review point in its prompt as resolved, and says it cost $0.50. Keeps
    each job it was given."""

    def __init__(self, files: dict[str, str] | None = None, results: list | None = None) -> None:
        self.files = files if files is not None else {"cache.py": "CACHE = {}\n", "test_cache.py": "def test(): pass\n"}
        self.results = results or []
        self.jobs: list = []

    def __call__(self, name: str, key: str) -> "FakeAgent":
        """As agents.make: the same fake whatever the agent."""
        self.made = (name, key)
        return self

    async def run(self, job, log, should_stop):
        from pathlib import Path

        from lo9i_factory.agents import Result

        self.jobs.append(job)
        for name, text in self.files.items():
            (Path(job.sandbox.worktree) / name).write_text(text + f"# build {len(self.jobs)}\n")
        log({"type": "tool", "tool": "Write", "detail": "cache.py"})
        if self.results:
            return self.results.pop(0)
        ids = [int(i) for i in re.findall(r"^- \[(\d+)\]", job.prompt, re.MULTILINE)]
        points = [{"id": i, "status": "resolved", "reason": "Done"} for i in ids]
        return Result(ok=True, summary="Added the cache", points=points, usd=0.5, input_tokens=1000)


def once(runner, conn, allow: bool = True) -> bool:
    """The runner's turn, with the user answering every approval it raises."""
    from lo9i_factory import service

    async def go():
        async def answering():
            while True:
                for t in service.list_tasks(conn):
                    for a in service.approvals(conn, t.id):
                        service.answer_approval(conn, a.id, allow, "" if allow else "not now")
                await asyncio.sleep(0.01)

        helper = asyncio.create_task(answering())
        try:
            return await runner.run_once()
        finally:
            helper.cancel()

    return asyncio.run(go())
