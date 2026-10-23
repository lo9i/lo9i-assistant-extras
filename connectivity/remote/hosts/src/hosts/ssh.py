"""Running commands on a host with the system's ssh, so the user's config, keys, agent and known hosts
apply as in their own terminal. Commands go to the host's `sh`, whatever its login shell is.
"""

import asyncio
import shlex
from dataclasses import dataclass
from typing import Protocol

from .sshconfig import ssh_options

# ssh exits with this when it couldn't connect or lost the connection.
SSH_FAILED = 255
# What `run` reports when the command didn't end in time.
TIMED_OUT = -1
# A command that runs on its own never asks for a password: it fails instead of waiting for one.
_BATCH = ("-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5")
# A terminal's connection notices a dead network within half a minute, so the page can reconnect.
_TERMINAL = ("-tt", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")
# What ssh says when it fails, in plain words. The first one found in its message is used.
_PROBLEMS = (
    ("could not resolve hostname", "its address couldn't be found. Is it on this network?"),
    ("timed out", "it didn't answer. It may be off, asleep or on another network."),
    ("no route to host", "there's no network path to it from here."),
    ("network is unreachable", "there's no network path to it from here."),
    ("connection refused", "it refused the connection. Is its SSH server running?"),
    ("host key verification failed", "its identity (host key) isn't the one this computer knows for it."),
    ("identification has changed", "its identity (host key) isn't the one this computer knows for it."),
    ("permission denied", "it didn't accept this computer's SSH key."),
    ("too many authentication failures", "it didn't accept this computer's SSH key."),
    ("connection closed", "it closed the connection."),
    ("connection reset", "the connection was cut."),
    ("broken pipe", "the connection was cut."),
)
# Problems trying again won't fix: the user has to do something first.
_LASTING = ("host key verification failed", "identification has changed", "permission denied", "too many auth")


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


class Ssh(Protocol):
    async def run(self, host: str, script: str, *args: str, timeout: float = 30) -> Result:
        """Runs `script` with `sh` on the host, with `args` as its $1, $2..."""
        ...


class OpenSsh:
    async def run(self, host: str, script: str, *args: str, timeout: float = 30) -> Result:
        process = await asyncio.create_subprocess_exec(
            "ssh", *ssh_options(), *_BATCH, "--", host, remote(script, *args), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return Result(TIMED_OUT, "", "")
        return Result(process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


def remote(script: str, *args: str) -> str:
    """The command line for the host's login shell: `sh -c` with the script and its arguments, quoted, so
    nothing in them is read by that shell."""
    return " ".join(["sh", "-c", shlex.quote(script), "sh", *(shlex.quote(a) for a in args)])


def terminal_command(host: str, script: str) -> list[str]:
    """ssh in a terminal of its own, for a person: it may ask for a key's passphrase there."""
    return ["ssh", *ssh_options(), *_TERMINAL, "--", host, remote(script)]


def problem(err: str) -> str:
    """Why ssh failed, in plain words, from what it wrote."""
    lowered = err.lower()
    for found, said in _PROBLEMS:
        if found in lowered:
            return said
    lines = [line.strip() for line in err.splitlines() if line.strip()]
    return lines[-1] if lines else "the connection failed."


def lasting(err: str) -> bool:
    """Whether trying again won't help until the user changes something (a key, a host key)."""
    lowered = err.lower()
    return any(found in lowered for found in _LASTING)
