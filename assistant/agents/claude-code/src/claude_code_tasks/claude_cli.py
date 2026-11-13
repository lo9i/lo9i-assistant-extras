"""The Claude Code CLI a task runs: the user's own when it's installed, else the one the Claude Agent SDK ships.
The SDK's copy is a large binary (over 200 MB), so once the user's is found it's deleted."""

import os
import shutil
from pathlib import Path

import claude_agent_sdk

# Where Claude Code's installer puts it, which the daemon's PATH often lacks.
_LOCAL_BIN = Path.home() / ".local" / "bin"


def installed(local_bin: Path = _LOCAL_BIN) -> str | None:
    """The user's claude: on PATH or in ~/.local/bin."""
    path = os.pathsep.join(filter(None, (os.environ.get("PATH"), str(local_bin))))
    if found := shutil.which("claude", path=path):
        return found
    local = local_bin / "claude"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def bundled(package: Path = Path(claude_agent_sdk.__file__).parent) -> Path:
    return package / "_bundled" / "claude"


def drop_bundled(user_cli: str | None, copy: Path | None = None) -> None:
    """Deletes the SDK's copy when the user has their own; it stays as the fallback otherwise."""
    if user_cli is not None:
        (copy or bundled()).unlink(missing_ok=True)
