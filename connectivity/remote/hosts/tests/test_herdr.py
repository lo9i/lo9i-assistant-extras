"""Herdr on a host. The scripts run for real, with `sh` on this computer standing in for the host, against a
fake `herdr` in ~/.local/bin that answers as the real one does (its JSON as read on herdr 0.9.5); the
answers to ssh's failures come from a fake ssh."""

import asyncio
import json
import os
import re
import stat
import sys

import pytest
from conftest import FakeSsh, answers, matched, process_info

from hosts import herdr
from hosts.herdr import BusyError, Herdr, HerdrError, NotInstalledError, Probe, UnreachableError
from hosts.ssh import Result

_FAKE_HERDR = r"""#!PYTHON
import json, os, re, subprocess, sys
from pathlib import Path

state = Path(os.environ["HOME"]) / "herdr-state"
state.mkdir(exist_ok=True)
args = sys.argv[1:]
with open(state / "log", "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["--session", "lo9i"]:
    args = args[2:]
screen = state / "screen"


def answer(result):
    print(json.dumps({"id": "cli", "result": result}))
    sys.exit(0)


def fail(code, message):
    print(json.dumps({"id": "cli", "error": {"code": code, "message": message}}))
    sys.exit(1)


if args == ["server"]:
    (state / "server").touch()
    sys.exit(0)
if args[:2] == ["session", "list"]:
    running = (state / "server").exists()
    answer_text = {"sessions": [{"name": "default", "running": False}, {"name": "lo9i", "running": running}]}
    print(json.dumps(answer_text))
    sys.exit(0)
if not (state / "server").exists():
    fail("server_not_running", "no herdr server is running")
if args[:2] == ["workspace", "list"]:
    answer({"type": "workspace_list", "workspaces": []})
if args[:2] == ["workspace", "create"]:
    (state / "pane").touch()
    answer({"type": "workspace_created", "root_pane": {"pane_id": "w1:p1", "focused": True}})
if args[:2] == ["pane", "current"]:
    if not (state / "pane").exists():
        fail("pane_not_found", "pane not found")
    answer({"type": "pane_current", "pane": {"pane_id": "w1:p1", "terminal_id": "term_1", "focused": True}})
# The user's tab, and the assistant's once it's made, as herdr 0.9 lists them: compact JSON, a pane's scroll an
# object of its own.
tabs = [{"agent_status": "unknown", "focused": True, "label": "1", "tab_id": "w1:t1", "workspace_id": "w1"}]
panes = [{"pane_id": "w1:p1", "scroll": {"offset_from_bottom": 0}, "tab_id": "w1:t1"}]
if (state / "tab").exists():
    tabs.append({"agent_status": "unknown", "focused": False, "label": "assistant", "tab_id": "w1:t2"})
    panes.append({"pane_id": "w1:p2", "scroll": {"offset_from_bottom": 0}, "tab_id": "w1:t2"})
if args[:2] == ["tab", "list"]:
    print(json.dumps({"id": "cli", "result": {"tabs": tabs, "type": "tab_list"}}, separators=(",", ":")))
    sys.exit(0)
if args[:2] == ["pane", "list"]:
    print(json.dumps({"id": "cli", "result": {"panes": panes, "type": "pane_list"}}, separators=(",", ":")))
    sys.exit(0)
if args[:2] == ["tab", "create"]:
    (state / "tab").write_text(json.dumps(args))
    answer({"type": "tab_created", "root_pane": {"pane_id": "w1:p2"}})
if args[:2] == ["pane", "process-info"]:
    pane = args[args.index("--pane") + 1]
    busy = (state / "busy").read_text() if (state / "busy").exists() else ""
    foreground = [{"pid": 11, "name": "vim", "cmdline": busy}] if busy else [{"pid": 10, "name": "bash"}]
    answer({"process_info": {"pane_id": pane, "shell_pid": 10, "foreground_processes": foreground}})
if args[:2] == ["pane", "run"]:
    line = args[3]
    ran = subprocess.run(["sh", "-c", line], capture_output=True, text=True)
    with open(screen, "a") as shown:
        shown.write("me@host:~$ " + line + "\n" + ran.stdout + ran.stderr)
    sys.exit(0)
if args[:2] == ["pane", "wait-output"]:
    pattern = args[args.index("--regex") + 1]
    text = screen.read_text() if screen.exists() else ""
    if any(re.match(pattern, line) for line in text.splitlines()):
        answer({"type": "output_matched", "read": {"text": text + "me@host:~$"}})
    print(json.dumps({"error": {"code": "timeout", "message": "timed out"}}), file=sys.stderr)
    sys.exit(1)
if args[:2] == ["pane", "read"]:
    print((screen.read_text() if screen.exists() else "") + "me@host:~$ ", end="")
    sys.exit(0)
fail("unknown", "unknown command " + " ".join(args))
"""


