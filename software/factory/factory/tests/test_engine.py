"""The debate engine, with every member on a fake model that answers each kind
of message as the test scripts it."""

import asyncio

import pytest
from fakes import Team, default, on_fakes

from lo9i_factory import repo, service, settings
from lo9i_factory.engine import Brief, StoppedError, debate
from lo9i_factory.meter import LimitReachedError
from lo9i_factory.providers import ProviderError, Providers, Request

TEAM = ("architect", "security", "tester")
BRIEF = Brief(goal="the spec for adding a search cache", context="Add a cache to the search.", roles=TEAM)


@pytest.fixture
def task(conn, git_repo):
    on_fakes(conn)
    return service.create_task(conn, str(git_repo), "Add a cache to the search")


def stage_id(conn, task):
    return service.stages(conn, task.id)[-1].id


def run(conn, task, team, brief=BRIEF):
    providers = Providers(env={}, clients={"copilot": team})
    return asyncio.run(debate(conn, providers, stage_id(conn, task), brief))


def thread(conn, task):
    return [(m.role, m.type) for m in repo.stage_messages(conn, stage_id(conn, task))]


def text(request: Request) -> str:
    return "\n".join(t.text for t in request.turns)


def test_a_round_of_proposals_critiques_and_revisions_ends_in_the_judges_decision(conn, task):
    team = Team()
    decision = run(conn, task, team)

    assert team.kinds() == [
        *((r, "proposal") for r in TEAM),
        *((r, "critique") for r in TEAM),
        *((r, "revision") for r in TEAM),
        ("judge", "verdict"),
    ]
    assert decision.kind == "winner" and decision.rationale == "Simplest" and decision.model == "copilot:judge"
    assert decision.body == {"winner": "architect", "result": "Final spec", "options": [], "agreed": True}
    messages = repo.stage_messages(conn, stage_id(conn, task))
    winner = next(m for m in messages if m.id == decision.winner_message_id)
    assert (winner.role, winner.type, winner.body["proposal"]) == ("architect", "revision", "architect's plan v2")
    votes = [(m.role, m.body["for"]) for m in messages if m.type == "vote"]
    assert votes == [("architect", "security"), ("security", "architect"), ("tester", "architect")]
    assert all(m.model == f"copilot:{m.role}" for m in messages)

    stage = service.stages(conn, task.id)[0]
    assert (stage.status, stage.round, stage.step) == ("running", 1, "judge")
    assert service.get_task(conn, task.id).status == "running"
    assert service.costs(conn, task.id)["requests"] == 10
    linked = conn.execute("SELECT COUNT(*) FROM costs WHERE message_id IS NOT NULL").fetchone()[0]
    assert linked == 9  # Every member's message; the judge's verdict is a decision.


def test_members_write_their_proposals_apart_and_read_everything_after(conn, task):
    team = Team()
    run(conn, task, team)
    proposals = [r for _, k, r in team.calls if k == "proposal"]
    assert all("'s plan" not in text(r) for r in proposals)
    critique = next(r for _, k, r in team.calls if k == "critique")
    assert all(f"{role} (copilot:{role}), proposal:\n{role}'s plan" in text(critique) for role in TEAM)
    assert "Risks:\n- stale results" in text(critique)
    judge = team.calls[-1][2]
    assert "architect (copilot:architect) votes for security's proposal: clearest" in text(judge)
    assert "Round 1 of at most 3" in judge.turns[-1].text


def test_without_agreement_another_round_follows_until_the_last(conn, task):
    settings.update(conn, {"max_rounds": 2})
    no = {"consensus": False, "reason": "security wants a smaller TTL", "winner": "", "result": "", "rationale": "",
          "options": []}  # fmt: skip
    team = Team({("judge", "verdict"): [no]})
    decision = run(conn, task, team)

    assert [k for _, k in team.kinds()[10:]] == ["critique"] * 3 + ["revision"] * 3 + ["decision"]
    assert decision.body["agreed"] is False and decision.round == 2
    first = repo.stage_decisions(conn, stage_id(conn, task))[0]
    assert (first.kind, first.round, first.rationale) == ("continue", 1, "security wants a smaller TTL")
    second_critique = team.calls[10][2]
    assert "The judge, after round 1: no agreement yet. security wants a smaller TTL" in text(second_critique)
    assert "## Round 2" not in text(second_critique)
    assert len([m for m in thread(conn, task) if m[1] == "revision"]) == 6


