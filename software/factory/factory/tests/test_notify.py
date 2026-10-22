"""Telling the user through lo9i, over a fake connection that keeps what it's sent."""

import asyncio

import pytest

from lo9i_factory import db, notify, repo, service
from lo9i_factory.notify import FINISHED_WORK, STARTED, Notifier

TELEGRAM = {"user_id": "owner", "thread_id": "t-1", "channel": "telegram"}
WEB = {"user_id": "owner", "thread_id": "t-2", "channel": "web"}


class Session:
    """lo9i's end of the connection."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    async def send_notification(self, notification) -> None:
        if self.fail:
            raise ConnectionError("closed")
        self.sent.append((notification.method, notification.params))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A database file: the notifier opens its own connections."""
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "factory.db"))
    conn = db.connect()
    yield conn
    conn.close()


@pytest.fixture
def lo9i():
    notifier, session = Notifier(), Session()
    notifier.attach(session)
    return notifier, session


def sync(notifier: Notifier) -> None:
    asyncio.run(notifier.sync())


def finished(session: Session) -> list[dict]:
    return [p for m, p in session.sent if m == FINISHED_WORK]


def started(session: Session) -> list[str]:
    return [p["work"] for m, p in session.sent if m == STARTED]


def decided(conn, task_id: int) -> int:
    stage = service.stages(conn, task_id)[-1].id
    with conn:
        return repo.insert_decision(
            conn, task_id=task_id, stage_id=stage, round=1, model="copilot:judge", kind="winner",
            rationale="Smallest change that does it", body={"winner": "architect", "result": "Spec"},
        )  # fmt: skip


def test_an_unfinished_task_holds_its_work_so_lo9i_keeps_the_server(conn, git_repo, lo9i):
    notifier, session = lo9i
    t = service.create_task(conn, str(git_repo), "Add a cache")
    service.driven_from(conn, t.id, TELEGRAM)
    sync(notifier)
    sync(notifier)
    assert session.sent == [(STARTED, {"work": "task-1"})]


def test_a_checkpoint_is_told_once_in_the_chat_the_task_came_from(conn, git_repo, lo9i):
    notifier, session = lo9i
    t = service.create_task(conn, str(git_repo), "Add a cache")
    service.driven_from(conn, t.id, TELEGRAM)
    sync(notifier)
    service.open_checkpoint(conn, t.id, decided(conn, t.id))
    sync(notifier)
    sync(notifier)

    (told,) = finished(session)
    assert told["work"] == "task-1" and told["origin"] == TELEGRAM
    assert told["ok"] is True and told["follow_up"] is True
    assert told["text"] == (
        "Task 1, Add a cache: the spec waits for you. The judge: Smallest change that does it"
        " Approve it, or reject it saying what to change."
    )
    assert len(told["text"]) <= 300
    # Still waiting: its work is held again.
    assert started(session) == ["task-1", "task-1"]


def test_the_agents_requests_pauses_failures_and_the_end_are_told(conn, git_repo, lo9i):
    notifier, session = lo9i
    t = service.create_task(conn, str(git_repo), "Add a cache")
    service.driven_from(conn, t.id, TELEGRAM)
    with conn:
        repo.update_task(conn, t.id, {"status": "running"})
        repo.insert_approval(conn, t.id, service.stages(conn, t.id)[0].id, "Bash", "npm install left-pad")
    sync(notifier)
    service.answer_approval(conn, 1, True)
    service.pause(conn, t.id, "reached max_usd: spent $10.01 of $10.00")
    sync(notifier)
    service.resume(conn, t.id)
    service.pause(conn, t.id, "the architect's model failed: 503")
    sync(notifier)
    service.cancel(conn, t.id)
    sync(notifier)
    sync(notifier)

    texts = [(p["ok"], p["follow_up"], p["text"]) for p in finished(session)]
    assert texts == [
        (True, True, "Task 1, Add a cache: the agent asks to use Bash: npm install left-pad Allow it or deny it?"),
        (False, False, "Task 1, Add a cache is paused: reached max_usd: spent $10.01 of $10.00"),
        (False, False, "Task 1, Add a cache is paused: the architect's model failed: 503"),
        (True, False, "Task 1, Add a cache was cancelled."),
    ]
    # Cancelled: its work isn't held again.
    assert session.sent[-1][0] == FINISHED_WORK


def test_a_task_driven_only_from_the_page_is_told_where_the_user_last_was(conn, git_repo, lo9i):
    notifier, session = lo9i
    chat = service.create_task(conn, str(git_repo), "From chat")
    service.driven_from(conn, chat.id, WEB)
    page = service.create_task(conn, str(git_repo), "From the page")
    service.fail(conn, page.id, "the factory failed: boom")
    sync(notifier)
    assert finished(session)[0]["origin"] == WEB and "From the page failed" in finished(session)[0]["text"]


def test_without_any_conversation_nothing_is_sent(conn, git_repo, lo9i):
    notifier, session = lo9i
    t = service.create_task(conn, str(git_repo), "x")
    service.fail(conn, t.id, "boom")
    sync(notifier)
    assert session.sent == []
    assert repo.told(conn, t.id) == {"failed"}


def test_a_deleted_task_ends_its_work_quietly(conn, git_repo, lo9i):
    notifier, session = lo9i
    t = service.create_task(conn, str(git_repo), "x")
    service.driven_from(conn, t.id, TELEGRAM)
    sync(notifier)
    service.delete_task(conn, t.id)
    sync(notifier)
    assert finished(session) == [
        {"work": "task-1", "origin": TELEGRAM, "ok": True, "text": "Task 1 was deleted.", "follow_up": False}
    ]


def test_nothing_is_sent_before_lo9i_calls_and_a_new_connection_holds_the_work_again(conn, git_repo):
    notifier = Notifier()
    t = service.create_task(conn, str(git_repo), "x")
    service.driven_from(conn, t.id, TELEGRAM)
    sync(notifier)  # No call yet: no connection to talk on.

    first = Session()
    notifier.attach(first)
    sync(notifier)
    second = Session()
    notifier.attach(second)  # lo9i reconnected: it knows of no work.
    sync(notifier)
    assert (started(first), started(second)) == (["task-1"], ["task-1"])


def test_a_lost_connection_waits_for_the_next(conn, git_repo):
    notifier = Notifier()
    notifier.attach(Session(fail=True))
    t = service.create_task(conn, str(git_repo), "x")
    service.driven_from(conn, t.id, TELEGRAM)
    with pytest.raises(ConnectionError):
        sync(notifier)
    assert notifier.session is None


def test_notices_are_short(conn, git_repo):
    assert notify.short("word " * 200).endswith("…") and len(notify.short("word " * 200)) == 280