class LocalSsh:
    """Runs the script with `sh` here, as ssh would on the host, with a PATH that has no herdr of this
    computer's."""

    async def run(self, host: str, script: str, *args: str, timeout: float = 30) -> Result:
        process = await asyncio.create_subprocess_exec(
            "sh", "-c", script, "sh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=os.environ | {"PATH": "/usr/bin:/bin"},
        )  # fmt: skip
        out, err = await asyncio.wait_for(process.communicate(), timeout)
        return Result(process.returncode or 0, out.decode(), err.decode())


@pytest.fixture
def home(config):
    return config.parent.parent


@pytest.fixture
def installed(home):
    """A fake herdr in ~/.local/bin, which no PATH has but the scripts'."""
    folder = home / ".local" / "bin"
    folder.mkdir(parents=True)
    program = folder / "herdr"
    program.write_text(_FAKE_HERDR.replace("PYTHON", sys.executable))
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    return home / "herdr-state"


def _log(state) -> list[list[str]]:
    return [json.loads(line) for line in (state / "log").read_text().splitlines()]


# --- The scripts, run for real ---


async def test_a_command_runs_in_the_assistants_own_tab_opened_for_it(installed):
    run = await Herdr(LocalSsh()).run("pi", "echo hello && sh -c 'exit 3'", 10)
    assert (run.output, run.exit_code, run.truncated) == ("hello", 3, False)
    commands = [args[2:4] for args in _log(installed) if args[:2] == ["--session", "lo9i"]]
    assert ["server"] in [c[:1] for c in commands]
    assert ["workspace", "create"] in commands
    created = json.loads((installed / "tab").read_text())
    assert created == ["tab", "create", "--label", "assistant", "--cwd", str(installed.parent), "--env",
                           "SHELL=/bin/sh", "--no-focus"]  # fmt: skip
    ran = [args for args in _log(installed) if args[2:4] == ["pane", "run"]]
    assert [args[4] for args in ran] == ["w1:p2"]  # not the focused pane, w1:p1
    await Herdr(LocalSsh()).run("pi", "true", 10)
    assert sum(args[2:4] == ["tab", "create"] for args in _log(installed)) == 1  # found again, not made again


async def test_quotes_and_dollars_reach_the_shell_as_typed(installed, home):
    run = await Herdr(LocalSsh()).run("pi", """printf '%s\\n' "it's $HOME" 'a;b'""", 10)
    assert run.output.splitlines() == [f"it's {home}", "a;b"]
    assert run.exit_code == 0


async def test_a_command_over_several_lines_or_with_a_comment_still_ends(installed):
    run = await Herdr(LocalSsh()).run("pi", "echo one\necho two # a comment", 10)
    assert run.output.splitlines()[-2:] == ["one", "two"]
    assert run.exit_code == 0


async def test_a_busy_terminal_is_left_alone(installed):
    installed.mkdir()
    (installed / "server").touch()
    (installed / "pane").touch()
    (installed / "tab").touch()
    (installed / "busy").write_text("vim notes.txt")
    with pytest.raises(BusyError, match=re.escape("still running `vim notes.txt`")):
        await Herdr(LocalSsh()).run("pi", "rm -rf /tmp/x", 10)
    assert not any(args[2:4] == ["pane", "run"] for args in _log(installed))


async def test_the_screen_is_read_from_the_assistants_tab_or_the_users_pane(installed):
    lo9i = Herdr(LocalSsh())
    await lo9i.run("pi", "echo on screen", 10)
    assert "on screen" in await lo9i.screen("pi")
    await lo9i.screen("pi", 50)
    await lo9i.screen("pi", user=True)
    reads = [args for args in _log(installed) if args[2:4] == ["pane", "read"]]
    assert reads[-3][4:] == ["w1:p2", "--source", "visible"]
    assert reads[-2][4:] == ["w1:p2", "--source", "recent", "--lines", "50"]
    assert reads[-1][4:] == ["w1:p1", "--source", "visible"]


async def test_no_session_or_no_tab_yet_means_nothing_to_read(installed):
    with pytest.raises(HerdrError, match="has no terminal open"):
        await Herdr(LocalSsh()).screen("pi", user=True)
    (installed / "server").touch()
    with pytest.raises(HerdrError, match="has no assistant tab yet: run_command opens it"):
        await Herdr(LocalSsh()).screen("pi")


async def test_without_herdr_on_the_host_it_says_how_to_install_it(home):
    with pytest.raises(NotInstalledError, match="installs it from the Hosts page"):
        await Herdr(LocalSsh()).run("pi", "uptime", 10)
    assert await Herdr(LocalSsh()).probe("pi") == Probe(True, installed=False)


