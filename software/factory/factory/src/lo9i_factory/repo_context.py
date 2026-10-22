"""What the team reads about a repository, when the task asks for it (SPEC §9):
its README, its file tree, and the instructions it keeps for coding agents
(CLAUDE.md, AGENTS.md). Each is cut to a size, saying so, so a large
repository doesn't fill the models' context."""

import os
import subprocess
from pathlib import Path

READMES = ("README.md", "README", "README.rst", "README.txt")
AGENT_FILES = ("CLAUDE.md", "AGENTS.md")
MAX_FILE_CHARS = 20_000
MAX_TREE_LINES = 400
# Left out of the tree when git can't list the files.
_SKIPPED = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}


def read(repo: str) -> str:
    root = Path(repo)
    parts = [f"## Files in the repository\n\n{_tree(root)}"]
    readme = next((name for name in READMES if (root / name).is_file()), None)
    for name in ([readme] if readme else []) + [n for n in AGENT_FILES if (root / n).is_file()]:
        parts.append(f"## {name}\n\n{_cut((root / name).read_text(errors='replace'), MAX_FILE_CHARS, 'characters')}")
    return "\n\n".join(parts)


def _tree(root: Path) -> str:
    files = _git_files(root)
    if files is None:
        files = sorted(_walk(root))
    return _cut("\n".join(files), MAX_TREE_LINES, "files") if files else "(no files)"


def _git_files(root: Path) -> list[str] | None:
    """The files git tracks, which leaves out what .gitignore does; None
    when git can't say."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, timeout=20, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.splitlines()


def _walk(root: Path) -> list[str]:
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIPPED]
        found += [os.path.relpath(os.path.join(folder, n), root) for n in names if not n.startswith(".")]
    return found


def _cut(text: str, limit: int, unit: str) -> str:
    """At most `limit` characters, or lines for "files"."""
    if unit == "files":
        lines = text.splitlines()
        if len(lines) <= limit:
            return text
        return "\n".join(lines[:limit]) + f"\n… and {len(lines) - limit} more files"
    return text if len(text) <= limit else text[:limit] + f"\n… (cut at {limit} {unit})"
