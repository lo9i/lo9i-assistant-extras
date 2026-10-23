import json

import pytest
from conftest import FakeSsh, answers, matched, process_info
from mcp import Client

from hosts import db, herdr, mcp_server
from hosts.mcp_server import mcp


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
    pi, nas = listed
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
    assert result.is_error and "busy running `htop`" in result.content[0].text
    ssh.answers[herdr._SCREEN] = answers(127)
    result = await call("read_screen", {"host": "pi"})
    assert result.is_error and "Herdr isn't installed on pi" in result.content[0].text


async def test_the_screen_is_what_the_user_sees(ssh: FakeSsh):
    ssh.answers[herdr._SCREEN] = answers(out="me@pi:~$ uptime\n 10:00 up 3 days\nme@pi:~$ ")
    result = await call("read_screen", {"host": "pi", "lines": 30})
    assert "up 3 days" in result.content[0].text
    assert ssh.calls[-1][2] == ("30",)
