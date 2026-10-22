"""The sandbox, the agents' command lines and how their output is read, the
local-mode approvals, and the git worktree."""

import asyncio
import json
import os
import subprocess

import pytest
from mcp import Client

from lo9i_factory import approvals, db, git, repo, service
from lo9i_factory.agents import Job, Result, report
from lo9i_factory.agents.claude_code import ClaudeCode, result, steps
from lo9i_factory.agents.codex import Codex
from lo9i_factory.sandbox import WORKDIR, Done, Sandbox, image


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "factory.db"))
    return tmp_path


def job(mode: str, worktree: str = "/repo/wt", budget: float | None = 2.5) -> Job:
    return Job(1, 7, "Build the cache", "You are the implementer.", "claude-sonnet-5-5", budget,
               Sandbox.of(mode, worktree, 1, 7))  # fmt: skip


# --- Sandbox ---


def test_docker_mounts_only_the_worktree_and_passes_values_by_name(data, monkeypatch):
    monkeypatch.setenv("SECRET_OF_LO9I", "x")
    argv, env, _ = Sandbox.of("docker", "/repo/wt", 1, 7).command(["claude", "-p"], {"ANTHROPIC_API_KEY": "sk"})
    assert argv[1:4] == ["run", "--rm", "-i"] and argv[-3:] == [image(), "claude", "-p"]
    assert argv[argv.index("-v") + 1] == f"/repo/wt:{WORKDIR}" and argv.count("-v") == 1
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "ANTHROPIC_API_KEY" in argv and "sk" not in argv
    assert env["ANTHROPIC_API_KEY"] == "sk" and "SECRET_OF_LO9I" not in env
    assert argv[argv.index("--name") + 1] == "lo9i-factory-1-7"


def test_local_gives_the_agent_its_own_home_and_nothing_else(data, monkeypatch):
    monkeypatch.setenv("SECRET_OF_LO9I", "x")
    argv, env, cwd = Sandbox.of("local", "/repo/wt", 1, 7).command(["codex"], {"CODEX_API_KEY": "sk"})
    assert (argv, cwd) == (["codex"], "/repo/wt")
    assert set(env) == {"PATH", "HOME", "LANG", "CODEX_API_KEY"}
    assert env["HOME"] == str(data / "homes" / "lo9i-factory-1-7")


def test_a_local_command_streams_its_lines_and_can_be_stopped(data, tmp_path):
    box = Sandbox.of("local", str(tmp_path), 1, 7)
    lines: list[str] = []
    done = asyncio.run(box.run(["sh", "-c", "echo one; echo two >&2; exit 3"], on_line=lines.append, timeout=60))
    assert (lines, done.output.strip(), done.exit_code) == (["one\n"], "two", 3)
    together = asyncio.run(box.run(["sh", "-c", "echo out; echo err >&2"], timeout=60))
    assert together.output.split() == ["out", "err"]
    stopped = asyncio.run(box.run(["sleep", "30"], should_stop=lambda: True, timeout=60))
    assert stopped.stopped and stopped.seconds < 10


# --- Claude Code ---


def test_claude_code_in_docker_skips_its_permission_checks(data):
    argv = ClaudeCode("sk").argv(job("docker"))
    assert argv[:2] == ["claude", "-p"] and "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["summary", "points"]
    assert "--strict-mcp-config" in argv and "--permission-prompt-tool" not in argv
    assert ClaudeCode("sk").env(job("docker")) == {"ANTHROPIC_API_KEY": "sk", "DISABLE_AUTOUPDATER": "1"}


def test_claude_code_locally_asks_the_factory_before_each_step(data, monkeypatch):
    monkeypatch.setattr("lo9i_factory.agents.claude_code.find", lambda name: f"/bin/{name}")
    argv = ClaudeCode("sk").argv(job("local"))
    assert argv[0] == "/bin/claude" and "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__factory__approve"
    assert argv[argv.index("--setting-sources") + 1] == "local"
    server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["factory"]
    assert server["args"] == ["-m", "lo9i_factory.approvals"]
    assert server["env"] == {
        "FACTORY_DB": str(data / "factory.db"),
        "FACTORY_TASK": "1",
        "FACTORY_STAGE": "7",
        "FACTORY_WORKTREE": "/repo/wt",
    }
    assert "MCP_TOOL_TIMEOUT" in ClaudeCode("sk").env(job("local"))


