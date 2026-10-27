import json
import os
import signal
import stat
import sys
import tempfile
from pathlib import Path

import pytest
import uvloop
from conftest import FakeSsh, answers
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hosts import db, herdr
from hosts.web.app import create_app, listen
from hosts.web.terminal import Terminal

# Stands in for ssh in the terminal: says its size, echoes a line, then exits as told (sys.argv[1]),
# writing what ssh would to stderr.
_FAKE_TERMINAL = """
import os, sys
size = os.get_terminal_size(0)
print(f"size {size.columns}x{size.lines}", flush=True)
line = sys.stdin.readline().strip()
size = os.get_terminal_size(0)
print(f"got {line} at {size.columns}x{size.lines}", flush=True)
sys.stderr.write(sys.argv[2])
sys.exit(int(sys.argv[1]))
"""


def _terminal(code: int = 0, err: str = ""):
    return lambda name, options: [sys.executable, "-c", _FAKE_TERMINAL, str(code), err]


@pytest.fixture
def client(fake_herdr):
    return TestClient(create_app(fake_herdr, _terminal()))


def _hidden(name: str) -> None:
    conn = db.connect()
    db.set_hidden(conn, name, True)
    conn.close()


def test_the_table_lists_the_hosts_and_checks_each_as_it_loads(client):
    page = client.get("/", headers={"X-Forwarded-Prefix": "/hosts"}).text
    assert '<base href="/hosts/">' in page
    assert 'id="filter"' in page and page.count("<th data-sort") == 6
    # At once, without waiting for any host: each is checked after, over /statuses (table.js).
    assert 'data-host="pi"' in page and "Checking…" in page
    assert "data-checking" in page and 'hx-get="hosts/pi/row"' not in page
    assert "lo9i@lo9i-pi.local" in page and "groso@192.168.0.10:2222" in page
    assert client.get("/static/table.js", headers={"X-Forwarded-Prefix": "/hosts"}).status_code == 200


def test_a_checked_row_says_whether_it_answers_and_runs_the_session(client, ssh: FakeSsh):
    ssh.answers[herdr._PROBE] = answers(out=json.dumps({"sessions": [{"name": "lo9i", "running": True}]}))
    row = client.get("/hosts/pi/row").text
    assert "Online" in row and "Running" in row and "hosts/pi/row" not in row
    assert 'data-terminal="hosts/pi"' in row  # Double-clicking it opens its terminal (table.js).
    details = client.get("/hosts/pi/details").text  # The host on the right shows what the check found.
    assert '<span class="pill pill--accent">Online</span>' in details
    assert '<a class="btn btn--sm btn--neutral" href="hosts/pi"' in details
    ssh.answers[herdr._PROBE] = answers(127)
    assert "No Herdr" in client.get("/hosts/pi/row").text
    ssh.answers[herdr._PROBE] = answers(255, err="ssh: Could not resolve hostname lo9i-pi.local")
    row = client.get("/hosts/pi/row").text
    assert "Offline" in row and "address couldn&#39;t be found" in row
    assert "data-terminal" not in row  # Nor does double-clicking it.
    # Its terminal can't connect: Terminal is off, saying why.
    details = client.get("/hosts/pi/details").text
    assert 'href="hosts/pi"' not in details and "address couldn&#39;t be found" in details
    assert '<button class="btn btn--sm btn--neutral" disabled title="pi is offline:' in details


def test_each_hosts_row_comes_over_one_socket_as_its_check_ends_and_shows_at_once_next_time(client, ssh: FakeSsh):
    ssh.answers[herdr._PROBE] = answers(out=json.dumps({"sessions": [{"name": "lo9i", "running": True}]}))
    _hidden("nas")
    with client.websocket_connect("/statuses") as ws:
        [checked] = [ws.receive_json()]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()  # Only the hosts shown; then it closes.
    assert checked["host"] == "pi" and "Online" in checked["html"] and "data-checking" not in checked["html"]
    page = client.get("/").text
    assert "Checking…" not in page and "Online" in page  # What it found last, while it checks again.
    with client.websocket_connect("/statuses?hidden=true") as ws:
        assert {ws.receive_json()["host"], ws.receive_json()["host"]} == {"pi", "nas"}


def test_hidden_hosts_leave_the_table_until_asked_for(client):
    response = client.post("/hosts/nas/hide")
    assert response.headers["HX-Refresh"] == "true"
    page = client.get("/").text
    assert "hosts/nas" not in page and "1 hidden" in page and "Show hidden hosts" in page
    page = client.get("/?hidden=true&host=nas").text
    assert 'hx-post="hosts/nas/show"' in page and "hidden</span>" in page
    client.post("/hosts/nas/show")
    assert "hosts/nas" in client.get("/").text


def test_the_host_picked_shows_on_the_right_the_first_until_one_is(client):
    page = client.get("/").text  # By name: nas first.
    assert '<div id="details" class="column">' in page and 'data-details="nas"' in page
    assert 'hx-get="hosts/nas/tunnels" hx-trigger="load"' in page  # Its tunnels, in their own card.
    page = client.get("/?host=pi").text
    assert 'data-details="pi"' in page and 'data-details="nas"' not in page
    assert 'data-details="nas"' in client.get("/?host=elsewhere").text
    assert client.get("/hosts/elsewhere/details").status_code == 404


