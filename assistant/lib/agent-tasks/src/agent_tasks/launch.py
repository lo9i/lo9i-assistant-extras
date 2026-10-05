"""Starting and stopping workers. A worker is started through a shell that exits at once, so it belongs
to no one: the MCP server never has to wait for it, and it keeps running when the server stops."""

import contextlib
import os
import signal
import subprocess
from pathlib import Path

# Runs the worker in the background with its output in a log, and exits.
_DETACH = '"$@" </dev/null >>"$WORKER_LOG" 2>&1 &'


def start(command: list[str], folder: Path) -> None:
    """Runs `command` with the task's folder as its last argument."""
    env = {**os.environ, "WORKER_LOG": str(folder / "worker.log")}
    subprocess.run(["/bin/sh", "-c", _DETACH, "sh", *command, str(folder)], env=env, check=True)


def stop(pid: int) -> None:
    """Stops the worker and everything it started: it leads its own process group (worker.py)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