def test_claude_codes_result_and_steps():
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Done.",
        "total_cost_usd": 1.25,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
        "structured_output": {"summary": "Added the cache", "points": [{"id": 1, "status": "resolved", "reason": "x"}]},
    }
    r = result(event)
    assert (r.ok, r.summary, r.usd, r.input_tokens, r.cache_read_tokens) == (True, "Added the cache", 1.25, 10, 100)
    assert r.points == [{"id": 1, "status": "resolved", "reason": "x"}]
    over = result({**event, "subtype": "error_max_budget_usd", "is_error": True, "structured_output": None})
    assert not over.ok and over.error.startswith("Claude Code ended with error_max_budget_usd")
    said = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Reading it"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
            ]
        },
    }
    assert steps(said) == [
        {"type": "text", "text": "Reading it"},
        {"type": "tool", "tool": "Bash", "detail": "pytest -q"},
    ]


class FakeBox:
    """A sandbox that prints the given JSON lines instead of running anything."""

    def __init__(self, mode: str, lines: list[dict], exit_code: int = 0) -> None:
        self.mode, self.worktree, self.lines, self.exit_code = mode, "/repo/wt", lines, exit_code
        self.folder = WORKDIR if mode == "docker" else self.worktree

    async def run(self, argv, env=None, *, stdin=None, on_line=None, should_stop=lambda: False, timeout):
        self.seen = (argv, env, stdin)
        for line in self.lines:
            on_line(json.dumps(line) + "\n")
        return Done(self.exit_code, "", 1.0)


def test_claude_code_runs_and_reports(data):
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "On it"}]}},
        {"type": "result", "subtype": "success", "result": "", "total_cost_usd": 0.3,
         "structured_output": {"summary": "Done", "points": []}},
    ]  # fmt: skip
    sandbox = FakeBox("docker", lines)
    logged: list[dict] = []
    r = asyncio.run(ClaudeCode("sk").run(Job(1, 7, "Build", "sys", "m", None, sandbox), logged.append, lambda: False))
    assert (r.ok, r.summary, r.usd) == (True, "Done", 0.3)
    assert logged == [{"type": "text", "text": "On it"}]
    assert sandbox.seen[2] == "Build"


# --- Codex ---


def test_codex_in_docker_and_locally(data, monkeypatch):
    monkeypatch.setattr("lo9i_factory.agents.codex.find", lambda name: f"/bin/{name}")
    docker = Codex("sk").argv(job("docker"))
    assert docker[:3] == ["codex", "exec", "--json"] and "--dangerously-bypass-approvals-and-sandbox" in docker
    assert docker[docker.index("--cd") + 1] == WORKDIR
    local = Codex("sk").argv(job("local"))
    assert local[0] == "/bin/codex" and local[local.index("--sandbox") + 1] == "workspace-write"
    assert local[local.index("--cd") + 1] == "/repo/wt"
    assert Codex("sk").env(job("local")) == {"CODEX_API_KEY": "sk"}


