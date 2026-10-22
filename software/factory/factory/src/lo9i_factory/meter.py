"""The cost meter: every call the team makes goes through `call`, which runs
it on the role's model, records what it cost, and keeps the task within its
limit (SPEC §1: the task pauses when the limit is reached).

- A call starts only while the task has spent less than max_usd. One that
  would start past it pauses the task instead (status paused, with the
  reason), and raises LimitReachedError for the runner to stop at. The user
  raises the limit and resumes it (service.resume).
- A call that has started is recorded however it ends, refusals included:
  the provider charged for it. So the last call can take a task a little
  past its limit, never more than that one call.
- Direct API calls cost USD at the model's price (prices.py); a model
  without one isn't called. Copilot calls cost a request each.
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import prices, repo, service, settings
from .errors import ConflictError
from .providers import ProviderError, Providers, Reply, Request


class LimitReachedError(ConflictError):
    """The task reached max_usd and is paused."""


@dataclass(frozen=True)
class Answer:
    text: str
    # The reply parsed, for a request with a schema.
    data: Any
    # provider:model that answered.
    model: str
    usd: float
    requests: int
    # Its row in costs, to link to the message the runner saves.
    cost_id: int


def left(conn: sqlite3.Connection, task_id: int, s: settings.Settings) -> float:
    """What the task may still spend. When nothing, it's paused and
    LimitReachedError is raised."""
    spent = repo.spent_usd(conn, task_id)
    if spent >= s.max_usd:
        service.pause(conn, task_id, f"reached max_usd: spent ${spent:.2f} of ${s.max_usd:.2f}")
        raise LimitReachedError(f"task {task_id} has spent ${spent:.2f} of max_usd ${s.max_usd:.2f}; it's paused")
    return s.max_usd - spent


async def call(
    conn: sqlite3.Connection, providers: Providers, task_id: int, stage_id: int | None, role: str, request: Request
) -> Answer:
    """Ask `role`'s model. Raises LimitReachedError when the task is out of
    money, ProviderError when the model can't or won't answer."""
    s = settings.load(conn)
    model = s.models[role]
    provider, _, name = model.partition(":")
    # Before spending anything: a model that can't be counted isn't used.
    price = None if provider == "copilot" else prices.price(model, s)
    left(conn, task_id, s)

    reply = await providers.get(provider).complete(name, request)

    served = f"{provider}:{reply.model}"
    if price is not None and reply.model != name:
        # A refusal fallback answered: priced as that model when known.
        price = prices.find(served, s) or price
    u = reply.usage
    usd = (
        prices.usd(price, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) if price else 0.0
    )
    requests = 1 if provider == "copilot" else 0
    with conn:
        cost_id = repo.insert_cost(
            conn,
            task_id=task_id,
            stage_id=stage_id,
            role=role,
            provider=provider,
            model=reply.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
            usd=usd,
            requests=requests,
        )
    return Answer(reply.text, _data(served, reply, request), served, usd, requests, cost_id)


def _data(model: str, reply: Reply, request: Request) -> Any:
    """The reply's JSON, for a request with a schema. Raises ProviderError for
    one that didn't finish."""
    if reply.stop == "refusal":
        raise ProviderError(f"{model} declined to answer" + (f" ({reply.detail})" if reply.detail else ""))
    if reply.stop == "max_tokens":
        raise ProviderError(f"{model}'s reply was cut at {request.max_tokens} tokens")
    if request.schema is None:
        return None
    try:
        return json.loads(reply.text)
    except ValueError:
        raise ProviderError(f"{model} didn't answer in the JSON asked for") from None
