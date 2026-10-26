import asyncio
import json
import shutil
import tempfile

import pytest
import uvicorn
from conftest import FakeSpawn, FakeSsh, answers, matched, process_info, settled
from mcp import Client

from hosts import db, herdr, mcp_server
from hosts.mcp_server import mcp
from hosts.tunnels import Tunnel
from hosts.web.app import create_app


@pytest.fixture(autouse=True)
def over_fake_ssh(monkeypatch, fake_herdr):
    monkeypatch.setattr(mcp_server, "_herdr", lambda: fake_herdr)


async def call(name: str, args: dict):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def test_only_running_a_command_needs_the_users_approval():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["list_hosts"].annotations.read_only_hint
    assert tools["read_screen"].annotations.read_only_hint
    assert not (tools["run_command"].annotations and tools["run_command"].annotations.read_only_hint)
    assert "needs the user's OK first" in tools["run_command"].description


async def test_hosts_are_the_ones_the_user_shows(config):
    conn = db.connect()
    db.set_hidden(conn, "nas", True)
    db.connected(conn, "pi")
    conn.close()
    listed = (await call("list_hosts", {"check": False})).structured_content["result"]
    assert [h["name"] for h in listed] == ["pi"]
    assert listed[0]["address"] == "lo9i@lo9i-pi.local" and listed[0]["last_connected"]
    assert "reachable" not in listed[0]


async def test_checking_says_which_answer_and_run_herdr(ssh: FakeSsh):
    sessions = json.dumps({"sessions": [{"name": "lo9i", "running": True}]})
    ssh.answers[herdr._PROBE] = lambda: (
        answers(out=sessions)() if ssh.calls[-1][0] == "pi" else answers(255, err="Connection refused")()
    )
    listed = (await call("list_hosts", {})).structured_content["result"]
    nas, pi = listed  # By name.
    assert (pi["reachable"], pi["herdr_installed"], pi["session_running"]) == (True, True, True)
    assert not nas["reachable"] and "refused the connection" in nas["problem"]


async def test_a_command_comes_back_with_its_output_and_exit_code(ssh: FakeSsh, monkeypatch):
    monkeypatch.setattr(herdr.secrets, "token_hex", lambda n: "abcd1234")
    ssh.answers[herdr._READY] = answers(out=process_info())
    typed = 'me@pi:~$ df -h /; echo "[lo9i:$?:abcd1234]"'
    ssh.answers[herdr._RUN] = answers(out=matched(f"{typed}\n/dev/root 30G 9G\n[lo9i:0:abcd1234]\nme@pi:~$"))
    result = payload(await call("run_command", {"host": "pi", "command": "df -h /"}))
    assert result == {"output": "/dev/root 30G 9G", "exit_code": 0, "still_running": False}


async def test_hidden_or_unknown_hosts_are_not_reached(ssh: FakeSsh):
    conn = db.connect()
    db.set_hidden(conn, "nas", True)
    conn.close()
    for host in ("nas", "-oProxyCommand=sh", "elsewhere"):
        result = await call("run_command", {"host": host, "command": "uptime"})
        assert result.is_error and "The hosts are: pi." in result.content[0].text
    assert ssh.calls == []


async def test_herdrs_problems_reach_the_assistant_as_errors_to_act_on(ssh: FakeSsh):
    ssh.answers[herdr._READY] = answers(out=process_info(busy="htop"))
    result = await call("run_command", {"host": "pi", "command": "uptime"})
    assert result.is_error and "still running `htop`" in result.content[0].text
    ssh.answers[herdr._SCREEN] = answers(127)
    result = await call("read_screen", {"host": "pi"})
    assert result.is_error and "Herdr isn't installed on pi" in result.content[0].text


async def test_the_screen_is_the_assistants_tab_or_the_users_pane(ssh: FakeSsh):
    ssh.answers[herdr._SCREEN] = answers(out="me@pi:~$ uptime\n 10:00 up 3 days\nme@pi:~$ ")
    result = await call("read_screen", {"host": "pi", "lines": 30})
    assert "up 3 days" in result.content[0].text
    assert ssh.calls[-1][2] == ("30", "")
    await call("read_screen", {"host": "pi", "user_pane": True})
    assert ssh.calls[-1][2] == ("0", "user")


