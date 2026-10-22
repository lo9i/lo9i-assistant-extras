import subprocess

import pytest

from lo9i_factory import db


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def git_repo(tmp_path):
    """A folder that passes for a git repository: the service only looks for .git."""
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    return repo


def run(*args: str, cwd) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def project(tmp_path):
    """A real repository with one commit."""
    path = tmp_path / "shop"
    path.mkdir()
    run("git", "init", "-q", "-b", "main", cwd=path)
    (path / "README.md").write_text("# Shop\n")
    run("git", "add", ".", cwd=path)
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start", cwd=path)
    return path