def test_directed_questions_are_answered_before_the_next_step(conn, task):
    def ask(request):
        return {
            "proposal": "Cache in Redis",
            "risks": [],
            "questions": [{"to": "security", "question": "Is Redis ok?"}],
        }

    team = Team({("architect", "proposal"): [ask]})
    run(conn, task, team)

    assert team.kinds()[3] == ("security", "answers")
    messages = repo.stage_messages(conn, stage_id(conn, task))
    question = next(m for m in messages if m.type == "question")
    answer = next(m for m in messages if m.type == "answer")
    assert (question.role, question.to_role, question.body) == ("architect", "security", {"question": "Is Redis ok?"})
    assert (answer.role, answer.to_role, answer.reply_to) == ("security", "architect", question.id)
    assert f"[{question.id}] The architect asks: Is Redis ok?" in team.calls[3][2].turns[-1].text
    critique = team.calls[4][2]
    assert f"security (copilot:security), answering [{question.id}]: security answers {question.id}" in text(critique)


def test_a_comment_during_the_debate_is_answered_by_every_member_first(conn, task):
    def step_in(request):
        service.comment(conn, task.id, "Keep it under 100 lines")
        return default("architect", "critique", request)

    team = Team({("architect", "critique"): [step_in]})
    run(conn, task, team)

    assert [k for _, k in team.kinds()[6:9]] == ["answers"] * 3
    messages = repo.stage_messages(conn, stage_id(conn, task))
    comment = next(m for m in messages if m.type == "comment")
    assert (comment.round, comment.body) == (1, {"text": "Keep it under 100 lines"})
    assert sorted(m.role for m in messages if m.type == "answer" and m.reply_to == comment.id) == sorted(TEAM)
    revision = next(r for _, k, r in team.calls if k == "revision")
    assert f"[{comment.id}] The user: Keep it under 100 lines" in text(revision)


def test_a_comment_before_the_debate_is_context_not_a_question(conn, task):
    service.comment(conn, task.id, "Only the search page")
    team = Team()
    run(conn, task, team)
    assert "answers" not in [k for _, k in team.kinds()]
    assert "## Before the debate\n\n[1] The user: Only the search page" in text(team.calls[0][2])


def test_a_failed_call_pauses_the_task_and_resuming_goes_on_from_there(conn, task):
    team = Team({("security", "critique"): [ProviderError("GitHub Copilot answered 503")]})
    with pytest.raises(ProviderError, match="the security's model failed: GitHub Copilot answered 503"):
        run(conn, task, team)

    paused = service.get_task(conn, task.id)
    assert (paused.status, paused.reason) == ("paused", "the security's model failed: GitHub Copilot answered 503")
    assert [m for m in thread(conn, task) if m[1] == "critique"] == [("architect", "critique"), ("tester", "critique")]

    service.resume(conn, task.id)
    team.calls.clear()
    run(conn, task, team)
    assert team.kinds()[0] == ("security", "critique")
    assert len([k for k in team.kinds() if k[1] == "proposal"]) == 0


def test_the_cost_limit_pauses_the_debate(conn, task):
    settings.update(conn, {"max_usd": 0.01})
    with conn:
        repo.insert_cost(conn, task_id=task.id, role="architect", provider="openai", model="gpt-5", usd=0.02)
    team = Team()
    with pytest.raises(LimitReachedError):
        run(conn, task, team)
    assert team.calls == []
    assert service.get_task(conn, task.id).status == "paused"


def test_cancelling_stops_the_debate_at_the_next_call(conn, task):
    def cancel(request):
        service.cancel(conn, task.id)
        return default("architect", "proposal", request)

    team = Team({("architect", "proposal"): [cancel]})
    with pytest.raises(StoppedError, match="task 1 is cancelled"):
        run(conn, task, team)
    assert team.kinds() == [("architect", "proposal")]
    assert thread(conn, task) == [("architect", "proposal")]


def test_a_paused_task_isnt_debated(conn, task):
    service.pause(conn, task.id, "reached max_usd")
    with pytest.raises(StoppedError, match="task 1 is paused"):
        run(conn, task, Team())


def test_a_single_member_proposes_and_the_judge_decides(conn, task):
    team = Team()
    decision = run(conn, task, team, Brief(goal="the spec", context="Add a cache.", roles=("architect",)))
    assert team.kinds() == [("architect", "proposal"), ("judge", "decision")]
    assert "questions" not in team.calls[0][2].schema["properties"]
    assert decision.body["winner"] == "architect"


def test_a_decided_stage_isnt_debated_again(conn, task):
    first = run(conn, task, Team())
    team = Team()
    assert run(conn, task, team) == first and team.calls == []