# Tunnels: the tools reach them through the page, which runs them, over its socket.


@pytest.fixture
async def page(fake_herdr, spawn: FakeSpawn, monkeypatch):
    folder = tempfile.mkdtemp(dir="/tmp")  # A socket's path must be short.
    path = f"{folder}/page.sock"
    monkeypatch.setenv("HOSTS_WEB_SOCKET", path)
    server = uvicorn.Server(
        uvicorn.Config(create_app(fake_herdr, running=spawn.tunnels()), uds=path, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    await settled(lambda: server.started)
    yield
    server.should_exit = True
    await serving
    shutil.rmtree(folder)


def _tunnel(host: str, name: str, kind: str = "local", port: int = 15432) -> Tunnel:
    target = ("", None) if kind == "socks" else ("localhost", 5432)
    conn = db.connect()
    try:
        return db.save_tunnel(conn, Tunnel(None, host, name, kind, port, *target))
    finally:
        conn.close()


async def test_listing_tunnels_needs_no_approval_but_switching_one_does():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["list_tunnels"].annotations.read_only_hint
    for name in ("start_tunnel", "stop_tunnel", "create_tunnel", "delete_tunnel"):
        assert not (tools[name].annotations and tools[name].annotations.read_only_hint)
    assert tools["delete_tunnel"].annotations.destructive_hint


async def test_the_assistant_lists_starts_and_stops_the_users_tunnels(page, spawn: FakeSpawn):
    _tunnel("pi", "Database")
    _tunnel("pi", "Proxy", "socks", 1080)
    _tunnel("nas", "Web", port=8080)
    listed = (await call("list_tunnels", {"host": "pi"})).structured_content["result"]
    assert [(t["name"], t["type"], t["state"]) for t in listed] == [
        ("Database", "local", "stopped"),
        ("Proxy", "socks", "stopped"),
    ]
    assert listed[0]["description"] == "localhost:15432 → localhost:5432 on pi"

    started = payload(await call("start_tunnel", {"host": "pi", "tunnel": "Database"}))
    assert started["name"] == "Database" and started["state"] in ("starting", "running")
    await settled(lambda: len(spawn.started) == 1)
    assert "127.0.0.1:15432:localhost:5432" in spawn.commands[0]
    await asyncio.sleep(0.05)
    every = (await call("list_tunnels", {})).structured_content["result"]
    assert {t["name"]: t["state"] for t in every} == {"Web": "stopped", "Database": "running", "Proxy": "stopped"}

    stopped = payload(await call("stop_tunnel", {"host": "pi", "tunnel": "Database"}))
    assert stopped["state"] == "stopped" and spawn.started[0].terminated


async def test_only_the_shown_hosts_tunnels_and_ones_by_their_names(page, spawn: FakeSpawn):
    _tunnel("pi", "Database")
    _tunnel("nas", "Web")
    conn = db.connect()
    db.set_hidden(conn, "nas", True)
    conn.close()
    assert [t["name"] for t in (await call("list_tunnels", {})).structured_content["result"]] == ["Database"]
    result = await call("start_tunnel", {"host": "nas", "tunnel": "Web"})
    assert result.is_error and "The hosts are: pi." in result.content[0].text
    result = await call("start_tunnel", {"host": "pi", "tunnel": "Nope"})
    assert result.is_error and "pi has no tunnel named 'Nope'. Its tunnels are: Database." in result.content[0].text
    assert spawn.commands == []


async def test_without_the_page_the_assistant_hears_how_to_get_it_back(monkeypatch):
    monkeypatch.setenv("HOSTS_WEB_SOCKET", "/tmp/nothing-listens-here.sock")
    result = await call("list_tunnels", {})
    assert result.is_error and "The Hosts page isn't running" in result.content[0].text


async def test_the_assistant_adds_a_tunnel_the_user_asks_for(page, spawn: FakeSpawn):
    asked = {"host": "pi", "name": "Database", "type": "local", "listen_port": 15432, "target_port": 5432}
    created = payload(await call("create_tunnel", asked))
    listed = (await call("list_tunnels", {"host": "pi"})).structured_content["result"]
    assert listed == [created]
    assert created | {"id": 0} == {
        "id": 0, "host": "pi", "name": "Database", "type": "local", "listen_port": 15432,
        "target_host": "localhost", "target_port": 5432, "description": "localhost:15432 → localhost:5432 on pi",
        "state": "stopped",
    }  # fmt: skip
    assert spawn.commands == []

    proxy = {"host": "nas", "name": "Proxy", "type": "socks", "listen_port": 1080, "start": True}
    started = payload(await call("create_tunnel", proxy))
    assert "target_host" not in started and started["state"] in ("starting", "running")
    await settled(lambda: len(spawn.started) == 1)
    assert "127.0.0.1:1080" in spawn.commands[0]
    conn = db.connect()
    assert [t.name for t in db.tunnels(conn)] == ["Proxy", "Database"]
    conn.close()


@pytest.mark.parametrize(
    ("asked", "said"),
    [
        ({"host": "nope"}, "No host named 'nope'. The hosts are: nas, pi."),
        ({"name": "Database"}, "pi has a tunnel named 'Database' already."),
        ({"listen_port": 80}, "listen_port: Ports under 1024 need an administrator"),
        ({"type": "socks", "listen_port": 15432}, "listen_port: pi's tunnel 'Database' uses port 15432 on this Mac."),
        ({"host": "nas", "type": "remote", "listen_port": 9000}, "listen_port: nas's tunnel 'Dev' uses port 9000"),
        ({"target_port": None}, "target_port: A port from 1 to 65535."),
        ({"target_host": "-oProxyCommand=sh"}, "target_host: A host name or an IP address."),
    ],
)
async def test_a_tunnel_the_assistant_cant_add_says_what_to_change(page, spawn: FakeSpawn, asked, said):
    _tunnel("pi", "Database")
    _tunnel("nas", "Dev", "remote", 9000)
    base = {"host": "pi", "name": "Web", "type": "local", "listen_port": 18080, "target_port": 80, "start": True}
    result = await call("create_tunnel", base | asked)
    assert result.is_error and said in result.content[0].text
    conn = db.connect()
    assert len(db.tunnels(conn)) == 2
    conn.close()
    assert spawn.commands == []


async def test_a_remote_tunnel_may_use_a_port_another_host_uses(page):
    _tunnel("nas", "Dev", "remote", 9000)
    asked = {"host": "pi", "name": "Dev", "type": "remote", "listen_port": 9000, "target_port": 3000}
    assert payload(await call("create_tunnel", asked))["description"] == "pi:9000 → localhost:3000 on this Mac"


async def test_no_tunnel_is_added_through_a_hidden_host(page):
    conn = db.connect()
    db.set_hidden(conn, "nas", True)
    conn.close()
    asked = {"host": "nas", "name": "Proxy", "type": "socks", "listen_port": 1080}
    result = await call("create_tunnel", asked)
    assert result.is_error and "The hosts are: pi." in result.content[0].text


async def test_the_assistant_deletes_a_tunnel_stopping_it_first(page, spawn: FakeSpawn):
    _tunnel("pi", "Database")
    _tunnel("pi", "Proxy", "socks", 1080)
    payload(await call("start_tunnel", {"host": "pi", "tunnel": "Database"}))
    await settled(lambda: len(spawn.started) == 1)
    deleted = payload(await call("delete_tunnel", {"host": "pi", "tunnel": "Database"}))
    assert (deleted["name"], deleted["state"], deleted["deleted"]) == ("Database", "stopped", True)
    assert spawn.started[0].terminated
    assert [t["name"] for t in (await call("list_tunnels", {})).structured_content["result"]] == ["Proxy"]
    result = await call("delete_tunnel", {"host": "pi", "tunnel": "Database"})
    assert result.is_error and "Its tunnels are: Proxy." in result.content[0].text
