"""Herdr (herdr.dev) on a host: its terminals keep running in a background server when the connection
drops, and attaching again (running `herdr` again) shows them as they are. lo9i uses one named session
per host, `lo9i`: the web app's terminal attaches to it, and the assistant's commands run in its focused
pane, the one the user sees.

Commands and their documentation: https://herdr.dev/docs/cli-reference/ and /docs/persistence-remote/.
`herdr --session <name>` launches or attaches to a session; the API commands (`pane run`, `pane read`,
`pane wait-output`) answer JSON, and a failed one exits 1 with a JSON error. Herdr installs with its
installer, which puts it in ~/.local/bin, a folder login shells often leave off PATH, so every script here
puts it first.
"""

import json
import re
import secrets
from dataclasses import dataclass

from .ssh import SSH_FAILED, TIMED_OUT, Result, Ssh, lasting, problem

SESSION = "lo9i"
INSTALLER = "https://herdr.dev/install.sh"
# Exit codes of the scripts here; the terminal's (ATTACH) says Herdr is missing with NOT_INSTALLED.
NOT_INSTALLED = 127
_NO_CURL = 3
_NO_DOWNLOAD = 4
_NO_SERVER = 5
_NO_PANE = 6
_STILL_RUNNING = 124
# How much of the pane a command's output is read from.
_ROWS = 400
# The most output given back; the rest is cut from the start, which is the least likely to matter.
_MAX_OUTPUT = 20_000

_PREAMBLE = f"""export PATH="$HOME/.local/bin:$PATH"
command -v herdr >/dev/null 2>&1 || exit {NOT_INSTALLED}
h() {{ herdr --session {SESSION} "$@"; }}
"""
_PROBE = f"""export PATH="$HOME/.local/bin:$PATH"
command -v herdr >/dev/null 2>&1 || exit {NOT_INSTALLED}
herdr session list --json
"""
_INSTALL = f"""command -v curl >/dev/null 2>&1 || exit {_NO_CURL}
installer=$(curl -fsSL --retry 2 --connect-timeout 10 {INSTALLER}) || exit {_NO_DOWNLOAD}
printf '%s\\n' "$installer" | HERDR_INSTALL_DIR="$HOME/.local/bin" sh || exit $?
"$HOME/.local/bin/herdr" --version
"""
# The focused pane's id in $pane, from `pane current`'s JSON, which has only that pane's.
_CURRENT = f"""current=$(h pane current) || exit {_NO_PANE}
pane=$(printf '%s\\n' "$current" | sed -n 's/.*"pane_id" *: *"\\([^"]*\\)".*/\\1/p')
"""
# Starts the session's server if it isn't running, with a workspace if it has none, and says what the
# focused pane runs.
_READY = (
    _PREAMBLE
    + f"""if ! h workspace list >/dev/null 2>&1; then
  nohup herdr --session {SESSION} server >/dev/null 2>&1 </dev/null &
  # A headless server gives no sign that it's ready but its socket answering; 5 seconds at most.
  tries=0
  until h workspace list >/dev/null 2>&1; do
    tries=$((tries + 1)); [ "$tries" -gt 50 ] && exit {_NO_SERVER}
    sleep 0.1
  done
fi
h pane current >/dev/null 2>&1 || h workspace create --label {SESSION} --cwd "$HOME" --focus >/dev/null
"""
    + _CURRENT
    + """h pane process-info --pane "$pane"
"""
)
# $1 the pane, $2 the command line, $3 the pattern of its end, $4 how long to wait (ms).
_RUN = (
    _PREAMBLE
    + f"""h pane run "$1" "$2" >/dev/null || exit {_NO_PANE}
h pane wait-output "$1" --regex "$3" --source recent-unwrapped --lines {_ROWS} --timeout "$4" && exit 0
h pane read "$1" --source recent-unwrapped --lines {_ROWS}
exit {_STILL_RUNNING}
"""
)
# $1 rows of scrollback to read, 0 for the screen as it is.
_SCREEN = (
    _PREAMBLE
    + _CURRENT
    + """if [ "$1" -gt 0 ]; then
  h pane read "$pane" --source recent --lines "$1"
else
  h pane read "$pane" --source visible
fi
"""
)
ATTACH = f"""export PATH="$HOME/.local/bin:$PATH"
command -v herdr >/dev/null 2>&1 || exit {NOT_INSTALLED}
exec herdr --session {SESSION}
"""


class HerdrError(Exception):
    """Something the user or the assistant can act on, in plain words."""


class NotInstalledError(HerdrError):
    pass


class UnreachableError(HerdrError):
    pass


class BusyError(HerdrError):
    pass


@dataclass(frozen=True)
class Probe:
    reachable: bool
    installed: bool = False
    # lo9i's Herdr session is running on the host.
    session: bool = False
    problem: str = ""


@dataclass(frozen=True)
class CommandRun:
    output: str
    # None while it still runs.
    exit_code: int | None
    # Lines before the output scrolled out of what was read.
    truncated: bool = False


