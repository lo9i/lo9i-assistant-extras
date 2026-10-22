"""Anthropic's Messages API, with the official SDK.

- The prompt cache is on (automatic caching): a debate sends the same
  system prompt and a growing thread again and again, and a cached prefix
  costs a tenth or less.
- Thinking is left to each model's default. The newest models think
  adaptively unless told otherwise; older ones that a user may pick reject
  some settings, so nothing model-specific is sent but the fallback below.
- On the models that have it, a refusal is retried server-side on the model
  Anthropic recommends for its category (`fallbacks: "default"`); the reply
  then names that model, and is priced as it.
"""

import anthropic
from anthropic import AsyncAnthropic

from . import ProviderError, Reply, Request, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The models that take server-side fallbacks.
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
_STOPS = {"max_tokens": "max_tokens", "refusal": "refusal"}


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
        *,
        base_url: str | None = None,
        label: str = "Anthropic",
    ) -> None:
        """`http_client` replaces the network in tests. `base_url` and `label`
        are for another server that speaks the Messages API: GitHub Copilot
        does, for its Claude models (copilot.py)."""
        self.client = AsyncAnthropic(api_key=api_key, http_client=http_client, base_url=base_url)
        self.label = label

    async def complete(self, model: str, request: Request) -> Reply:
        params: dict = {
            "model": model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": t.role, "content": t.text} for t in request.turns],
            "cache_control": {"type": "ephemeral"},
        }
        if request.schema is not None:
            params["output_config"] = {"format": {"type": "json_schema", "schema": request.schema}}
        try:
            if model in FALLBACK_MODELS:
                message = await self.client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                message = await self.client.messages.create(**params)
        except anthropic.AuthenticationError:
            key = "ANTHROPIC_API_KEY" if self.label == "Anthropic" else "its key"
            raise ProviderError(f"{self.label} refused {key}: it's wrong or was revoked") from None
        except anthropic.NotFoundError:
            raise ProviderError(f"{self.label} has no model {model}") from None
        except anthropic.APIStatusError as e:
            raise ProviderError(f"{self.label} answered {e.status_code} for {model}: {e.message}") from None
        except anthropic.APIConnectionError:
            raise ProviderError(f"couldn't reach {self.label}") from None
        u = message.usage
        details = getattr(message, "stop_details", None)
        return Reply(
            text="".join(b.text for b in message.content if b.type == "text"),
            model=message.model,
            usage=Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_tokens=u.cache_read_input_tokens or 0,
                cache_write_tokens=u.cache_creation_input_tokens or 0,
            ),
            stop=_STOPS.get(message.stop_reason or "", "end"),
            detail=(getattr(details, "category", None) or "") if details else "",
        )
