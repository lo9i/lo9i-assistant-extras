"""How a request to the daemon fails."""

import contextlib
from collections.abc import Iterator

import httpx


class DaemonError(Exception):
    """The daemon refused a request or couldn't be reached. The text says why."""


class DaemonUnavailableError(DaemonError):
    """The daemon couldn't be reached or failed on its side; trying again later can work."""


class RefusedError(DaemonError):
    """The daemon answered with a client error (4xx): the same request won't work later either."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


@contextlib.contextmanager
def daemon_errors() -> Iterator[None]:
    """Turns httpx's errors into DaemonError, so callers handle one kind of failure."""
    try:
        yield
    except httpx.HTTPStatusError as e:
        raise _status_error(e.response) from e
    except httpx.TransportError as e:
        raise DaemonUnavailableError(f"The assistant isn't reachable: {e}") from e


async def raise_for_status(response: httpx.Response) -> None:
    """Like `response.raise_for_status`, reading a streamed body first so its detail is known."""
    if response.is_success:
        return
    await response.aread()
    raise _status_error(response)


def _status_error(response: httpx.Response) -> DaemonError:
    detail = _detail(response)
    if response.is_client_error:
        return RefusedError(response.status_code, detail)
    return DaemonUnavailableError(f"The assistant failed: {detail}")


def _detail(response: httpx.Response) -> str:
    """FastAPI puts the reason in `detail`; anything else is shown as it came."""
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail else response.text