def test_every_host_hidden_says_how_to_bring_them_back(client):
    _hidden("pi")
    _hidden("nas")
    assert "Every host is hidden" in client.get("/").text


def test_a_host_the_config_doesnt_have_is_not_found(client):
    assert client.get("/hosts/elsewhere").status_code == 404
    assert client.post("/hosts/elsewhere/hide").status_code == 404


def test_the_terminal_page_loads_xterm_from_the_plugin(client):
    page = client.get("/hosts/pi").text
    assert 'data-host="pi"' in page
    for asset in ("xterm.js", "addon-fit.js", "terminal.js", "xterm.css"):
        assert f"static/{asset}?v=" in page
        assert client.get(f"/static/{asset}").status_code == 200


def test_the_terminal_passes_bytes_both_ways_and_its_size(client):
    with client.websocket_connect("/hosts/pi/terminal?cols=100&rows=30") as ws:
        assert b"size 100x30" in _until(ws, b"size")
        ws.send_text(json.dumps({"type": "resize", "cols": 132, "rows": 43}))
        ws.send_bytes(b"hello\r")
        assert b"got hello at 132x43" in _until(ws, b"got")
        ended = _ended(ws)
    assert ended["reason"] == "detached" and "keeps running there" in ended["message"]
    conn = db.connect()
    assert "pi" in db.last_connected(conn)
    conn.close()


@pytest.mark.parametrize(
    ("code", "err", "reason", "said"),
    [
        (127, "", "no-herdr", "Herdr isn't installed on pi."),
        (255, "ssh: connect to host lo9i-pi.local port 22: Operation timed out", "unreachable", "didn't answer"),
        (255, "lo9i@lo9i-pi.local: Permission denied (publickey).", "refused", "didn't accept"),
        (1, "", "failed", "exit code 1"),
    ],
)
def test_how_the_terminal_ended_tells_the_page_what_to_do(fake_herdr, code, err, reason, said):
    client = TestClient(create_app(fake_herdr, _terminal(code, err)))
    with client.websocket_connect("/hosts/pi/terminal") as ws:
        _until(ws, b"size")
        ws.send_bytes(b"x\r")
        ended = _ended(ws)
    assert ended["reason"] == reason and said in ended["message"]


def test_no_ssh_on_this_computer_says_so(fake_herdr):
    client = TestClient(create_app(fake_herdr, lambda name, options: ["/nonexistent/ssh"]))
    with client.websocket_connect("/hosts/pi/terminal") as ws:
        ended = ws.receive_json()
    assert ended["reason"] == "failed" and "couldn't start ssh" in ended["message"]


def test_the_terminal_reaches_only_the_configs_hosts(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/hosts/elsewhere/terminal"):
        pass


def test_the_terminal_comes_under_the_prefix_too(client):
    with client.websocket_connect("/hosts/pi/terminal", headers={"X-Forwarded-Prefix": "/hosts"}) as ws:
        assert b"size" in _until(ws, b"size")
        ws.send_bytes(b"x\r")
        _ended(ws)


async def test_leaving_the_terminal_stops_ssh_and_nothing_else():
    terminal = await Terminal.start(
        [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"], 80, 24
    )
    assert b"up" in await terminal.read()
    await terminal.stop()
    code, _ = await terminal.ended()
    assert code == -signal.SIGHUP


def test_installing_herdr_answers_when_done(client, ssh: FakeSsh):
    ssh.answers[herdr._INSTALL] = answers(out="herdr 0.9.5\n")
    with client.websocket_connect("/hosts/pi/install") as ws:
        assert ws.receive_json() == {"ok": True, "message": "Herdr is installed on pi."}
    ssh.answers[herdr._INSTALL] = answers(3)
    with client.websocket_connect("/hosts/pi/install") as ws:
        answer = ws.receive_json()
    assert not answer["ok"] and "no curl" in answer["message"]


def test_the_terminal_is_its_commands_own_under_uvicorns_loop_too():
    """uvicorn runs on uvloop, which starts processes in its own way."""

    async def started() -> bytes:
        check = "import os; print('own', os.getsid(0) == os.getpid(), os.ttyname(0) == os.ttyname(1), flush=True)"
        terminal = await Terminal.start([sys.executable, "-c", check], 80, 24)
        seen = await terminal.read()
        await terminal.ended()
        await terminal.stop()
        return seen

    assert b"own True True" in uvloop.run(started())


def test_the_socket_is_only_this_users():
    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        sock = listen(f"{folder}/web.sock")
        assert stat.S_IMODE(os.stat(f"{folder}/web.sock").st_mode) == 0o600
        sock.close()


def _until(ws, wanted: bytes) -> bytes:
    seen = b""
    while wanted not in seen:
        seen += ws.receive_bytes()
    return seen


def _ended(ws) -> dict:
    while True:
        message = ws.receive()
        if message.get("text"):
            return json.loads(message["text"])


def test_hidden_parts_stay_hidden_whatever_their_display():
    # The terminal's notice is display: flex; without this, hiding it left "Installing…" over the terminal.
    css = (Path(__file__).parent.parent / "src" / "hosts" / "web" / "static" / "app.css").read_text()
    assert "[hidden] { display: none !important; }" in css
