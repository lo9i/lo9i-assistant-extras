"""SSH as the tests see it: a fake that answers each of herdr.py's scripts as a host would, a fake of the
tunnels' ssh processes, and an SSH config of their own (HOSTS_SSH_CONFIG, which `ssh -G` reads offline).
Nothing here reaches a network."""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from hosts import herdr
from hosts.ssh import Result
from hosts.tunnels import Tunnels

CONFIG = """\
# Two names for the Pi; the first is the one shown.
Host pi lo9i-pi
  HostName lo9i-pi.local
  User lo9i

Host nas
  HostName 192.168.0.10
  User groso
  Port 2222

Host *
  ServerAliveInterval 30
"""


def process_info(pane: str = "w1:p2", busy: str | None = None) -> str:
    """`pane process-info`'s answer for the assistant's pane: the shell alone, or a program in the foreground."""
    foreground = [{"pid": 10, "name": "bash", "cmdline": "/bin/bash"}]
    if busy:
        foreground = [{"pid": 11, "name": busy.split()[0], "cmdline": busy}]
    return json.dumps(
        {"result": {"process_info": {"pane_id": pane, "shell_pid": 10, "foreground_processes": foreground}}}
    )


def matched(text: str) -> str:
    """`pane wait-output`'s answer once the pattern matched."""
    return json.dumps({"result": {"type": "output_matched", "read": {"text": text}}})


@dataclass
class FakeSsh:
    """Answers by script; records every call as (host, script, args)."""

    answers: dict[str, Callable[..., Result]] = field(default_factory=dict)
    calls: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    async def run(self, host: str, script: str, *args: str, timeout: float = 30) -> Result:
        self.calls.append((host, script, args))
        answer = self.answers.get(script)
        if answer is None:
            raise AssertionError(f"Unexpected script:\n{script}")
        return answer(*args)

    def scripts(self) -> list[str]:
        return [script for _, script, _ in self.calls]


def answers(code: int = 0, out: str = "", err: str = "") -> Callable[..., Result]:
    return lambda *args: Result(code, out, err)


@pytest.fixture
def ssh() -> FakeSsh:
    return FakeSsh()


@pytest.fixture
def fake_herdr(ssh: FakeSsh) -> herdr.Herdr:
    return herdr.Herdr(ssh)


class FakeProcess:
    """A tunnel's ssh: writes `lines` to stderr, then exits with `code`, or runs until stopped (None)."""

    def __init__(self, lines: Sequence[str] = (), code: int | None = None) -> None:
        self.stderr = asyncio.StreamReader()
        for line in lines:
            self.stderr.feed_data(f"{line}\n".encode())
        self.returncode: int | None = None
        self.terminated = False
        self._exited = asyncio.Event()
        if code is not None:
            self.exit(code)

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.stderr.feed_eof()
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)


# What ssh -v says once a local or SOCKS tunnel is up, and a remote one.
UP = "debug1: Entering interactive session."
REMOTE_UP = "debug1: remote forward success for: listen 9000, connect localhost:3000"


@dataclass
class FakeSpawn:
    """Starts the next of `processes` for each try (ones that run until stopped once they're used up);
    records each command line. Waits between tries are recorded, not waited."""

    processes: list[Callable[[], FakeProcess]] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    started: list[FakeProcess] = field(default_factory=list)
    waits: list[float] = field(default_factory=list)

    async def __call__(self, args: Sequence[str]) -> FakeProcess:
        self.commands.append(list(args))
        process = self.processes.pop(0)() if self.processes else FakeProcess([UP])
        self.started.append(process)
        return process

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        await asyncio.sleep(0)

    def tunnels(self, settle_seconds: float = 30) -> Tunnels:
        return Tunnels(self, self.sleep, settle_seconds)


async def settled(check: Callable[[], bool], seconds: float = 2) -> None:
    """Lets the tunnels' tasks run until `check` holds."""
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.001)


@pytest.fixture
def spawn() -> FakeSpawn:
    return FakeSpawn()


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    """The tests' SSH config and database, and a home folder of their own."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    path = home / ".ssh" / "config"
    path.write_text(CONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HOSTS_SSH_CONFIG", str(path))
    monkeypatch.setenv("HOSTS_DB", str(tmp_path / "hosts.db"))
    return path
