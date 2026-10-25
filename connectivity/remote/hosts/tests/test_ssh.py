"""The ssh runner, with a fake `ssh` first on PATH that says what it was given."""

import os
import shlex
import stat
from pathlib import Path

import pytest

from hosts import ssh
from hosts.ssh import TIMED_OUT, OpenSsh

_FAKE_SSH = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done
echo "said on stderr" >&2
[ -n "$FAKE_SSH_SLEEP" ] && sleep "$FAKE_SSH_SLEEP"
exit "${FAKE_SSH_EXIT:-0}"
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    program = folder / "ssh"
    program.write_text(_FAKE_SSH)
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


async def test_a_script_runs_with_sh_on_the_host_never_asking_for_a_password(fake_ssh, config):
    result = await OpenSsh().run("pi", "echo $1", "a b; rm -rf ~")
    args = result.out.splitlines()
    assert args[0] == "-F" and args[1] != str(config)  # Its own config, from the list, never the user's.
    assert not Path(args[1]).exists()  # Gone once ssh is.
    assert "BatchMode=yes" in args and "-T" in args
    host_at = args.index("--") + 1
    assert args[host_at] == "pi"
    assert shlex.split(args[host_at + 1]) == ["sh", "-c", "echo $1", "sh", "a b; rm -rf ~"]
    assert (result.code, result.err) == (0, "said on stderr\n")


async def test_the_exit_code_comes_back(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    assert (await OpenSsh().run("pi", "true")).code == 255


async def test_a_command_that_takes_too_long_is_stopped(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_SLEEP", "5")
    assert (await OpenSsh().run("pi", "true", timeout=0.3)).code == TIMED_OUT


def test_the_terminal_gets_a_terminal_and_notices_a_dead_network():
    command = ssh.terminal_command("pi", "exec herdr", ["-F", "/tmp/hosts-ssh-x/config"])
    assert command[:3] == ["ssh", "-F", "/tmp/hosts-ssh-x/config"]
    assert "-tt" in command and "ServerAliveInterval=10" in command
    assert command[-2:] == ["pi", "sh -c 'exec herdr' sh"]
    assert command[-3] == "--"


@pytest.mark.parametrize(
    ("err", "said", "lasting"),
    [
        ("ssh: Could not resolve hostname x.local: nodename nor servname provided", "address couldn't be found", False),
        ("ssh: connect to host 10.0.0.9 port 22: Operation timed out", "didn't answer", False),
        ("ssh: connect to host x port 22: Connection refused", "refused the connection", False),
        ("lo9i@x: Permission denied (publickey).", "didn't accept this computer's SSH key", True),
        ("Host key verification failed.", "host key", True),
        (
            "Something odd\nkex_exchange_identification: read: weird\n",
            "kex_exchange_identification: read: weird",
            False,
        ),
        ("", "the connection failed.", False),
    ],
)
def test_ssh_failures_in_plain_words(err, said, lasting):
    assert said in ssh.problem(err)
    assert ssh.lasting(err) is lasting
