"""How full Copilot's context is once a task ended. The CLI writes its usage to --usage-output-file when it's done:
`lastCallInputTokens`, what its last model call took in, is how full the context was then, and `currentModel` the
model. The window is that model's, as Copilot's model list says (`capabilities.limits`), read with the plugin's token
as the CLI reads it: GitHub names the account's Copilot API, which lists the models.

Written from the CLI's code (1.0.95) and not yet tried against a Copilot account: when anything doesn't answer as
expected the task has no context, and goes on as before."""

import json
import sys
from pathlib import Path
from typing import Any

import httpx

from copilot_tasks.task_folder import Context

_USER_URL = "https://api.github.com/copilot_internal/user"
_DEFAULT_API = "https://api.githubcopilot.com"
_HEADERS = {"Copilot-Integration-Id": "copilot-developer-cli"}
_TIMEOUT = 20


async def context(usage_file: Path, token: str, transport: httpx.AsyncBaseTransport | None = None) -> Context | None:
    """None when the CLI wrote no usage, or the model's window can't be learned."""
    usage = _read(usage_file)
    tokens, model = usage.get("lastCallInputTokens"), _model(usage)
    if not isinstance(tokens, int) or tokens <= 0 or not model:
        return None
    try:
        window = await context_window(model, token, transport)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        print(f"Couldn't read the context window of {model}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    return Context(tokens=tokens, window=window) if window else None


async def context_window(model: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> int | None:
    """How many tokens `model` takes in, as Copilot's model list says; None when it doesn't list it."""
    headers = {"Authorization": f"Bearer {token}", **_HEADERS}
    async with httpx.AsyncClient(transport=transport, headers=headers, timeout=_TIMEOUT) as http:
        models = await _models(http, await _api(http))
    found = next((m for m in models if m.get("id") == model), {})
    limits = (found.get("capabilities") or {}).get("limits") or {}
    window = limits.get("max_context_window_tokens") or limits.get("max_prompt_tokens")
    return int(window) if window else None


def _read(usage_file: Path) -> dict[str, Any]:
    try:
        data = json.loads(usage_file.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _model(usage: dict[str, Any]) -> str:
    """The model it ended on; else the last one it used."""
    metrics = usage.get("modelMetrics")
    used = list(metrics) if isinstance(metrics, dict) else []
    return str(usage.get("currentModel") or (used[-1] if used else ""))


async def _api(http: httpx.AsyncClient) -> str:
    """The account's Copilot API (individual, business or enterprise)."""
    response = await http.get(_USER_URL)
    response.raise_for_status()
    return str((response.json().get("endpoints") or {}).get("api") or _DEFAULT_API).rstrip("/")


async def _models(http: httpx.AsyncClient, api: str) -> list[dict[str, Any]]:
    response = await http.get(f"{api}/models")
    response.raise_for_status()
    return [m for m in response.json().get("data", []) if isinstance(m, dict)]