class Herdr:
    def __init__(self, ssh: Ssh) -> None:
        self._ssh = ssh

    async def probe(self, host: str) -> Probe:
        """Whether the host answers, has Herdr, and runs lo9i's session. Never asks for a password."""
        result = await self._ssh.run(host, _PROBE, timeout=12)
        if result.code in (SSH_FAILED, TIMED_OUT):
            return Probe(False, problem=_unreachable(host, result.err, result.code))
        if result.code == NOT_INSTALLED:
            return Probe(True)
        return Probe(True, installed=True, session=_running(result.out))

    async def install(self, host: str) -> None:
        """Installs Herdr with its installer, in ~/.local/bin."""
        result = await self._ssh.run(host, _INSTALL, timeout=240)
        if result.code in (SSH_FAILED, TIMED_OUT):
            raise UnreachableError(_unreachable(host, result.err, result.code))
        if result.code == _NO_CURL:
            raise HerdrError(f"{host} has no curl, which Herdr's installer needs. Install curl there first.")
        if result.code == _NO_DOWNLOAD:
            raise HerdrError(f"{host} couldn't download Herdr's installer from herdr.dev. Is it online?")
        if result.code != 0:
            raise HerdrError(f"Herdr's installer stopped on {host}: {_last_line(result.out + result.err)}")

    async def run(self, host: str, command: str, timeout: float) -> CommandRun:
        """Types the command into the focused pane of lo9i's session and waits for it to end."""
        pane = await self._ready_pane(host)
        tag = secrets.token_hex(4)
        line = _with_marker(command, tag)
        ending = rf"^\[lo9i:[0-9]+:{tag}\]$"
        result = await self._ssh.run(host, _RUN, pane, line, ending, str(int(timeout * 1000)), timeout=timeout + 20)
        self._check(host, result)
        if result.code == _STILL_RUNNING:
            return CommandRun(_cut(result.out.rstrip()), None)
        text = json.loads(result.out)["result"]["read"]["text"]
        return _output(text, tag)

    async def screen(self, host: str, rows: int = 0) -> str:
        """What the focused pane shows, or its last `rows` rows."""
        result = await self._ssh.run(host, _SCREEN, str(max(rows, 0)), timeout=20)
        self._check(host, result)
        return result.out

    async def _ready_pane(self, host: str) -> str:
        result = await self._ssh.run(host, _READY, timeout=30)
        self._check(host, result)
        info = json.loads(result.out)["result"]["process_info"]
        busy = [p for p in info.get("foreground_processes", []) if p.get("pid") != info.get("shell_pid")]
        if busy:
            running = busy[0].get("cmdline") or busy[0].get("name") or "a program"
            raise BusyError(
                f"The terminal on {host} is busy running `{running}`. Wait for it to end, or ask the user to "
                "finish it in the terminal (Hosts page), then try again."
            )
        return info["pane_id"]

    def _check(self, host: str, result: Result) -> None:
        if result.code in (SSH_FAILED, TIMED_OUT):
            raise UnreachableError(_unreachable(host, result.err, result.code))
        if result.code == NOT_INSTALLED:
            raise NotInstalledError(
                f"Herdr isn't installed on {host}. The user installs it from the Hosts page in lo9i's web app "
                "(open the host's terminal)."
            )
        if result.code == _NO_SERVER:
            raise HerdrError(f"Herdr's session on {host} didn't start.")
        if result.code == _NO_PANE:
            raise HerdrError(f"lo9i's Herdr session on {host} has no terminal open. Open it from the Hosts page.")
        if result.code not in (0, _STILL_RUNNING):
            raise HerdrError(f"Herdr failed on {host}: {_herdr_error(result.out + result.err)}")


def _with_marker(command: str, tag: str) -> str:
    """The command line, followed by a line with its exit code and the tag, which ends its output. The
    line as typed shows `$?` where the output shows the code, so only the output matches its pattern. A
    command over several lines, or with a `#` that may start a comment, goes in braces, so the marker
    still runs after it."""
    marker = f'echo "[lo9i:$?:{tag}]"'
    command = command.strip()
    if "\n" in command or "#" in command:
        return f"{{ {command}\n}}; {marker}"
    return f"{command}; {marker}"


def _output(text: str, tag: str) -> CommandRun:
    """The lines between the command as typed and its marker."""
    lines = text.splitlines()
    marker = re.compile(rf"^\[lo9i:([0-9]+):{tag}\]$")
    end, code = next((i, int(m.group(1))) for i in reversed(range(len(lines))) if (m := marker.match(lines[i])))
    typed = [i for i, line in enumerate(lines[:end]) if f"[lo9i:$?:{tag}]" in line]
    start = typed[-1] + 1 if typed else 0
    return CommandRun(_cut("\n".join(lines[start:end])), code, truncated=not typed)


def _cut(text: str) -> str:
    return text if len(text) <= _MAX_OUTPUT else "…" + text[-_MAX_OUTPUT:]


def _running(sessions_json: str) -> bool:
    try:
        sessions = json.loads(sessions_json).get("sessions", [])
    except (ValueError, AttributeError):
        return False
    return any(s.get("name") == SESSION and s.get("running") for s in sessions)


def _unreachable(host: str, err: str, code: int) -> str:
    if code == TIMED_OUT:
        return f"{host} didn't answer in time."
    reason = problem(err)
    return f"Can't reach {host}: {reason}" + (" Check it in a terminal with `ssh`." if lasting(err) else "")


def _herdr_error(text: str) -> str:
    """The message of Herdr's JSON error, or its last line."""
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            continue
    return _last_line(text)


def _last_line(text: str) -> str:
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    return lines[-1] if lines else "no reason given."
