"""The task's git worktree and branch (SPEC §1, §3, §10).

Each task works in its own worktree, on the branch factory/<id>-<slug>, made
from the repository's base branch (Settings, per repo) or what's checked out.
A new project starts as a folder the factory makes a repository of
(ensure_repo); with no commit yet, the branch starts empty (an orphan), so
nothing is added to the user's own branches, and its diff is all it has.
The factory commits what the agent changed, as "lo9i factory", skipping the
repository's hooks and commit signing, which could stop or hang it. After
the final diff is approved the worktree goes; the branch stays, never merged
or pushed.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import FactoryError

AUTHOR = ("lo9i factory", "factory@lo9i.invalid")
# What a branch with no commit before the task's is compared with: git's empty tree.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_TIMEOUT = 120


class GitError(FactoryError):
    pass


@dataclass(frozen=True)
class Changed:
    """A file in the diff; added and deleted are None for a binary file."""

    path: str
    added: int | None
    deleted: int | None


def git(folder: str | Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git {args[0]} failed in {folder}: {e}") from None
    if done.returncode != 0:
        raise GitError(f"git {args[0]} failed in {folder}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def has_branch(repo: str, branch: str) -> bool:
    try:
        git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except GitError:
        return False
    return True


def commit_of(folder: str, ref: str = "HEAD") -> str | None:
    """The commit `ref` is at; None when there's none (a repository with no
    commit yet, a branch not made)."""
    try:
        return git(folder, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip() or None
    except GitError:
        return None


def ensure_repo(path: Path) -> None:
    """A git repository at `path`, made if it's a new project: a folder that
    doesn't exist yet, or an empty one. Raises GitError for a folder with
    files that isn't a repository: committing them is the user's call."""
    if (path / ".git").exists():
        return
    if path.is_dir() and any(path.iterdir()):
        raise GitError(
            f"{path} has files but isn't a git repository: run git init there and commit them, or give an empty"
            " folder for a new project"
        )
    if not path.parent.is_dir():
        raise GitError(f"no folder {path.parent} to make {path.name} in")
    path.mkdir(exist_ok=True)
    git(path, "init", "-q", "-b", "main")


def add_worktree(repo: str, path: Path, branch: str, base: str) -> str:
    """The worktree at `path` on `branch`, made from `base` if it's new (or
    as it was, when a restart or a resume finds it gone). Returns the commit
    `base` is at, or the empty tree when the repository has no commit yet:
    the branch then starts empty."""
    git(repo, "worktree", "prune")
    path.parent.mkdir(parents=True, exist_ok=True)
    base_commit = commit_of(repo, base)
    if base_commit is None and base != "HEAD":
        raise GitError(f"no branch or commit {base} in {repo} to start the task's branch from")
    if has_branch(repo, branch):
        git(repo, "worktree", "add", str(path), branch)
    elif base_commit is None:
        git(repo, "worktree", "add", "--orphan", "-b", branch, str(path))
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), base_commit)
    return base_commit or EMPTY_TREE


def commit(worktree: str, message: str) -> bool:
    """Commit everything changed in the worktree; False when nothing was."""
    git(worktree, "add", "-A")
    if not git(worktree, "status", "--porcelain"):
        return False
    git(
        worktree,
        "-c",
        f"user.name={AUTHOR[0]}",
        "-c",
        f"user.email={AUTHOR[1]}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-q",
        "-m",
        message,
    )
    return True


def head(worktree: str) -> str:
    """The worktree's commit; the empty tree before its first."""
    return commit_of(worktree) or EMPTY_TREE


def changed(folder: str, base: str, tip: str = "HEAD") -> list[Changed]:
    """What `tip` changed since `base`: files with lines added and deleted.
    Nothing while `tip` has no commit (a new project's branch, still empty)."""
    if commit_of(folder, tip) is None:
        return []
    out = []
    for line in git(folder, "diff", "--numstat", base, tip).splitlines():
        added, deleted, path = line.split("\t", 2)
        out.append(Changed(path, None if added == "-" else int(added), None if deleted == "-" else int(deleted)))
    return out


def diff(folder: str, base: str, limit: int, tip: str = "HEAD") -> str:
    """What `tip` changed since `base`, as a diff cut at `limit` characters."""
    if commit_of(folder, tip) is None:
        return ""
    text = git(folder, "diff", base, tip)
    return text if len(text) <= limit else text[:limit] + f"\n… (diff cut at {limit} characters)"


def remove_worktree(repo: str, path: str) -> None:
    """The worktree goes; its branch stays."""
    if Path(path).exists():
        git(repo, "worktree", "remove", "--force", path)
    git(repo, "worktree", "prune")
