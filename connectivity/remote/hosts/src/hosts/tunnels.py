"""SSH tunnels: ports forwarded through a host by ssh on this computer (the Mac lo9i runs on), with the
user's SSH config, as `ssh -N` with -L, -R or -D. The web page (web/app.py) runs them, so they stop with it;
the assistant's tools (mcp_server.py) start and stop them through it.

- local (-L): a port on this Mac (127.0.0.1 only) reaches target_host:target_port as the host sees it.
- remote (-R): a port on the host reaches target_host:target_port as this Mac sees it.
- socks (-D): a SOCKS proxy on this Mac (127.0.0.1 only) whose connections leave from the host.

A tunnel runs only once the user switches it on, and goes on running until they switch it off: when its
connection drops, ssh starts again, waiting longer after each failure in a row. A problem trying again won't
fix (a key the host doesn't accept, a port in use) ends it as failed instead, with what to do in plain words.
"""

import asyncio
import contextlib
import ipaddress
import os
import re
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Protocol

from .ssh import lasting, problem
from .sshconfig import ssh_options

KINDS = {"local": "Local", "remote": "Remote", "socks": "SOCKS"}
# Where local and SOCKS tunnels listen: only programs on this Mac reach them.
LOOPBACK = "127.0.0.1"
# ssh notices a dead connection within 45 seconds, and gives up connecting after 10. It runs with no
# terminal, so it fails instead of asking for a password; nor does it share a connection the user's config
# may keep open (ControlMaster), which would end it at once.
_OPTIONS = (
    "-v", "-N", "-T",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=10",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "RemoteCommand=none",
    "-o", "PermitLocalCommand=no",
)  # fmt: skip
# Seconds before trying again after each failure in a row, the last one repeated.
RETRY_SECONDS = (1, 2, 5, 10, 30, 60)
# Up this long, a tunnel that drops starts again from the first wait.
_STEADY_SECONDS = 60
# What ssh -v says once the tunnel is up; if it never says, being still connected after this long counts.
_UP = "entering interactive session"
_REMOTE_UP = "remote forward success"
_SETTLE_SECONDS = 20
_STOP_SECONDS = 3
# The lines of ssh's own (not debug) messages kept to say why it stopped.
_KEPT_LINES = 20
_NAME = re.compile(r"^[\w .:@+-]{1,40}$")
# A host name or an IPv4 address (an IPv6 one is checked as such), as the far end of a forward. Never an ssh
# option.
_TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}$")