async def test_a_probe_sees_the_session(installed):
    lo9i = Herdr(LocalSsh())
    assert await lo9i.probe("pi") == Probe(True, installed=True, session=False)
    await lo9i.run("pi", "true", 10)
    assert await lo9i.probe("pi") == Probe(True, installed=True, session=True)


# --- What ssh and herdr answer, faked ---


async def test_a_host_that_doesnt_answer_says_why_in_plain_words(ssh: FakeSsh, fake_herdr):
    ssh.answers[herdr._PROBE] = answers(255, err="ssh: connect to host x port 22: Operation timed out\n")
    probe = await fake_herdr.probe("pi")
    assert not probe.reachable
    assert probe.problem == "Can't reach pi: it didn't answer. It may be off, asleep or on another network."


async def test_a_key_the_host_refuses_says_to_check_it(ssh: FakeSsh, fake_herdr):
    ssh.answers[herdr._READY] = answers(255, err="lo9i@pi: Permission denied (publickey).\n")
    with pytest.raises(
        UnreachableError, match=re.escape("SSH key, or the host's password. Check its details on the Hosts page")
    ):
        await fake_herdr.run("pi", "uptime", 10)


async def test_a_command_still_running_gives_back_the_screen(ssh: FakeSsh, fake_herdr):
    ssh.answers[herdr._READY] = answers(out=process_info())
    ssh.answers[herdr._RUN] = answers(124, out="me@host:~$ sleep 999; echo ...\n")
    run = await fake_herdr.run("pi", "sleep 999", 2)
    assert (run.exit_code, run.output) == (None, "me@host:~$ sleep 999; echo ...")
    _, _, args = ssh.calls[-1]
    pane, line, ending, wait = args
    assert pane == "w1:p2" and wait == "2000"
    assert line.startswith('sleep 999; echo "[lo9i:$?:') and ending.startswith(r"^\[lo9i:[0-9]+:")


async def test_output_that_scrolled_away_is_said_to_be_cut(ssh: FakeSsh, fake_herdr, monkeypatch):
    monkeypatch.setattr(herdr.secrets, "token_hex", lambda n: "abcd1234")
    ssh.answers[herdr._READY] = answers(out=process_info())
    ssh.answers[herdr._RUN] = answers(out=matched("line 398\nline 399\n[lo9i:0:abcd1234]\nme@host:~$"))
    run = await fake_herdr.run("pi", "seq 400", 10)
    assert (run.output, run.exit_code, run.truncated) == ("line 398\nline 399", 0, True)


async def test_installing_uses_herdrs_installer_into_local_bin(ssh: FakeSsh, fake_herdr):
    ssh.answers[herdr._INSTALL] = answers(out="installed herdr to /home/lo9i/.local/bin/herdr\nherdr 0.9.5\n")
    await fake_herdr.install("pi")
    assert "https://herdr.dev/install.sh" in herdr._INSTALL and 'HERDR_INSTALL_DIR="$HOME/.local/bin"' in herdr._INSTALL


@pytest.mark.parametrize(
    ("code", "out", "said"),
    [
        (3, "", "pi has no curl, which Herdr's installer needs"),
        (4, "", "couldn't download Herdr's installer from herdr.dev"),
        (
            1,
            "  \x1b[31m✗\x1b[0m unsupported architecture: riscv64\n",
            "stopped on pi: ✗ unsupported architecture: riscv64",
        ),
    ],
)
async def test_an_install_that_fails_says_why_without_terminal_colors(ssh: FakeSsh, fake_herdr, code, out, said):
    ssh.answers[herdr._INSTALL] = answers(code, out=out)
    with pytest.raises(HerdrError) as failed:
        await fake_herdr.install("pi")
    assert said in str(failed.value) and "\x1b" not in str(failed.value)


async def test_herdrs_own_errors_come_back_as_its_message(ssh: FakeSsh, fake_herdr):
    error = json.dumps({"id": "cli", "error": {"code": "invalid_params", "message": "bad pane id"}})
    ssh.answers[herdr._SCREEN] = answers(1, out=error)
    with pytest.raises(HerdrError, match="Herdr failed on pi: bad pane id"):
        await fake_herdr.screen("pi")


def test_the_terminal_attaches_to_lo9is_session():
    assert herdr.ATTACH.strip().endswith("exec herdr --session lo9i")
    assert 'export PATH="$HOME/.local/bin:$PATH"' in herdr.ATTACH


def test_every_script_finds_herdr_where_its_installer_puts_it():
    for script in (herdr._PROBE, herdr._READY, herdr._RUN, herdr._SCREEN, herdr.ATTACH):
        assert script.startswith('export PATH="$HOME/.local/bin:$PATH"')
