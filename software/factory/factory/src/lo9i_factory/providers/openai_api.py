"""OpenAI's Chat Completions and Responses APIs, with the official SDK.
OpenAI's own models take either; GitHub Copilot serves some of its models
on one and some on the other (copilot.py)."""

from collections.abc import Awaitable, Callable
from typing import Any

import openai
from openai import AsyncOpenAI

from . import ProviderError, Reply, Request, Usage

_STOPS = {"length": "max_tokens", "content_filter": "refusal"}


class ChatProvider:
    """A provider over an OpenAI-compatible chat endpoint. `label` names it
    in errors."""

    def __init__(self, client: AsyncOpenAI, label: str) -> None:
        self.client = client
        self.label = label

    async def complete(self, model: str, request: Request) -> Reply:
        params: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": t.role, "content": t.text} for t in request.turns),
            ],
            "max_completion_tokens": request.max_tokens,
        }
        if request.schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": request.schema, "strict": True},
            }
        completion = await _calling(self.label, model, lambda: self.client.chat.completions.create(**params))
        if not completion.choices:
            raise ProviderError(f"{self.label} answered {model} with no reply")
        choice = completion.choices[0]
        u = completion.usage
        cached = (u.prompt_tokens_details.cached_tokens or 0) if u and u.prompt_tokens_details else 0
        refusal = choice.message.refusal or ""
        return Reply(
            text=choice.message.content or "",
            model=completion.model or model,
            usage=Usage(
                input_tokens=(u.prompt_tokens - cached) if u else 0,
                output_tokens=u.completion_tokens if u else 0,
                cache_read_tokens=cached,
            ),
            stop="refusal" if refusal else _STOPS.get(choice.finish_reason or "", "end"),
            detail=refusal,
        )


class ResponsesProvider:
    """A provider over the Responses API: Copilot serves its GPT models there
    only. `label` names it in errors."""

    def __init__(self, client: AsyncOpenAI, label: str) -> None:
        self.client = client
        self.label = label

    async def complete(self, model: str, request: Request) -> Reply:
        params: dict = {
            "model": model,
            "instructions": request.system,
            "input": [{"role": t.role, "content": t.text} for t in request.turns],
            "max_output_tokens": request.max_tokens,
            "store": False,
        }
        if request.schema is not None:
            params["text"] = {
                "format": {"type": "json_schema", "name": "reply", "schema": request.schema, "strict": True}
            }
        response = await _calling(self.label, model, lambda: self.client.responses.create(**params))
        refusals = [
            part.refusal
            for item in response.output
            if item.type == "message"
            for part in item.content
            if part.type == "refusal"
        ]
        details = response.incomplete_details
        cut = response.status == "incomplete" and details is not None and details.reason == "max_output_tokens"
        u = response.usage
        cached = (u.input_tokens_details.cached_tokens or 0) if u and u.input_tokens_details else 0
        return Reply(
            text=response.output_text or "",
            model=response.model or model,
            usage=Usage(
                input_tokens=(u.input_tokens - cached) if u else 0,
                output_tokens=u.output_tokens if u else 0,
                cache_read_tokens=cached,
            ),
            stop="refusal" if refusals else "max_tokens" if cut else "end",
            detail=refusals[0] if refusals else "",
        )


async def _calling(label: str, model: str, call: Callable[[], Awaitable[Any]]) -> Any:
    """The SDK's errors as ProviderError, saying what went wrong."""
    try:
        return await call()
    except openai.AuthenticationError:
        raise ProviderError(f"{label} refused its key: it's wrong or was revoked") from None
    except openai.NotFoundError:
        raise ProviderError(f"{label} has no model {model}") from None
    except openai.APIStatusError as e:
        raise ProviderError(f"{label} answered {e.status_code} for {model}: {e.message}") from None
    except openai.APIConnectionError:
        raise ProviderError(f"couldn't reach {label}") from None


def openai_provider(api_key: str, http_client: openai.DefaultAsyncHttpxClient | None = None) -> ChatProvider:
    """`http_client` replaces the network in tests."""
    return ChatProvider(AsyncOpenAI(api_key=api_key, http_client=http_client), "OpenAI")
