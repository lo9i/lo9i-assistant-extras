"""SSH as the tests see it: a fake that answers each of herdr.py's scripts as a host would, and an SSH config
of their own (HOSTS_SSH_CONFIG, which `ssh -G` reads offline). Nothing here reaches a network."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from hosts import herdr
from hosts.ssh import Result

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


def process_info(pane: str = "w1:p1", busy: str | None = None) -> str:
    """`pane process-info`'s answer: the shell alone, or a program in the foreground."""
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
