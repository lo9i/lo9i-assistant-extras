"""The hosts in the user's SSH config: ~/.ssh/config and the files it includes, one host per Host line,
named after its first name that isn't a pattern (`Host pi lo9i-pi` is `pi`). Where each one goes
(address, user, port) comes from `ssh -G`, which applies the whole config the way ssh will.
"""

import asyncio
import glob
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# ssh's own limit on nested Include files.
_MAX_DEPTH = 16
# A keyword and its values, after a space or an "=". Comments and blank lines don't match.
_LINE = re.compile(r"([A-Za-z]\w*)(?:\s*=\s*|\s+)(.*)")


@dataclass(frozen=True)
class Address:
    hostname: str
    user: str
    port: int

    def __str__(self) -> str:
        port = "" if self.port == 22 else f":{self.port}"
        return f"{self.user}@{self.hostname}{port}" if self.user else f"{self.hostname}{port}"


def config_path() -> Path:
    """HOSTS_SSH_CONFIG when set (tests); the user's own config otherwise."""
    return Path(os.environ.get("HOSTS_SSH_CONFIG") or Path.home() / ".ssh" / "config")


def ssh_options() -> list[str]:
    """What tells ssh to read HOSTS_SSH_CONFIG instead of the user's config, when it's set."""
    chosen = os.environ.get("HOSTS_SSH_CONFIG")
    return ["-F", chosen] if chosen else []


def names(path: Path | None = None) -> list[str]:
    """Each host's name, in the order the config has them."""
    found: list[str] = []
    for keyword, values in _entries(path or config_path(), 0):
        name = next((v for v in values if _is_name(v)), None) if keyword == "host" else None
        if name and name not in found:
            found.append(name)
    return found


async def resolve(name: str) -> Address | None:
    """Where ssh goes for this host, or None when ssh can't say (a broken config)."""
    process = await asyncio.create_subprocess_exec(
        "ssh", *ssh_options(), "-G", "--", name, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    out, _ = await process.communicate()
    if process.returncode != 0:
        return None
    options: dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines():
        key, _, value = line.partition(" ")
        options[key] = value
    try:
        port = int(options.get("port", "22"))
    except ValueError:
        port = 22
    return Address(options.get("hostname", name), options.get("user", ""), port)


def _is_name(value: str) -> bool:
    # Patterns match many hosts; a name starting with "-" would read as an option of ssh's.
    return not any(c in value for c in "*?!") and not value.startswith("-")


def _entries(path: Path, depth: int) -> list[tuple[str, list[str]]]:
    """The config's keywords with their values, the included files' in their place."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        keyword, values = _parse(line)
        if keyword == "include" and depth < _MAX_DEPTH:
            for included in _included(values):
                entries.extend(_entries(included, depth + 1))
        elif keyword:
            entries.append((keyword, values))
    return entries


def _parse(line: str) -> tuple[str, list[str]]:
    """A line's keyword, lowercase, and its values: `Host pi lo9i-pi`, `HostName=pi.local`."""
    found = _LINE.fullmatch(line.strip())
    if found is None:
        return "", []
    keyword, rest = found.groups()
    try:
        values = shlex.split(rest, comments=True)
    except ValueError:
        values = rest.split()
    return keyword.lower(), values


def _included(patterns: list[str]) -> list[Path]:
    """Include's files: relative ones are in ~/.ssh, as ssh reads them for the user's config."""
    found: list[Path] = []
    for pattern in patterns:
        full = Path(pattern).expanduser()
        if not full.is_absolute():
            full = Path.home() / ".ssh" / full
        found.extend(Path(p) for p in sorted(glob.glob(str(full))))
    return found
