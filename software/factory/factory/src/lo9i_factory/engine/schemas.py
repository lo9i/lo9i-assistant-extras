"""The JSON each kind of debate message is written in (SPEC §7), as the
providers' structured outputs take it: every field required, nothing else
allowed. Who a role may address is limited to the others in the debate.
"""


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


def _list(items: dict, description: str) -> dict:
    return {"type": "array", "items": items, "description": description}


def _one_of(values: list[str], description: str) -> dict:
    return {"type": "string", "enum": values, "description": description}


def _questions(others: list[str]) -> dict[str, dict]:
    """Questions only when there's someone to ask."""
    if not others:
        return {}
    question = _object(to=_one_of(others, "Who it's for"), question=_text("The question"))
    return {"questions": _list(question, "Questions for another member, answered before the next step; [] for none")}


def proposal(others: list[str]) -> dict:
    return _object(
        proposal=_text("Your proposal, in Markdown"),
        risks=_list(_text("A risk"), "What could go wrong with it"),
        **_questions(others),
    )


def critique(others: list[str]) -> dict:
    one = _object(
        of=_one_of(others, "Whose proposal"),
        strengths=_text("What it gets right"),
        weaknesses=_text("What it misses or gets wrong"),
        suggestions=_text("What to change"),
    )
    return _object(critiques=_list(one, "One for each other member's latest proposal"), **_questions(others))


def revision(others: list[str]) -> dict:
    return _object(
        proposal=_text("Your revised proposal, whole, in Markdown"),
        risks=_list(_text("A risk"), "What could go wrong with it"),
        changes=_text("What you changed and why, and the critiques you didn't take and why"),
        vote=_object(**{"for": _one_of(others, "The best proposal other than yours"), "reason": _text("Why")}),
        **_questions(others),
    )


ANSWERS = _object(
    answers=_list(
        _object(id={"type": "integer", "description": "The question's or comment's id"}, answer=_text("Your answer")),
        "One for each question or comment put to you",
    )
)


def _decision(roles: list[str], empty: str = "") -> dict[str, dict]:
    """The judge's decision; `empty` allows an empty one, for no consensus."""
    winners = [*roles, "merged", *([""] if empty else [])]
    lost = _object(role=_one_of(roles, "Whose"), why_not=_text("What it got wrong or traded away"))
    return {
        "winner": _one_of(winners, "Whose proposal wins, or merged when you combined several" + empty),
        "result": _text("The winning proposal, whole, in Markdown, merged if so" + empty),
        "rationale": _text("Why it wins" + empty),
        "options": _list(lost, "The proposals that lost" + empty),
    }


def verdict(roles: list[str]) -> dict:
    """After a round: has the team agreed? If so, the decision with it."""
    return _object(
        consensus={"type": "boolean", "description": "Whether the team agrees enough to decide now"},
        reason=_text("Why the team has or hasn't agreed, and what's still open"),
        **_decision(roles, empty="; empty without consensus"),
    )


def decision(roles: list[str]) -> dict:
    """At the last round: the decision, agreed or not."""
    return _object(**_decision(roles))


def _review(empty: str = "") -> dict[str, dict]:
    """The judge's word on a diff; `empty` allows none, for no consensus."""
    return {
        "accept": {"type": "boolean", "description": "Accept the diff as it is" + empty},
        "rationale": _text("Why" + empty),
        "points": _list(_text("One thing to fix, specific enough to act on"), "What to fix; [] to accept" + empty),
    }


def review_verdict() -> dict:
    """After a round of reviewing a diff: do the reviewers agree? If so, the decision."""
    return _object(
        consensus={"type": "boolean", "description": "Whether the reviewers agree enough to decide now"},
        reason=_text("Why they do or don't agree, and what's still open"),
        **_review(empty="; ignored without consensus"),
    )


def review_decision() -> dict:
    """At the last round: accept the diff, or the fixes it needs."""
    return _object(**_review())