class InvalidTunnelError(Exception):
    """What's wrong with a tunnel's fields, by field."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


@dataclass(frozen=True)
class Tunnel:
    id: int | None
    host: str
    name: str
    kind: str
    listen_port: int
    target_host: str = ""
    target_port: int | None = None

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]

    def describe(self) -> str:
        """Where it goes, in a line: `localhost:8080 → db:5432 on pi`."""
        target = f"{self.target_host}:{self.target_port}"
        if self.kind == "local":
            return f"localhost:{self.listen_port} → {target} on {self.host}"
        if self.kind == "remote":
            return f"{self.host}:{self.listen_port} → {target} on this Mac"
        return f"SOCKS proxy at localhost:{self.listen_port}, through {self.host}"


@dataclass(frozen=True)
class Status:
    # stopped, starting, running, reconnecting or failed.
    state: str
    message: str = ""

    @property
    def on(self) -> bool:
        return self.state in ("starting", "running", "reconnecting")


STOPPED = Status("stopped")


# Local and SOCKS tunnels listen on 127.0.0.1 of this computer, where only an administrator may take a port
# under this one (macOS allows them on every address at once, not on one).
_ON_THIS_MAC = ("local", "socks")
_FIRST_OPEN_PORT = 1024
_PRIVILEGED = "Ports under 1024 need an administrator on this Mac. Choose one from 1024 up, like 13306."
# What ssh says when it isn't allowed to listen: "bind [127.0.0.1]:1000: Permission denied".
_BIND_REFUSED = re.compile(r"bind \[[^\]]*\]:\d+: permission denied")


def parse(
    host: str, name: str, kind: str, listen_port: Any, target_host: str = "", target_port: Any = None,
    id: int | None = None,
) -> Tunnel:  # fmt: skip
    """A tunnel from what the user typed. Raises InvalidTunnelError saying what to change."""
    errors: dict[str, str] = {}
    name = (name or "").strip()
    if not name:
        errors["name"] = "Give it a name."
    elif not _NAME.match(name):
        errors["name"] = "Up to 40 letters, digits, spaces and . : @ + - _"
    if kind not in KINDS:
        errors["kind"] = "Choose local, remote or SOCKS."
    listen = _port(listen_port, "listen_port", errors)
    if kind in _ON_THIS_MAC and listen is not None and listen < _FIRST_OPEN_PORT and "listen_port" not in errors:
        errors["listen_port"] = _PRIVILEGED
    target, to = "", None
    if kind in ("local", "remote"):
        target = (target_host or "").strip() or "localhost"
        if not (_TARGET.match(target) or _ipv6(target)):
            errors["target_host"] = "A host name or an IP address."
        to = _port(target_port, "target_port", errors)
    if errors:
        raise InvalidTunnelError(errors)
    assert listen is not None
    return Tunnel(id, host, name, kind, listen, target, to)


def _ipv6(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv6Address)
    except ValueError:
        return False


def _port(value: Any, field: str, errors: dict[str, str]) -> int | None:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        errors[field] = "A port from 1 to 65535."
        return None
    return port


def forward(tunnel: Tunnel) -> list[str]:
    """ssh's option for the tunnel: -L, -R or -D with its ports."""
    target = f"[{tunnel.target_host}]" if ":" in tunnel.target_host else tunnel.target_host
    if tunnel.kind == "local":
        return ["-L", f"{LOOPBACK}:{tunnel.listen_port}:{target}:{tunnel.target_port}"]
    if tunnel.kind == "remote":
        return ["-R", f"{tunnel.listen_port}:{target}:{tunnel.target_port}"]
    return ["-D", f"{LOOPBACK}:{tunnel.listen_port}"]


def command(tunnel: Tunnel) -> list[str]:
    """The whole ssh command line, to the host by its name in the SSH config."""
    return ["ssh", *ssh_options(), *_OPTIONS, *forward(tunnel), "--", tunnel.host]


def explain(tunnel: Tunnel, err: str, code: int) -> tuple[str, bool]:
    """Why ssh stopped, in plain words, and whether trying again won't help until the user does something.
    `err` is what ssh wrote, its debug lines left out."""
    lowered = err.lower()
    if _BIND_REFUSED.search(lowered):
        return _PRIVILEGED, True
    if "address already in use" in lowered or "could not request local forwarding" in lowered:
        return f"Port {tunnel.listen_port} is already in use on this Mac. Choose another one.", True
    if "remote port forwarding failed" in lowered:
        return f"{tunnel.host} wouldn't open port {tunnel.listen_port}: it may be in use there, or not allowed.", True
    if "privileged port" in lowered:
        return _PRIVILEGED, True
    if "bad " in lowered and "specification" in lowered:
        return "ssh didn't accept this tunnel's ports or address.", True
    if "bad configuration option" in lowered or "terminating, " in lowered:
        return "ssh stopped at an error in your SSH config (~/.ssh/config).", True
    message = f"Can't reach {tunnel.host}: {problem(err, f'ssh stopped (exit code {code}).')}"
    if "permission denied" in lowered:
        message += " A tunnel can't ask for a passphrase: add the key to the SSH agent (ssh-add)."
    return message, lasting(err)


class Process(Protocol):
    """What the tunnels need of a running ssh (asyncio's Process; the tests' fake)."""

    stderr: asyncio.StreamReader | None
    returncode: int | None

    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


async def spawn(args: Sequence[str]) -> Process:
    """ssh in the background, in the page's process group, so whatever stops the page stops it too."""
    return await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        env=_environment(),
    )  # fmt: skip


def _environment() -> dict[str, str]:
    """lo9i starts the page with few variables, without the SSH agent's: launchd has it on a Mac."""
    env = dict(os.environ)
    if "SSH_AUTH_SOCK" not in env and (agent := _launchd_agent()):
        env["SSH_AUTH_SOCK"] = agent
    return env


