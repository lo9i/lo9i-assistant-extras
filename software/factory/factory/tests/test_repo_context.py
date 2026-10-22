import subprocess

from lo9i_factory import repo_context


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_git_repositorys_tracked_files_readme_and_agent_instructions(tmp_path):
    repo = tmp_path / "shop"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("print('hi')")
    (repo / "README.md").write_text("# Shop")
    (repo / "AGENTS.md").write_text("Run make test.")
    (repo / ".gitignore").write_text("secrets.env\n")
    (repo / "secrets.env").write_text("KEY=1")
    git(tmp_path, "init", "-q", "shop")
    git(repo, "add", ".")

    text = repo_context.read(str(repo))
    assert "## Files in the repository\n\n.gitignore\nAGENTS.md\nREADME.md\nsrc/app.py" in text
    assert "secrets.env" not in text
    assert "## README.md\n\n# Shop" in text and "## AGENTS.md\n\nRun make test." in text
    assert "CLAUDE.md" not in text


def test_without_git_the_files_are_walked(git_repo):
    (git_repo / "node_modules").mkdir()
    (git_repo / "node_modules" / "x.js").write_text("")
    (git_repo / "main.go").write_text("package main")
    text = repo_context.read(str(git_repo))
    assert "## Files in the repository\n\nmain.go" in text and "node_modules" not in text


def test_large_files_and_trees_are_cut(git_repo):
    (git_repo / "README").write_text("x" * (repo_context.MAX_FILE_CHARS + 10))
    for i in range(repo_context.MAX_TREE_LINES + 5):
        (git_repo / f"f{i:04}.txt").write_text("")
    text = repo_context.read(str(git_repo))
    assert f"… (cut at {repo_context.MAX_FILE_CHARS} characters)" in text
    assert "… and 6 more files" in text
