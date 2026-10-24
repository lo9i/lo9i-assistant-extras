"""The page, over tasks the runner took through their stages on fake models."""

import re
import tempfile
from pathlib import Path

import pytest
from fakes import FakeAgent, Team, fixes, on_fakes, once
from fastapi.testclient import TestClient

from lo9i_factory import approvals, db, repo, service, settings
from lo9i_factory.providers import Providers
from lo9i_factory.runner import Runner
from lo9i_factory.web.app import create_app, listen

HX = {"HX-Request": "true"}


@pytest.fixture
def conn(tmp_path, monkeypatch, project):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "data" / "factory.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(approvals, "POLL_SECONDS", 0.01)
    (tmp_path / "data").mkdir()
    conn = db.connect()
    on_fakes(conn)
    settings.update(
        conn,
        {
            "run_mode": "local",
            "models.implementer": "anthropic:claude-sonnet-5-5",
            f"repos.{project}": {"test_command": "python3 -c 'print(\"full ok\")'"},
        },
    )
    yield conn
    conn.close()


@pytest.fixture
def client(conn):
    return TestClient(create_app())


@pytest.fixture
def team():
    return Team()


@pytest.fixture
def runner(team):
    return Runner(Providers(env={}, clients={"copilot": team}), make_agent=FakeAgent())


def headings(page: str) -> list[str]:
    return re.findall(r'<section class="column" id="column-(\w+)">\s*<h2>([^<]+)', page)


def test_the_board_has_a_column_per_stage(client):
    page = client.get("/", headers={"X-Forwarded-Prefix": "/factory"}).text
    assert '<base href="/factory/">' in page
    assert headings(page) == [
        ("waiting", "Waiting for you "),
        ("spec", "Spec "),
        ("plan", "Plan "),
        ("build", "Build "),
        ("review", "Review "),
        ("done", "Done "),
    ]
    # No dragging: cards move only by approve and reject.
    assert "draggable" not in page
    assert client.get("/static/app.css", headers={"X-Forwarded-Prefix": "/factory"}).status_code == 200


def test_a_new_task_opens_its_page_and_its_card_waits_for_you(client, conn, project, runner):
    r = client.post("/tasks", headers=HX, data={"repo": str(project), "description": "Add a search cache"})
    assert r.headers["HX-Redirect"] == "/tasks/1"
    queued = client.get("/").text
    assert queued.index('href="tasks/1"') > queued.index('id="column-spec"')

    once(runner, conn)
    board = client.get("/").text
    card = board[board.index('href="tasks/1"') :]
    assert board.index('href="tasks/1"') < board.index('id="column-spec"')
    assert "Add a search cache" in card and "Waiting for you" in card and ">spec<" in card


def test_a_bad_task_keeps_what_was_typed(client, tmp_path):
    r = client.post("/tasks", headers=HX, data={"repo": str(tmp_path), "description": "Add a cache"})
    assert r.headers["HX-Retarget"] == "#new-task-form"
    assert "isn&#39;t a git repository" in r.text and ">Add a cache</textarea>" in r.text


def test_a_checkpoint_shows_the_judges_decision_to_approve_or_reject(client, conn, project, runner, team):
    team.script[("architect", "proposal")] = [
        {"proposal": "## Cache\n\nUse an **LRU**. <script>alert(1)</script>", "risks": ["stale results"]}
    ]
    t = service.create_task(conn, str(project), "Add a search cache")
    once(runner, conn)

    page = client.get(f"/tasks/{t.id}").text
    assert "Your call: the spec" in page and "Approve" in page and "Reject: do it again" in page
    # The debate as a chat: the architect's bubble with its model, Markdown shown, HTML not run.
    assert '<span class="badge role-architect">architect</span>' in page
    assert '<span class="model mono">copilot:architect</span>' in page
    assert "<h2>Cache</h2>" in page and "<strong>LRU</strong>" in page
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page
    assert "picked the architect" in page and "Simplest" in page and "Final spec" in page
    # Stopped, so nothing reads it again.
    assert 'hx-trigger="every' not in page

    assert client.post(f"/tasks/{t.id}/reject", headers=HX, data={"comment": " "}).status_code == 400
    assert client.post(f"/tasks/{t.id}/approve", headers=HX, data={"comment": "Good"}).headers["HX-Refresh"]
    assert service.get_task(conn, t.id).stage == "plan"
    assert "You approved it: “Good”." in client.get(f"/tasks/{t.id}").text