@cache
def _launchd_agent() -> str:
    if sys.platform != "darwin":
        return ""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        found = subprocess.run(["launchctl", "getenv", "SSH_AUTH_SOCK"], capture_output=True, text=True, timeout=5)
        return found.stdout.strip()
    return ""


class Tunnels:
    """The tunnels switched on, each kept running by a task of its own, and how each one is doing."""

    def __init__(
        self,
        spawn: Callable[[Sequence[str]], Awaitable[Process]] = spawn,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        settle_seconds: float = _SETTLE_SECONDS,
    ) -> None:
        """Tests pass a fake ssh, and a sleep that doesn't wait."""
        self._spawn = spawn
        self._sleep = sleep
        self._settle = settle_seconds
        self._tasks: dict[int, asyncio.Task] = {}
        self._status: dict[int, Status] = {}

    def status(self, tunnel_id: int) -> Status:
        return self._status.get(tunnel_id, STOPPED)

    async def start(self, tunnel: Tunnel) -> Status:
        """Switches it on; one already on goes on as it is."""
        assert tunnel.id is not None
        if tunnel.id not in self._tasks:
            self._status[tunnel.id] = Status("starting")
            task = asyncio.create_task(self._keep_up(tunnel), name=f"tunnel {tunnel.id}")
            self._tasks[tunnel.id] = task
            task.add_done_callback(lambda done: self._ended(tunnel.id, done))
        return self.status(tunnel.id)

    async def stop(self, tunnel_id: int) -> None:
        """Switches it off: ssh stops, and it's stopped, failed or not."""
        task = self._tasks.pop(tunnel_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._status.pop(tunnel_id, None)

    async def restart(self, tunnel: Tunnel) -> None:
        """Runs it again as changed, if it's on. Why it failed, if it did, was about how it was."""
        assert tunnel.id is not None
        if self.status(tunnel.id).on:
            await self.stop(tunnel.id)
            await self.start(tunnel)
        else:
            self._status.pop(tunnel.id, None)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(i) for i in list(self._tasks)))

    def _ended(self, tunnel_id: int, task: asyncio.Task) -> None:
        if self._tasks.get(tunnel_id) is task:
            del self._tasks[tunnel_id]

    async def _keep_up(self, tunnel: Tunnel) -> None:
        assert tunnel.id is not None
        failures = 0
        while True:
            try:
                up_for, err, code = await self._attempt(tunnel)
            except OSError as e:
                self._status[tunnel.id] = Status("failed", f"This Mac couldn't start ssh: {e.strerror or e}.")
                return
            message, final = explain(tunnel, err, code)
            if final:
                self._status[tunnel.id] = Status("failed", message)
                return
            if up_for is not None:
                message = f"The connection to {tunnel.host} dropped."
                if up_for >= _STEADY_SECONDS:
                    failures = 0
            wait = RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)]
            failures += 1
            self._status[tunnel.id] = Status("reconnecting", f"{message} Trying again in {wait} s.")
            await self._sleep(wait)
            self._status[tunnel.id] = Status("reconnecting", f"{message} Trying again…")

    async def _attempt(self, tunnel: Tunnel) -> tuple[float | None, str, int]:
        """Runs ssh until it exits: how long the tunnel was up (None if it never came up), what ssh said,
        its debug lines left out, and its exit code."""
        assert tunnel.id is not None
        loop = asyncio.get_running_loop()
        process = await self._spawn(command(tunnel))
        said: deque[str] = deque(maxlen=_KEPT_LINES)
        up_at: float | None = None

        def came_up() -> None:
            nonlocal up_at
            if up_at is None:
                up_at = loop.time()
                self._status[tunnel.id] = Status("running")

        marker = _REMOTE_UP if tunnel.kind == "remote" else _UP
        settle = loop.call_later(self._settle, came_up)
        try:
            assert process.stderr is not None
            while raw := await process.stderr.readline():
                line = raw.decode(errors="replace").strip()
                if marker in line.lower():
                    came_up()
                elif line and not line.startswith("debug"):
                    said.append(line)
            code = await process.wait()
        finally:
            settle.cancel()
            await _end(process)
        return (None if up_at is None else loop.time() - up_at), "\n".join(said), code


async def _end(process: Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), _STOP_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
