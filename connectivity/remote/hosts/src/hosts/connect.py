"""Connecting to a host of the plugin's list: ssh never reads the user's SSH config. For each connection an ssh
config is written from the list (service.ssh_config) to a file only the user can read, in a folder of its own,
given to ssh with -F, and deleted, folder and all, when the connection ends.

A host with a password: ssh asks the askpass helper for it (askpass.py, with SSH_ASKPASS_REQUIRE=force), which
gets it over a Unix socket in that folder that this process answers once, then closes. The password is never on
a command line, in an environment or in a file. ssh asks for it once (NumberOfPasswordPrompts=1), and the helper
answers only a password prompt: anything else ssh asks (a key's passphrase, whether to trust a new host key) is
asked in the terminal when there's one, and refused when there isn't.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from .askpass import ASKPASS_SOCKET
from .errors import ConnectError

_ASKPASS = "hosts-askpass"
# ssh asks for a password while it connects: after this long the socket stops listening, asked or not.
_ANSWER_SECONDS = 60


@dataclass(frozen=True)
class Connection:
    # ssh's options for the host: -F the config written for it, and how often to ask for its password.
    options: list[str]
    # What ssh's environment gets: the helper's, for a host with a password.
    env: dict[str, str] = field(default_factory=dict)
    password: bool = False

    @property
    def unattended(self) -> list[str]:
        """Options for an ssh nobody watches: it fails instead of asking for anything, but for the host's
        password, which the helper gives."""
        return ["-o", "BatchMode=no"] if self.password else ["-o", "BatchMode=yes"]


@contextlib.asynccontextmanager
async def session(name: str) -> AsyncIterator[Connection]:
    """What ssh needs to connect to the host, until the connection ends. Raises UnknownHostError, LockedError
    when the hosts can't be read, or ConnectError (errors.py)."""
    # The hosts know their tunnels, whose ssh connects here: imported when used, not to go round in a circle.
    from . import service

    text, password = await service.ssh_config(name)
    folder = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="hosts-ssh-"))
    server: asyncio.Server | None = None
    try:
        config = folder / "config"
        await asyncio.to_thread(_write_private, config, text)
        if not password:
            yield Connection(["-F", str(config)])
            return
        socket = folder / "askpass.sock"
        server = await _answer_once(socket, password)
        env = {"SSH_ASKPASS": askpass_program(), "SSH_ASKPASS_REQUIRE": "force", ASKPASS_SOCKET: str(socket)}
        yield Connection(["-F", str(config), "-o", "NumberOfPasswordPrompts=1"], env, True)
    finally:
        if server is not None:
            server.close()
        await asyncio.to_thread(shutil.rmtree, folder, True)


def askpass_program() -> str:
    """The helper, among the scripts of the plugin's environment, beside its Python."""
    program = Path(sys.executable).parent / _ASKPASS
    if program.is_file():
        return str(program)
    found = shutil.which(_ASKPASS)
    if found is None:
        raise ConnectError("The hosts plugin's password helper is missing. Update the hosts plugin in Plugins.")
    return found


def _write_private(path: Path, text: str) -> None:
    # Created readable only by the user, never readable by others even briefly.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(text)


async def _answer_once(path: Path, password: str) -> asyncio.Server:
    """A socket that gives the password to the first one to connect, then stops listening, as it does after
    _ANSWER_SECONDS when no one asks. Its folder is only the user's (mkdtemp makes it so)."""
    answered = False

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal answered
        server.close()
        try:
            if not answered:
                answered = True
                writer.write(password.encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(answer, str(path))
    asyncio.get_running_loop().call_later(_ANSWER_SECONDS, server.close)
    return server
