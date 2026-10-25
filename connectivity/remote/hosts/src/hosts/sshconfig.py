"""The user's SSH config (~/.ssh/config and the files it includes), read to import its hosts into the plugin's
own list (service.py), never written: one host per Host line, named after its first name that isn't a pattern
(`Host pi lo9i-pi` is `pi`), with the options the config gives it as ssh reads them: from every Host entry whose
patterns match its name (`Host *` too), the first value of each option winning. Match entries are left out.
"""

import fnmatch
import glob
import os
import re
import shlex
from pathlib import Path

from .details import Details, option_problem, valid_name

# ssh's own limit on nested Include files.
_MAX_DEPTH = 16
# A keyword and its values, after a space or an "=". Comments and blank lines don't match.
_LINE = re.compile(r"([A-Za-z]\w*)(?:\s*=\s*|\s+)(.*)")
# Options given on several lines, each one adding a value instead of the first one winning.
_MANY = {"identityfile", "certificatefile", "localforward", "remoteforward", "dynamicforward", "sendenv", "setenv"}


def config_path() -> Path:
    """HOSTS_SSH_CONFIG when set (tests); the user's own config otherwise."""
    return Path(os.environ.get("HOSTS_SSH_CONFIG") or Path.home() / ".ssh" / "config")


def names(path: Path | None = None) -> list[str]:
    """Each host's name, in the order the config has them."""
    return _names(_entries(path or config_path(), 0))


def hosts(path: Path | None = None) -> list[tuple[str, Details]]:
    """Each host with what the config gives it, named as the plugin can keep it (importable)."""
    entries = _entries(path or config_path(), 0)
    found: list[tuple[str, Details]] = []
    for name in _names(entries):
        kept = importable(name)
        if kept and kept not in {n for n, _ in found}:
            found.append((kept, _details(name, entries)))
    return found


def importable(name: str) -> str:
    """The name as the plugin keeps it: what it can't have (a space) becomes `-`; "" when nothing's left."""
    kept = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).lstrip("-.")[:63]
    return kept if valid_name(kept) else ""


def _names(entries: list[tuple[str, list[str]]]) -> list[str]:
    found: list[str] = []
    for keyword, values in entries:
        name = next((v for v in values if _is_name(v)), None) if keyword.lower() == "host" else None
        if name and name not in found:
            found.append(name)
    return found


def _details(name: str, entries: list[tuple[str, list[str]]]) -> Details:
    """What the config's entries give the host, as ssh would take it."""
    applies = True  # Before the first Host line, options are every host's.
    taken: list[tuple[str, str]] = []
    seen: set[str] = set()
    for keyword, values in entries:
        lowered = keyword.lower()
        if lowered == "host":
            applies = _matches(name, values)
        elif lowered == "match":
            applies = False
        elif applies and values and (lowered in _MANY or lowered not in seen):
            seen.add(lowered)
            taken.append((keyword, " ".join(_quoted(v) for v in values)))
    first: dict[str, str] = {}
    for keyword, value in taken:
        first.setdefault(keyword.lower(), _unquoted(value))
    hostname = first.get("hostname", name).replace("%h", name).replace("%%", "%")
    try:
        port = int(first.get("port", "22"))
    except ValueError:
        port = 22
    options: list[tuple[str, str]] = []
    identity_seen = False
    for keyword, value in taken:
        lowered = keyword.lower()
        if lowered in ("hostname", "user", "port"):
            continue
        if lowered == "identityfile" and not identity_seen:
            identity_seen = True  # The first one is the field; more are options.
            continue
        if not option_problem(keyword, value):
            options.append((keyword, value))
    return Details(hostname, first.get("user", ""), port, first.get("identityfile", ""), options)


def _matches(name: str, patterns: list[str]) -> bool:
    """Whether a Host line's patterns take the name, as ssh matches them: a `!` pattern that matches rules it out."""
    lowered = name.lower()
    found = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if fnmatch.fnmatchcase(lowered, pattern.removeprefix("!").lower()):
            if negated:
                return False
            found = True
    return found


def _quoted(value: str) -> str:
    return f'"{value}"' if any(c.isspace() for c in value) else value


def _unquoted(value: str) -> str:
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] == '"' else value


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
        if keyword.lower() == "include" and depth < _MAX_DEPTH:
            for included in _included(values):
                entries.extend(_entries(included, depth + 1))
        elif keyword:
            entries.append((keyword, values))
    return entries


def _parse(line: str) -> tuple[str, list[str]]:
    """A line's keyword, as written, and its values: `Host pi lo9i-pi`, `HostName=pi.local`."""
    found = _LINE.fullmatch(line.strip())
    if found is None:
        return "", []
    keyword, rest = found.groups()
    try:
        values = shlex.split(rest, comments=True)
    except ValueError:
        values = rest.split()
    return keyword, values


def _included(patterns: list[str]) -> list[Path]:
    """Include's files: relative ones are in ~/.ssh, as ssh reads them for the user's config."""
    found: list[Path] = []
    for pattern in patterns:
        full = Path(pattern).expanduser()
        if not full.is_absolute():
            full = Path.home() / ".ssh" / full
        found.extend(Path(p) for p in sorted(glob.glob(str(full))))
    return found
