import asyncio
import os
import signal
import sys

import pytest
from conftest import REMOTE_UP, UP, FakeProcess, FakeSpawn, settled

from hosts import tunnels
from hosts.tunnels import InvalidTunnelError, Tunnel, Tunnels, command, explain, parse

LOCAL = Tunnel(1, "pi", "Database", "local", 15432, "db.internal", 5432)
REMOTE = Tunnel(2, "pi", "Dev server", "remote", 9000, "localhost", 3000)
SOCKS = Tunnel(3, "pi", "Proxy", "socks", 1080)
TIMED_OUT = "ssh: connect to host lo9i-pi.local port 22: Operation timed out"


# What the user types


def test_a_tunnel_from_the_form():
    t = parse("pi", " Database ", "local", "15432", "db.internal", " 5432")
    assert t == Tunnel(None, "pi", "Database", "local", 15432, "db.internal", 5432)
    assert parse("pi", "Web", "remote", 9000, "", 3000).target_host == "localhost"
    socks = parse("pi", "Proxy", "socks", "1080", "ignored", "99", id=7)
    assert (socks.id, socks.target_host, socks.target_port) == (7, "", None)


@pytest.mark.parametrize(
    ("fields", "wrong"),
    [
        (("", "local", "80", "localhost", "80"), {"name"}),
        (("x" * 41, "local", "80", "localhost", "80"), {"name"}),
        (("Web", "tcp", "80", "localhost", "80"), {"kind"}),
        (("Web", "local", "0", "localhost", "65536"), {"listen_port", "target_port"}),
        (("Web", "local", "eighty", "localhost", ""), {"listen_port", "target_port"}),
        # Nothing typed becomes an ssh option or a second forward.
        (("Web", "local", "80", "-oProxyCommand=sh", "80"), {"target_host"}),
        (("Web", "remote", "80", "a:80:b", "80"), {"target_host"}),
        (("Web", "local", "80", "two words", "80"), {"target_host"}),
    ],
)
def test_what_to_change_is_said_by_field(fields, wrong):
    with pytest.raises(InvalidTunnelError) as raised:
        parse("pi", *fields)
    assert set(raised.value.errors) == wrong


def test_each_type_says_where_it_goes():
    assert LOCAL.describe() == "localhost:15432 → db.internal:5432 on pi"
    assert REMOTE.describe() == "pi:9000 → localhost:3000 on this Mac"
    assert SOCKS.describe() == "SOCKS proxy at localhost:1080, through pi"


# The ssh command


def test_the_command_forwards_through_the_configs_host(config):
    args = command(LOCAL)
    assert args[:3] == ["ssh", "-F", str(config)]
    assert args[-2:] == ["--", "pi"]
    assert args[args.index("-L") + 1] == "127.0.0.1:15432:db.internal:5432"
    for option in ("BatchMode=yes", "ExitOnForwardFailure=yes", "ServerAliveInterval=15", "ServerAliveCountMax=3"):
        assert args[args.index(option) - 1] == "-o"
    # No shell, no terminal, its own connection whatever the config shares.
    assert {"-N", "-T", "-v", "ControlMaster=no", "ControlPath=none"} <= set(args)


def test_local_and_socks_listen_only_on_this_mac():
    assert tunnels.forward(LOCAL) == ["-L", "127.0.0.1:15432:db.internal:5432"]
    assert tunnels.forward(SOCKS) == ["-D", "127.0.0.1:1080"]
    assert tunnels.forward(REMOTE) == ["-R", "9000:localhost:3000"]
    ipv6 = Tunnel(4, "pi", "v6", "local", 8080, "::1", 80)
    assert tunnels.forward(ipv6) == ["-L", "127.0.0.1:8080:[::1]:80"]


def test_no_config_set_means_the_users_own(monkeypatch):
    monkeypatch.delenv("HOSTS_SSH_CONFIG")
    assert command(SOCKS)[:2] == ["ssh", "-v"]


# What ssh said, in plain words


