"""How full Copilot's context is once a task ended: the usage the CLI writes, against the model's window from
Copilot's model list."""

import json

import httpx

from copilot_tasks import usage
from copilot_tasks.task_folder import Context

_USAGE = {"currentModel": "gpt-5.6-sol", "lastCallInputTokens": 41250, "modelMetrics": {"gpt-5.6-sol": {}}}


def _copilot(
    window: int | None = 272000, api: str = "https://api.business.githubcopilot.com", seen: list | None = None
) -> httpx.MockTransport:
    """Copilot, with the account's API on its business host; `seen` gets each request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url == "https://api.github.com/copilot_internal/user":
            return httpx.Response(200, json={"endpoints": {"api": api}})
        assert request.url == f"{api}/models"
        limits = {"max_context_window_tokens": window} if window else {}
        return httpx.Response(200, json={"data": [{"id": "gpt-5.6-sol", "capabilities": {"limits": limits}}]})

    return httpx.MockTransport(handler)


async def test_the_last_calls_input_is_measured_against_the_models_window(tmp_path):
    (tmp_path / "usage.json").write_text(json.dumps(_USAGE))
    seen: list[httpx.Request] = []
    assert await usage.context(tmp_path / "usage.json", "github_pat_x", _copilot(seen=seen)) == Context(41250, 272000)
    assert len(seen) == 2 and all(r.headers["Authorization"] == "Bearer github_pat_x" for r in seen)


async def test_the_model_is_the_last_one_used_when_it_names_none(tmp_path):
    (tmp_path / "usage.json").write_text(json.dumps({"lastCallInputTokens": 9, "modelMetrics": {"gpt-5.6-sol": {}}}))
    assert await usage.context(tmp_path / "usage.json", "t", _copilot()) == Context(9, 272000)


async def test_without_usage_or_a_window_there_is_no_context(tmp_path):
    assert await usage.context(tmp_path / "missing.json", "t", _copilot()) is None
    (tmp_path / "usage.json").write_text(json.dumps(_USAGE))
    assert await usage.context(tmp_path / "usage.json", "t", _copilot(window=None)) is None
    refused = httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "Bad credentials"}))
    assert await usage.context(tmp_path / "usage.json", "t", refused) is None
