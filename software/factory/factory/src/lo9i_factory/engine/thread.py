"""The debate as the team reads it: every message of the stage, by round,
each with its id, so an answer can say what it answers (SPEC §7: every agent
sees the full history of the stage)."""

from ..models import Decision, Message


def render(messages: list[Message], decisions: list[Decision]) -> str:
    """The thread so far, or "" before anyone wrote."""
    rounds = sorted({m.round for m in messages} | {d.round for d in decisions})
    parts = []
    for r in rounds:
        lines = [_message(m) for m in messages if m.round == r]
        lines += [f"The judge, after round {r}: no agreement yet. {d.rationale}" for d in decisions if d.round == r]
        if lines:
            parts.append(f"## {'Before the debate' if r == 0 else f'Round {r}'}\n\n" + "\n\n".join(lines))
    return "\n\n".join(parts)


def _message(m: Message) -> str:
    who = "The user" if m.role == "user" else f"{m.role} ({m.model})"
    b = m.body
    match m.type:
        case "proposal" | "revision":
            text = f"[{m.id}] {who}, {'revised proposal' if m.type == 'revision' else 'proposal'}:\n{b['proposal']}"
            if b.get("risks"):
                text += "\nRisks:\n" + "\n".join(f"- {r}" for r in b["risks"])
            if b.get("changes"):
                text += f"\nWhat changed: {b['changes']}"
            return text
        case "critique":
            return f"[{m.id}] {who}, critique:\n" + "\n".join(
                f"Of {c['of']}'s: strengths: {c['strengths']} Weaknesses: {c['weaknesses']}"
                f" Suggestions: {c['suggestions']}"
                for c in b["critiques"]
            )
        case "vote":
            return f"[{m.id}] {who} votes for {b['for']}'s proposal: {b['reason']}"
        case "question":
            return f"[{m.id}] {who} asks {m.to_role}: {b['question']}"
        case "answer":
            return f"[{m.id}] {who}, answering [{m.reply_to}]: {b['answer']}"
        case _:
            return f"[{m.id}] {who}: {b.get('text', '')}"