@pytest.mark.parametrize(
    ("err", "said", "final"),
    [
        (TIMED_OUT, "Can't reach pi: it didn't answer.", False),
        ("ssh: Could not resolve hostname lo9i-pi.local: nodename nor servname provided", "address couldn't", False),
        ("Connection to lo9i-pi.local closed by remote host.", "it closed the connection.", False),
        ("lo9i@lo9i-pi.local: Permission denied (publickey).", "add the key to the SSH agent", True),
        ("Host key verification failed.", "host key", True),
        (
            "bind [127.0.0.1]:15432: Address already in use\nchannel_setup_fwd_listener_tcpip: cannot listen to port: "
            "15432\nCould not request local forwarding.",
            "Port 15432 is already in use on this Mac.",
            True,
        ),
        ("Error: remote port forwarding failed for listen port 15432", "pi wouldn't open port 15432", True),
        ("/Users/me/.ssh/config: line 3: Bad configuration option: hostnme", "error in your SSH config", True),
        ("something nobody expected", "ssh stopped (exit code 255).", False),
    ],
)
def test_why_it_stopped_is_short_and_plain(err, said, final):
    message, lasting = explain(LOCAL, err, 255)
    assert said in message and lasting == final
    assert "\n" not in message


# Starting, stopping, reconnecting


async def test_a_tunnel_runs_once_ssh_says_its_up_and_stops_when_switched_off(spawn: FakeSpawn):
    running = spawn.tunnels()
    assert running.status(1).state == "stopped"
    assert (await running.start(LOCAL)).state == "starting"
    await settled(lambda: running.status(1).state == "running")
    assert spawn.commands == [command(LOCAL)]
    await running.start(LOCAL)  # Already on: nothing new starts.
    await asyncio.sleep(0.01)
    assert len(spawn.started) == 1
    await running.stop(1)
    assert spawn.started[0].terminated and running.status(1).state == "stopped"


async def test_a_remote_tunnel_is_up_once_the_host_accepts_its_port(spawn: FakeSpawn):
    process = FakeProcess([UP])
    spawn.processes.append(lambda: process)
    running = spawn.tunnels()
    await running.start(REMOTE)
    await asyncio.sleep(0.01)
    assert running.status(2).state == "starting"
    process.stderr.feed_data(f"{REMOTE_UP}\n".encode())
    await settled(lambda: running.status(2).state == "running")
    await running.stop_all()


async def test_still_connected_after_a_while_counts_as_up(spawn: FakeSpawn):
    """In case an ssh says it otherwise."""
    spawn.processes.append(lambda: FakeProcess(["Authenticated to lo9i-pi.local"]))
    running = spawn.tunnels(settle_seconds=0.02)
    await running.start(SOCKS)
    await settled(lambda: running.status(3).state == "running")
    await running.stop_all()


async def test_a_dropped_connection_comes_back_on_its_own(spawn: FakeSpawn):
    first = FakeProcess([UP])
    spawn.processes.append(lambda: first)
    running = spawn.tunnels()
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "running")
    first.stderr.feed_data(b"Connection to lo9i-pi.local closed by remote host.\n")
    first.exit(255)
    await settled(lambda: len(spawn.started) == 2 and running.status(1).state == "running")
    assert spawn.waits == [1]
    assert spawn.commands[0] == spawn.commands[1]
    await running.stop_all()
    assert spawn.started[1].terminated


async def test_failing_to_connect_waits_longer_each_time(spawn: FakeSpawn):
    spawn.processes.extend([lambda: FakeProcess([TIMED_OUT], 255)] * 8)
    running = spawn.tunnels()
    await running.start(LOCAL)
    await settled(lambda: len(spawn.waits) == 8)
    assert spawn.waits == [1, 2, 5, 10, 30, 60, 60, 60]
    await settled(lambda: running.status(1).state == "running")


