"""Each connection's ssh config, written from the plugin's list for it alone and deleted when it ends, and a
host's password, which ssh gets from the askpass helper over a socket answered once: never on a command line,
in an environment or in a file. A fake `ssh` first on PATH says what it got and asks the helper as ssh would."""

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from hosts import connect, service
from hosts.details import Details
from hosts.ssh import OpenSsh

SECRET = "correct horse battery staple"

# Says its arguments, its config (and who may read it and its folder) and its environment's ssh variables, and
# asks the helper what ssh would: the password twice, then whether to trust a new host key. Without a terminal of
# its own, as the plugin's processes run: the helper can't ask anyone.
_FAKE_SSH = f"""#!{sys.executable}
import os, subprocess, sys
os.setsid()
args = sys.argv[1:]
config = args[args.index("-F") + 1]
print("args:", repr(args))
print("config:", repr(open(config).read()))
print("modes:", oct(os.stat(config).st_mode & 0o777), oct(os.stat(os.path.dirname(config)).st_mode & 0o777))
askpass = os.environ.get("SSH_ASKPASS")
if askpass:
    for prompt in ("lo9i@pi.local's password: ", "lo9i@pi.local's password: ", "Continue connecting (yes/no)? "):
        done = subprocess.run([askpass, prompt], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        print("answer:", repr((done.returncode, done.stdout)))
print("env:", repr({{k: v for k, v in os.environ.items() if k.startswith(("SSH_ASK", "HOSTS_ASK"))}}))
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    program = folder / "ssh"
    program.write_text(_FAKE_SSH)
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def _said(out: str, what: str) -> list[str]:
    return [line.removeprefix(f"{what}: ") for line in out.splitlines() if line.startswith(f"{what}: ")]


async def test_a_connection_gets_a_config_only_the_user_reads_gone_once_it_ends(config):
    await service.add_host("box", Details("box.example", "admin", 2200, "~/.ssh/box", [("ProxyJump", "pi")]))
    async with connect.session("box") as c:
        path = Path(c.options[1])
        assert c.options == ["-F", str(path)] and c.unattended == ["-o", "BatchMode=yes"] and c.env == {}
        assert path != config and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        text = path.read_text()
    assert not path.exists() and not path.parent.exists()
    assert "Host box\n  HostName box.example\n  User admin\n  Port 2200\n  IdentityFile ~/.ssh/box\n" in text
    assert "  ProxyJump pi\n" in text and "Host pi\n" in text  # Every host, for one it jumps through.


async def test_a_connection_ends_its_config_even_when_it_fails(config):
    with pytest.raises(RuntimeError):
        async with connect.session("pi") as c:
            path = Path(c.options[1])
            raise RuntimeError("ssh failed")
    assert not path.parent.exists()


async def test_a_host_with_a_password_gets_it_from_the_helper_once_and_nowhere_else(config, fake_ssh):
    await service.add_host("box", Details("box.example", "lo9i", 22, password=SECRET))
    result = await OpenSsh().run("box", "uptime")
    out = result.out
    assert _said(out, "answer") == [repr((0, SECRET + "\n")), repr((1, "")), repr((1, ""))]
    [args], [text], [env] = _said(out, "args"), _said(out, "config"), _said(out, "env")
    assert SECRET not in args + text + env
    assert "'BatchMode=no'" in args and "'NumberOfPasswordPrompts=1'" in args  # Asked once, by the helper.
    assert "'SSH_ASKPASS_REQUIRE': 'force'" in env and "hosts-askpass" in env
    assert _said(out, "modes") == ["0o600 0o700"]


async def test_a_host_without_a_password_has_no_helper_and_asks_nothing(config, fake_ssh):
    out = (await OpenSsh().run("pi", "uptime")).out
    assert _said(out, "answer") == [] and _said(out, "env") == ["{}"]
    assert "'BatchMode=yes'" in _said(out, "args")[0]


async def test_the_helper_never_answers_with_the_password_but_to_a_password_prompt(config):
    await service.add_host("box", Details("box.example", password=SECRET))

    async def ask(env: dict[str, str], prompt: str) -> tuple[int, str]:
        # Without a terminal, as ssh runs it from the plugin; the socket answers on this loop meanwhile.
        process = await asyncio.create_subprocess_exec(
            env["SSH_ASKPASS"], prompt, env=os.environ | env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )  # fmt: skip
        out, _ = await process.communicate()
        return process.returncode or 0, out.decode()

    async with connect.session("box") as c:
        assert await ask(c.env, "Enter passphrase for key '/x': ") == (1, "")
        assert await ask(c.env, "box's password: ") == (0, SECRET + "\n")
        assert await ask(c.env, "box's password: ") == (1, "")  # Once only.


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs ssh, which reads the config offline")
async def test_ssh_takes_the_config_written_for_a_connection(config):
    options = [("ServerAliveInterval", "30"), ("SendEnv", "LANG LC_*"), ("ProxyJump", "pi")]
    await service.add_host("box", Details("box.example", "admin", 2200, "~/.ssh/my keys/box", options, SECRET))
    async with connect.session("box") as c:
        process = await asyncio.create_subprocess_exec(
            "ssh", *c.options, "-G", "--", "box", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await process.communicate()
    assert process.returncode == 0, err.decode()
    said = out.decode().splitlines()
    for line in ("hostname box.example", "user admin", "port 2200", "serveraliveinterval 30", "proxyjump pi"):
        assert line in said
    assert any(line.startswith("identityfile ") and line.endswith("/.ssh/my keys/box") for line in said)
    assert "numberofpasswordprompts 1" in said