def test_codex_runs_and_reports_its_tokens(data):
    final = json.dumps({"summary": "Done", "points": [{"id": 2, "status": "declined", "reason": "out of scope"}]})
    lines = [
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
        {"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 50}},
    ]
    sandbox = FakeBox("docker", lines)
    logged: list[dict] = []
    r = asyncio.run(Codex("sk").run(Job(1, 7, "Build", "sys", "m", None, sandbox), logged.append, lambda: False))
    assert (r.ok, r.summary, r.usd) == (True, "Done", None)
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (200, 1000, 50)
    assert r.points == [{"id": 2, "status": "declined", "reason": "out of scope"}]
    assert logged[0] == {"type": "tool", "tool": "command", "detail": "pytest"}
    assert sandbox.seen[2].startswith("sys\n\nBuild") and '"summary"' in sandbox.seen[2]

    failed = FakeBox("docker", [{"type": "turn.failed", "error": {"message": "quota"}}], exit_code=1)
    bad = asyncio.run(Codex("sk").run(Job(1, 7, "Build", "sys", "m", None, failed), logged.append, lambda: False))
    assert (bad.ok, bad.error) == (False, "quota")


def test_a_report_is_found_in_an_agents_last_words():
    assert report('{"summary": "a", "points": []}') == {"summary": "a", "points": []}
    assert report('All done.\n```json\n{"summary": "b", "points": []}\n```') == {"summary": "b", "points": []}
    assert report("No report") is None
    assert isinstance(Result(ok=True, summary=""), Result)


# --- Approvals ---


def test_file_work_in_the_worktree_goes_ahead_and_the_rest_is_asked(tmp_path):
    wt = str(tmp_path)
    assert approvals.automatic("Edit", {"file_path": f"{wt}/src/a.py"}, wt)
    assert approvals.automatic("Read", {"file_path": "src/a.py"}, wt)
    assert approvals.automatic("Grep", {"pattern": "x"}, wt)
    assert not approvals.automatic("Read", {"file_path": f"{tmp_path.parent}/secrets.env"}, wt)
    assert not approvals.automatic("Write", {"file_path": f"{wt}/../escape.py"}, wt)
    assert not approvals.automatic("Bash", {"command": "ls"}, wt)
    assert not approvals.automatic("WebFetch", {"url": "https://example.com"}, wt)
    assert approvals.describe("Bash", {"command": "rm -rf build", "description": "clean"}) == "rm -rf build"
    assert approvals.describe("WebFetch", {"url": "https://example.com", "prompt": "x"}) == "https://example.com"


@pytest.fixture
def running(data, git_repo, monkeypatch):
    """A running task's stage, as the agent's approvals see it."""
    monkeypatch.setattr(approvals, "POLL_SECONDS", 0.01)
    conn = db.connect()
    t = service.create_task(conn, str(git_repo), "x")
    with conn:
        repo.update_task(conn, t.id, {"status": "running"})
    yield conn, t, service.stages(conn, t.id)[0]
    conn.close()


def test_the_agent_waits_for_the_users_answer(running):
    conn, t, stage = running

    async def scenario():
        asked = asyncio.create_task(approvals.ask(t.id, stage.id, "Bash", "make test"))
        while not service.approvals(conn, t.id):
            await asyncio.sleep(0.01)
        (pending,) = service.approvals(conn, t.id)
        assert (pending.tool, pending.detail) == ("Bash", "make test")
        service.answer_approval(conn, pending.id, False, "not on my machine")
        return await asked

    assert asyncio.run(scenario()) == (False, "not on my machine")
    with pytest.raises(Exception, match="already denied"):
        service.answer_approval(conn, 1, True)


def test_a_stopped_task_denies_what_it_was_waiting_for(running):
    conn, t, stage = running
    service.pause(conn, t.id, "reached max_usd")
    assert asyncio.run(approvals.ask(t.id, stage.id, "Bash", "ls")) == (False, "the task stopped")
    assert repo.get_approval(conn, 1).status == "denied"


def test_claude_codes_permission_prompt(running, monkeypatch, tmp_path):
    conn, t, stage = running
    monkeypatch.setenv("FACTORY_WORKTREE", str(tmp_path))
    monkeypatch.setenv("FACTORY_TASK", str(t.id))
    monkeypatch.setenv("FACTORY_STAGE", str(stage.id))

    async def scenario():
        async with Client(approvals.mcp) as client:
            edit = await client.call_tool("approve", {"tool_name": "Edit", "input": {"file_path": "a.py"}})
            asked = asyncio.create_task(client.call_tool("approve", {"tool_name": "Bash", "input": {"command": "ls"}}))
            while not service.approvals(conn, t.id):
                await asyncio.sleep(0.01)
            service.answer_approval(conn, service.approvals(conn, t.id)[0].id, True)
            return edit, await asked

    edit, bash = asyncio.run(scenario())
    assert json.loads(edit.content[0].text) == {"behavior": "allow", "updatedInput": {"file_path": "a.py"}}
    assert json.loads(bash.content[0].text)["behavior"] == "allow"


# --- Git ---


def test_a_worktree_on_its_branch_commits_and_goes_leaving_the_branch(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=project, check=True)
    wt = tmp_path / "wt"
    base = git.add_worktree(str(project), wt, "factory/1-x", "HEAD")
    assert git.head(str(wt)) == base
    assert not git.commit(str(wt), "nothing")
    (wt / "a.txt").write_text("a\nb\n")
    assert git.commit(str(wt), "add a")
    assert git.changed(str(wt), base) == [git.Changed("a.txt", 2, 0)]
    assert "+a\n+b" in git.diff(str(wt), base, 10_000)

    git.remove_worktree(str(project), str(wt))
    assert not wt.exists() and git.has_branch(str(project), "factory/1-x")
    # Made again where it was, on the same branch, as a resume does.
    git.add_worktree(str(project), wt, "factory/1-x", "HEAD")
    assert (wt / "a.txt").exists()