def test_questions_answers_and_votes_sit_under_what_they_reply_to(client, conn, project, runner, team):
    team.script[("security", "proposal")] = [
        {"proposal": "Sign the keys", "risks": [], "questions": [{"to": "tester", "question": "Load tests?"}]}
    ]
    t = service.create_task(conn, str(project), "Add a search cache")
    once(runner, conn)
    service.approve(conn, t.id)
    once(runner, conn)

    page = client.get(f"/tasks/{t.id}").text
    security = page[page.index("Sign the keys") :]
    replies = security[security.index('<div class="replies">') :]
    assert replies.index("asks tester") < replies.index("Load tests?") < replies.index("answers")
    assert "votes for security" in page and "Round 1" in page and "Your call: the plan" in page


def test_a_running_task_reads_its_live_part_again(client, conn, project):
    t = service.create_task(conn, str(project), "x")
    with conn:
        repo.update_task(conn, t.id, {"status": "running"})
    page = client.get(f"/tasks/{t.id}").text
    assert f'hx-get="tasks/{t.id}/live" hx-trigger="every 3s"' in page
    live = client.get(f"/tasks/{t.id}/live", headers=HX)
    assert live.text.strip().startswith('<div id="live"') and "<html" not in live.text
    # The comment box stays outside it, so typing isn't lost.
    assert page.index('class="comment-box"') > page.index("</aside>")

    service.pause(conn, t.id, "reached max_usd")
    assert client.get(f"/tasks/{t.id}/live", headers=HX).headers["HX-Refresh"] == "true"


def test_a_task_shows_its_progress_and_hides_the_debate_behind_a_toggle(client, conn, project, runner):
    t = service.create_task(conn, str(project), "Add a search cache")
    once(runner, conn)
    service.approve(conn, t.id)
    once(runner, conn)

    page = client.get(f"/tasks/{t.id}").text
    # The steps, then what it's doing now, then your call: above the stages.
    assert '<li class="step done">' in page and '<li class="step waiting current">' in page
    assert "Review &amp; tests" in page and "Final diff" in page
    assert '<p class="now-text">Waiting for your approval of the plan</p>' in page
    assert page.index('class="stepper"') < page.index('class="panel now') < page.index('id="checkpoint"')
    assert page.index('id="checkpoint"') < page.index('<section class="stage')
    # The spec's outcome in short; every debate collapsed, kept so across refreshes.
    spec = page[page.index(f'id="stage-{service.stages(conn, t.id)[0].id}"') :]
    assert "picked the architect" in spec and "You approved it." in spec
    assert f'<details class="discussion" data-keep="discussion-{service.stages(conn, t.id)[0].id}">' in page
    assert (
        "Show the full discussion" in page and '<details class="discussion" data-keep="discussion-1" open' not in page
    )
    assert "sessionStorage" in page and "htmx:afterSwap" in page


def test_a_debate_under_way_shows_where_it_stands(client, conn, project):
    t = service.create_task(conn, str(project), "Add a search cache")
    with conn:
        stage = service.stages(conn, t.id)[0].id
        repo.update_stage(conn, stage, {"status": "approved"})
        stage = repo.insert_stage(conn, t.id, "plan")
        repo.update_stage(conn, stage, {"status": "running", "started_at": repo.now(), "round": 2})
        repo.update_task(conn, t.id, {"stage": "plan", "status": "running"})
        repo.insert_message(conn, task_id=t.id, stage_id=stage, round=1, role="architect", type="proposal",
                            body={"proposal": "## Cache\n\nAn LRU in front of the search.", "risks": []})  # fmt: skip
        repo.insert_decision(conn, task_id=t.id, stage_id=stage, round=1, model="m", kind="continue",
                             rationale="They disagree on the TTL")  # fmt: skip

    live = client.get(f"/tasks/{t.id}/live", headers=HX).text
    assert "Round 2 of the plan: the architect, security agent and tester are critiquing" in live
    assert '<span class="spinner"></span>' in live and "in this stage" in live
    assert "The proposal" in live and "An LRU in front of the search." in live
    assert "Where it stands" in live and "They disagree on the TTL" in live and "not agreed yet" in live


def test_the_final_call_says_when_nothing_changed_and_when_the_fix_rounds_ran_out(client, conn, project):
    settings.update(conn, {"max_fix_loops": 0})
    team = Team({("judge", "review_verdict"): [fixes("Write the review in docs/review.md")]})
    runner = Runner(Providers(env={}, clients={"copilot": team}), make_agent=FakeAgent(files={}))
    t = service.create_task(conn, str(project), "Read the plan and discuss it")
    for _ in ("spec", "plan"):
        once(runner, conn)
        service.approve(conn, t.id)
    once(runner, conn)

    page = client.get(f"/tasks/{t.id}").text
    call = page[page.index('id="checkpoint"') :]
    assert "This task changed no files, so approving it changes nothing in your repository" in call
    assert "the 0 rounds of fixes" in call and "used up, so it stopped here" in call
    assert "asked for fixes" in call and "Write the review in docs/review.md" in call


