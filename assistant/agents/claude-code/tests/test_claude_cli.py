import os

from claude_code_tasks import claude_cli


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_the_users_claude_is_found_in_local_bin_when_path_lacks_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    claude = _executable(tmp_path / "local" / "claude")
    assert claude_cli.installed(tmp_path / "local") == str(claude)


def test_claude_on_path_comes_first(tmp_path, monkeypatch):
    on_path = _executable(tmp_path / "bin" / "claude")
    _executable(tmp_path / "local" / "claude")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert claude_cli.installed(tmp_path / "local") == str(on_path)


def test_without_one_the_sdks_copy_is_used_and_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "claude").write_text("not executable")
    copy = _executable(tmp_path / "_bundled" / "claude")
    found = claude_cli.installed(tmp_path / "local")
    claude_cli.drop_bundled(found, copy)
    assert found is None
    assert copy.exists()


def test_the_sdks_copy_goes_once_the_users_is_found(tmp_path):
    copy = _executable(tmp_path / "_bundled" / "claude")
    claude_cli.drop_bundled("/usr/local/bin/claude", copy)
    claude_cli.drop_bundled("/usr/local/bin/claude", copy)
    assert not os.path.exists(copy)
