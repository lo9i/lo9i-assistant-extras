"""The cost meter, over providers that answer what each test says."""

import asyncio
import json

import pytest

from lo9i_factory import meter, service, settings
from lo9i_factory.errors import ConflictError, ValidationError
from lo9i_factory.providers import ProviderError, Providers, Reply, Request, Turn, Usage

REQUEST = Request("You are the architect.", [Turn("user", "Add a cache")])
# On claude-opus-5-5 ($4 in, $20 out, $0.20 cache read, $5 cache write per
# million): 0.004 + 0.010 + 0.002 + 0.010.
USAGE = Usage(input_tokens=1000, output_tokens=500, cache_read_tokens=10_000, cache_write_tokens=2000)
COST = 0.026


class Fake:
    """A provider that answers with the replies given, in order."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    async def complete(self, model: str, request: Request) -> Reply:
        self.calls.append(model)
        return self.replies.pop(0)


def reply(model="claude-opus-5-5", **kw) -> Reply:
    return Reply(**{"text": "Use an LRU", "model": model, "usage": USAGE, **kw})


@pytest.fixture
def task(conn, git_repo):
    return service.create_task(conn, str(git_repo), "Add a cache")


def call(conn, task, role="architect", request=REQUEST, **clients):
    stage = service.stages(conn, task.id)[0].id
    return asyncio.run(meter.call(conn, Providers(env={}, clients=clients), task.id, stage, role, request))


def costs(conn):
    return [dict(r) for r in conn.execute("SELECT role, provider, model, usd, requests FROM costs")]


def test_a_call_is_priced_and_recorded(conn, task):
    answer = call(conn, task, anthropic=Fake(reply()))

    assert (answer.text, answer.data, answer.model, answer.requests) == (
        "Use an LRU",
        None,
        "anthropic:claude-opus-5-5",
        0,
    )
    assert answer.usd == pytest.approx(COST)
    assert costs(conn) == [
        {"role": "architect", "provider": "anthropic", "model": "claude-opus-5-5", "usd": answer.usd, "requests": 0}
    ]
    row = conn.execute("SELECT * FROM costs WHERE id = ?", (answer.cost_id,)).fetchone()
    assert (row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (1000, 10_000, 2000)


def test_copilot_counts_requests_not_dollars(conn, task):
    settings.update(conn, {"models.reviewer": "copilot:claude-sonnet-5-5"})
    answer = call(conn, task, "reviewer", copilot=Fake(reply("claude-sonnet-5-5")))
    assert (answer.usd, answer.requests) == (0, 1)
    assert service.costs(conn, task.id)["requests"] == 1


def test_a_fallback_is_priced_as_the_model_that_answered(conn, task):
    answer = call(conn, task, anthropic=Fake(reply("claude-opus-4-8")))
    # claude-opus-4-8: $5 in, $25 out, $0.50 cache read, $6.25 cache write.
    assert answer.usd == pytest.approx(0.005 + 0.0125 + 0.005 + 0.0125)
    assert answer.model == "anthropic:claude-opus-4-8"


def test_a_model_without_a_price_isnt_called(conn, task):
    settings.update(conn, {"models.architect": "openai:gpt-9"})
    fake = Fake(reply("gpt-9"))
    with pytest.raises(ValidationError, match=r"no price for openai:gpt-9.*set prices.openai:gpt-9"):
        call(conn, task, openai=fake)
    assert fake.calls == [] and costs(conn) == []

    settings.update(conn, {"prices.openai:gpt-9": {"input": 2, "output": 8}})
    # Without cache prices, the cache counts at the input price: 2 + 4 + 20 + 4 thousandths.
    assert call(conn, task, openai=fake).usd == pytest.approx(0.030)


def test_the_task_pauses_at_its_limit_until_the_user_raises_it(conn, task):
    settings.update(conn, {"max_usd": 0.03})
    fake = Fake(reply(), reply(), reply())
    call(conn, task, anthropic=fake)
    # Under the limit when it starts, so it runs, and may end past it.
    call(conn, task, anthropic=fake)

    with pytest.raises(meter.LimitReachedError, match=r"spent \$0.05 of max_usd \$0.03"):
        call(conn, task, anthropic=fake)
    assert len(fake.calls) == 2
    paused = service.get_task(conn, task.id)
    assert (paused.status, paused.column) == ("paused", "waiting")
    assert paused.reason == "reached max_usd: spent $0.05 of $0.03"

    with pytest.raises(ConflictError, match="raise max_usd first"):
        service.resume(conn, task.id)
    settings.update(conn, {"max_usd": 1})
    resumed = service.resume(conn, task.id)
    assert (resumed.status, resumed.reason) == ("queued", "")
    with pytest.raises(ConflictError, match="isn't paused"):
        service.resume(conn, task.id)


def test_refusals_and_cut_replies_are_paid_for_and_raised(conn, task):
    fake = Fake(reply(stop="refusal", detail="cyber"), reply(stop="max_tokens"))
    with pytest.raises(ProviderError, match=r"anthropic:claude-opus-5-5 declined to answer \(cyber\)"):
        call(conn, task, anthropic=fake)
    with pytest.raises(ProviderError, match="cut at 16000 tokens"):
        call(conn, task, anthropic=fake)
    assert len(costs(conn)) == 2


def test_a_structured_reply_is_parsed(conn, task):
    request = Request("s", [Turn("user", "x")], schema={"type": "object"})
    fake = Fake(reply(text=json.dumps({"plan": "LRU"})), reply(text="Sure! Here's the plan"))
    assert call(conn, task, request=request, anthropic=fake).data == {"plan": "LRU"}
    with pytest.raises(ProviderError, match="didn't answer in the JSON asked for"):
        call(conn, task, request=request, anthropic=fake)


def test_costs_per_stage_and_role(conn, task):
    settings.update(conn, {"models.security": "copilot:gpt-5"})
    call(conn, task, anthropic=Fake(reply()))
    call(conn, task, "security", copilot=Fake(reply("gpt-5")))
    c = service.costs(conn, task.id)
    assert (c["usd"], c["requests"]) == (round(COST, 4), 1)
    assert c["by_stage"] == {"spec": {"usd": round(COST, 4), "requests": 1}}
    assert c["by_role"] == {"architect": {"usd": round(COST, 4), "requests": 0}, "security": {"usd": 0, "requests": 1}}