async def test_reconnecting_says_why_meanwhile(spawn: FakeSpawn):
    gate = asyncio.Event()

    async def held(seconds: float) -> None:
        await gate.wait()

    spawn.processes.append(lambda: FakeProcess(["debug1: Connecting to lo9i-pi.local", TIMED_OUT], 255))
    running = Tunnels(spawn, held)
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "reconnecting")
    said = "Can't reach pi: it didn't answer. It may be off, asleep or on another network. Trying again in 1 s."
    assert running.status(1).message == said
    assert running.status(1).on
    # Switched off while it waits: nothing starts again.
    await running.stop(1)
    gate.set()
    await asyncio.sleep(0.01)
    assert len(spawn.started) == 1 and running.status(1).state == "stopped"


async def test_a_tunnel_up_for_a_while_starts_its_waits_over(spawn: FakeSpawn, monkeypatch):
    def dropping() -> FakeProcess:
        return FakeProcess([UP, "Connection to lo9i-pi.local closed by remote host."], 255)

    spawn.processes.extend([dropping] * 3)
    running = spawn.tunnels()
    await running.start(LOCAL)
    await settled(lambda: len(spawn.waits) == 3)
    assert spawn.waits == [1, 2, 5]  # Dropped at once each time: waits grow.
    await running.stop_all()

    monkeypatch.setattr(tunnels, "_STEADY_SECONDS", 0)
    spawn.waits.clear()
    spawn.processes.extend([dropping] * 3)
    await running.start(LOCAL)
    await settled(lambda: len(spawn.waits) == 3)
    assert spawn.waits == [1, 1, 1]
    await running.stop_all()


@pytest.mark.parametrize(
    ("err", "said"),
    [
        ("lo9i@lo9i-pi.local: Permission denied (publickey).", "didn't accept this computer's SSH key"),
        ("bind [127.0.0.1]:15432: Address already in use\nCould not request local forwarding.", "already in use"),
    ],
)
async def test_what_trying_again_wont_fix_fails_at_once(spawn: FakeSpawn, err, said):
    spawn.processes.append(lambda: FakeProcess(err.splitlines(), 255))
    running = spawn.tunnels()
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "failed")
    assert said in running.status(1).message and not running.status(1).on
    assert spawn.waits == []
    # Switched on again, it tries again.
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "running")
    await running.stop_all()


async def test_no_ssh_on_this_mac_fails(spawn: FakeSpawn):
    async def missing(args):
        raise FileNotFoundError(2, "No such file or directory")

    running = Tunnels(missing, spawn.sleep)
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "failed")
    assert running.status(1).message == "This Mac couldn't start ssh: No such file or directory."


async def test_a_changed_tunnel_runs_again_as_changed_only_if_it_was_on(spawn: FakeSpawn):
    running = spawn.tunnels()
    await running.restart(LOCAL)
    assert spawn.commands == []
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "running")
    moved = Tunnel(1, "pi", "Database", "local", 25432, "db.internal", 5432)
    await running.restart(moved)
    await settled(lambda: running.status(1).state == "running")
    assert spawn.started[0].terminated and "127.0.0.1:25432:db.internal:5432" in spawn.commands[1]
    await running.stop_all()


async def test_stopping_everything_leaves_no_ssh_running(spawn: FakeSpawn):
    running = spawn.tunnels()
    for t in (LOCAL, REMOTE, SOCKS):
        await running.start(t)
    await settled(lambda: len(spawn.started) == 3)
    await running.stop_all()
    assert all(p.terminated for p in spawn.started)
    assert {running.status(i).state for i in (1, 2, 3)} == {"stopped"}


# A real process: what tunnels.spawn starts stops for good.

_FAKE_SSH = """
import sys, time
print("debug1: Entering interactive session.", file=sys.stderr, flush=True)
time.sleep(60)
"""


async def test_a_real_ssh_process_ends_when_its_tunnel_stops():
    started = []

    async def python(args):
        process = await tunnels.spawn([sys.executable, "-c", _FAKE_SSH])
        started.append(process)
        return process

    running = Tunnels(python)
    await running.start(LOCAL)
    await settled(lambda: running.status(1).state == "running", seconds=10)
    pid = started[0].pid
    # In the page's process group, so whatever stops the page stops it too.
    assert os.getpgid(pid) == os.getpgid(0)
    await running.stop(1)
    assert started[0].returncode == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
