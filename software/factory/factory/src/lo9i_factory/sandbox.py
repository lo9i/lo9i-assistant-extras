"""Where the coding agent and the test commands run (SPEC §11).

- docker (the default): in a container of the factory's image (docker/
  Dockerfile), with only the task's worktree mounted, at /work, as the
  user's uid, so the files it writes are the user's. Any command, the whole
  network. Nothing else of the computer: no ~/.lo9i, no keys, no other repos.
- local: on the computer, in the worktree, with a home folder of its own, so
  the agent finds none of the user's settings, logins or MCP servers. What
  it may do there is up to the agent's runner (agents/): every command and
  network access is the user's to allow.

Either way the agent's environment has nothing but what it needs and its
own login: no secrets of lo9i's, no env of the server's. Values reach the
container by name (-e KEY), never in the command line, which other users of
the computer can read.
"""

import asyncio
import hashlib
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import db
from .errors import FactoryError

WORKDIR = "/work"
DOCKERFILE = Path(__file__).parent / "docker" / "Dockerfile"
# Where a CLI may be when lo9i's PATH is short: launched from a service, it
# often lacks the user's own bin folders.
_BIN = (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"))
# What the docker client itself needs: its config and context are in HOME.
_DOCKER_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
# Lines of a command's output kept: its end, where the errors are.
OUTPUT_CHARS = 20_000
# A line of a CLI's JSON output can be large: a whole file it read.
_LINE_BYTES = 64 * 1024 * 1024
_CHECK_SECONDS = 2.0


class SandboxError(FactoryError):
    pass


def find(name: str) -> str:
    """A CLI's path. Raises SandboxError when it isn't installed."""
    path = shutil.which(name) or next((str(d / name) for d in _BIN if (d / name).is_file()), None)
    if path is None:
        raise SandboxError(f"{name} isn't installed on this computer")
    return path


def image() -> str:
    """The image's tag, named after its Dockerfile so a change builds a new one."""
    return "lo9i-factory-agent:" + hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]


@dataclass(frozen=True)
class Done:
    exit_code: int
    # The end of what it printed (stderr, and stdout when not read as lines).
    output: str
    seconds: float
    # Stopped by should_stop or the time limit.
    stopped: bool = False


@dataclass(frozen=True)
class Sandbox:
    mode: str
    worktree: str
    # Names its container, to stop it.
    name: str

    @classmethod
    def of(cls, mode: str, worktree: str, task_id: int, stage_id: int) -> "Sandbox":
        return cls(mode, worktree, f"lo9i-factory-{task_id}-{stage_id}")

    @property
    def folder(self) -> str:
        """The worktree as the agent sees it."""
        return WORKDIR if self.mode == "docker" else self.worktree

    @property
    def home(self) -> Path:
        """The agent's own home folder in local mode."""
        return db.data_dir() / "homes" / self.name

    def command(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str], str]:
        """The process to start for `argv` with `env`: argv, its env, its folder."""
        if self.mode == "docker":
            run = [
                find("docker"),
                "run",
                "--rm",
                "-i",
                "--name",
                self.name,
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{self.worktree}:{WORKDIR}",
                "-w",
                WORKDIR,
                "--tmpfs",
                "/tmp:exec,mode=1777",
                "-e",
                "HOME=/tmp",
            ]
            for key in env:
                run += ["-e", key]
            client = {k: os.environ[k] for k in _DOCKER_ENV if k in os.environ}
            client["PATH"] = _path()
            return [*run, image(), *argv], {**client, **env}, self.worktree
        self.home.mkdir(parents=True, exist_ok=True)
        local = {"PATH": _path(), "HOME": str(self.home), "LANG": os.environ.get("LANG", "en_US.UTF-8")}
        return argv, {**local, **env}, self.worktree

    async def run(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        *,
        stdin: str | None = None,
        on_line: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] = lambda: False,
        timeout: float,
    ) -> Done:
        """Run `argv` here. With `on_line`, stdout comes to it line by line
        and stderr is what's kept; without, both are kept together."""
        args, process_env, cwd = self.command(argv, env or {})
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=process_env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if on_line else asyncio.subprocess.STDOUT,
            limit=_LINE_BYTES,
        )
        if stdin is not None:
            process.stdin.write(stdin.encode())
            await process.stdin.drain()
            process.stdin.close()
        kept: list[str] = []
        lines = asyncio.create_task(_read(process.stdout, on_line or kept.append))
        errors = asyncio.create_task(_read(process.stderr, kept.append)) if on_line else None
        stopped = False
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), _CHECK_SECONDS)
            except TimeoutError:
                if should_stop() or time.monotonic() - started > timeout:
                    stopped = True
                    await self._stop(process)
        await lines
        if errors:
            await errors
        return Done(process.returncode, _tail("".join(kept)), time.monotonic() - started, stopped)

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if self.mode == "docker":
            # The container outlives its client otherwise.
            killer = await asyncio.create_subprocess_exec(
                find("docker"), "kill", self.name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await killer.wait()
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _read(stream: asyncio.StreamReader, sink: Callable[[str], None]) -> None:
    async for raw in stream:
        sink(raw.decode(errors="replace"))


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else "…\n" + text[-OUTPUT_CHARS:]


def _path() -> str:
    """PATH with the usual bin folders, for CLIs installed for the user."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(dict.fromkeys([*parts, *(str(d) for d in _BIN)]))


async def check_docker() -> None:
    """Raises SandboxError when Docker isn't there or isn't running."""
    try:
        docker = find("docker")
    except SandboxError:
        raise SandboxError("Docker isn't installed: install Docker Desktop, or set run_mode to local") from None
    process = await asyncio.create_subprocess_exec(
        docker, "info", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    if await process.wait() != 0:
        raise SandboxError("Docker isn't running: start Docker Desktop, or set run_mode to local")


async def ensure_image(on_line: Callable[[str], None]) -> str:
    """The agent image, built the first time (a few minutes, from the network)."""
    tag = image()
    docker = find("docker")
    found = await asyncio.create_subprocess_exec(
        docker, "image", "inspect", tag, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    if await found.wait() == 0:
        return tag
    on_line(f"Building the agent image {tag}, once: a few minutes\n")
    build = await asyncio.create_subprocess_exec(
        docker,
        "build",
        "-t",
        tag,
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={k: os.environ[k] for k in _DOCKER_ENV if k in os.environ} | {"PATH": _path()},
    )
    build.stdin.write(DOCKERFILE.read_bytes())
    await build.stdin.drain()
    build.stdin.close()
    kept: list[str] = []
    await _read(build.stdout, lambda line: (kept.append(line), on_line(line)))
    if await build.wait() != 0:
        raise SandboxError("couldn't build the agent image: " + "".join(kept)[-2000:])
    return tag