def test_comments_resume_cancel_and_delete(client, conn, project):
    t = service.create_task(conn, str(project), "x")
    assert client.post(f"/tasks/{t.id}/comment", headers=HX, data={"text": "Only the search page"}).status_code == 200
    service.pause(conn, t.id, "reached max_usd")
    page = client.get(f"/tasks/{t.id}").text
    assert "reached max_usd" in page and "Resume" in page and "Only the search page" in page
    client.post(f"/tasks/{t.id}/resume", headers=HX)
    assert service.get_task(conn, t.id).status == "queued"
    client.post(f"/tasks/{t.id}/cancel", headers=HX)
    assert service.get_task(conn, t.id).status == "cancelled"
    assert client.post(f"/tasks/{t.id}/cancel", headers=HX).status_code == 409
    assert client.post(f"/tasks/{t.id}/delete", headers=HX).headers["HX-Redirect"] == "/"
    assert client.get(f"/tasks/{t.id}").status_code == 404


def test_the_final_diff_with_its_tests_and_a_link_to_the_editor(client, conn, project, runner):
    t = service.create_task(conn, str(project), "Add a search cache")
    for _ in ("spec", "plan"):
        once(runner, conn)
        service.approve(conn, t.id)
    once(runner, conn)

    page = client.get(f"/tasks/{t.id}").text
    worktree = service.get_task(conn, t.id).worktree
    assert "Your call: the final diff" in page and "accepted the diff" in page
    assert f'href="vscode://file/{worktree}"' in page
    assert "cache.py" in page and '<span class="success">+2</span>' in page
    assert '<span class="add">+CACHE = {}</span>' in page
    assert "the change&#39;s tests" in page and "all tests" in page and "passed" in page
    assert "Added the cache" in page  # The agent's report.
    assert "implementer" in page and "$0.50" in page


def test_the_agent_asks_and_the_user_answers(client, conn, project):
    t = service.create_task(conn, str(project), "x")
    with conn:
        repo.update_task(conn, t.id, {"status": "running"})
        repo.insert_approval(conn, t.id, service.stages(conn, t.id)[0].id, "Bash", "rm -rf build")
    page = client.get(f"/tasks/{t.id}").text
    assert "The agent asks" in page and "rm -rf build" in page
    assert "1 to allow" in client.get("/").text
    client.post(f"/tasks/{t.id}/approvals/1", headers=HX, data={"allow": "no", "reason": "keep it"})
    assert (repo.get_approval(conn, 1).status, repo.get_approval(conn, 1).reason) == ("denied", "keep it")


def test_settings_are_shown_and_changed(client, conn, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    page = client.get("/settings").text
    assert 'name="models.architect" value="anthropic:claude-opus-5-5"' not in page  # on fakes here
    assert 'value="copilot:architect"' in page and "anthropic: no key" in page

    form = {f"models.{r}": settings.load(conn).models[r] for r in settings.ROLES}
    form |= {"agent": "codex", "run_mode": "docker", "editor": "cursor://file/{path}", "max_rounds": "4",
             "max_fix_loops": "1", "max_usd": "2.5", "max_parallel": "1", "full_test_minutes": "3"}  # fmt: skip
    assert client.post("/settings", headers=HX, data=form).headers["HX-Refresh"] == "true"
    s = settings.load(conn)
    assert (s.agent, s.max_rounds, s.max_usd, s.editor) == ("codex", 4, 2.5, "cursor://file/{path}")

    bad = client.post("/settings", headers=HX, data={**form, "max_rounds": "lots"})
    assert bad.status_code == 400 and "max_rounds must be a whole number" in bad.text


def test_prompts_are_edited_and_reset(client, conn):
    client.post("/settings/prompts/tester", headers=HX, data={"prompt": "Only fast tests."})
    assert settings.load(conn).prompts["tester"] == "Only fast tests."
    assert "Reset to default" in client.get("/settings").text
    client.post("/settings/prompts/tester", headers=HX, data={"prompt": "x", "reset": "1"})
    assert settings.load(conn).prompts["tester"] == settings.default_prompt("tester")


def test_repositories_are_added_and_removed(client, conn, project):
    client.post("/settings/repos", headers=HX, data={"path": "/work/app", "test_command": "make test"})
    assert settings.load(conn).repo("/work/app").test_command == "make test"
    assert 'value="make test"' in client.get("/settings").text
    client.post("/settings/repos", headers=HX, data={"path": "/work/app", "remove": "1"})
    assert "/work/app" not in settings.load(conn).repos


def test_a_bad_prefix_is_ignored(client):
    assert '<base href="/">' in client.get("/", headers={"X-Forwarded-Prefix": '/x"><script>'}).text


def test_the_socket_is_only_the_users():
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sock = listen(str(Path(d) / "web.sock"))
        try:
            assert (Path(d) / "web.sock").stat().st_mode & 0o777 == 0o600
        finally:
            sock.close()
