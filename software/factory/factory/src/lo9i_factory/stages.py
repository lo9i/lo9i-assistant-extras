"""The stages (SPEC §2): what the spec and plan debate, who takes part, and
the checkpoint each ends at; then the build and its review (build.py).

- SPEC: the architect proposes, the judge decides: CHECKPOINT 1.
- PLAN: the architect, security and tester debate, the judge decides:
  CHECKPOINT 2. They start from the approved spec.

The factory's only result is the branch: a task that asks for a review or
an answer rather than code gets a document written in the repository.

A stage run again after its checkpoint was rejected starts from the result
that was rejected; the user's comment opens its thread.
"""

import sqlite3

from . import agents, build, repo, repo_context, service
from .engine import Brief, debate
from .models import Stage, Task
from .providers import Providers

# Who debates in each stage; the judge decides. SPEC §2 names architect(s)
# for the spec; the team has one.
ROLES = {
    "spec": ("architect",),
    "plan": ("architect", "security", "tester"),
}
GOALS = {
    "spec": (
        "the spec for this task: what to build and why, how it behaves, its edge cases, what's out of scope,"
        " and how we'll know it's done. Not how to build it: the plan comes next"
    ),
    "plan": (
        "the plan to build the approved spec: the files to change and how, the steps in order, the tests to"
        " write, the risks and how to handle them"
    ),
}


async def advance(
    conn: sqlite3.Connection, providers: Providers, task_id: int, make_agent: build.MakeAgent = agents.make
) -> None:
    """Run the task's stages until it waits for the user or stops: a spec or
    plan to its checkpoint; a build, its review, and the builds the review
    sends back, to the final checkpoint."""
    while True:
        task = service.get_task(conn, task_id)
        stage = repo.current_stage(conn, task_id)
        if stage.kind in ROLES:
            decision = await debate(conn, providers, stage.id, brief(conn, task, stage))
            service.open_checkpoint(conn, task_id, decision.id)
            return
        if stage.kind == "build":
            await build.build(conn, providers, task, stage, make_agent)
        elif await build.review(conn, providers, task, stage):
            return


def brief(conn: sqlite3.Connection, task: Task, stage: Stage) -> Brief:
    parts = [f"## {task.title}\n\n{task.description}", f"The repository: {task.repo}", build.DELIVERS]
    if stage.kind == "plan" and (spec := service.approved(conn, task.id, "spec")):
        parts.append(f"## The approved spec\n\n{spec}")
    for rejected in service.rejected(conn, task.id, stage):
        parts.append(
            f"## An earlier {stage.kind}, which the user rejected\n\n{rejected}\n\n"
            "The user's comment on it opens the debate: this run must answer it."
        )
    if task.include_context:
        parts.append(repo_context.read(task.repo))
    return Brief(goal=GOALS[stage.kind], context="\n\n".join(parts), roles=ROLES[stage.kind])
