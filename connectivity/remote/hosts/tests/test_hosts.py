"""The plugin's own hosts: imported once from the SSH config, which is never written; kept in its database with
their names readable and their details encrypted with the key lo9i gives it; added, changed and deleted there."""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from hosts import db, service, sshconfig
from hosts.db import DuplicateHostError
from hosts.details import Details, InvalidHostError, config_block, parse
from hosts.errors import LockedError, UnknownHostError
from hosts.tunnels import Tunnel

SECRET = "correct horse battery staple"


def _rows() -> list[tuple]:
    conn = sqlite3.connect(db.default_path())
    try:
        return conn.execute("SELECT * FROM hosts").fetchall()
    finally:
        conn.close()


async def test_the_first_look_imports_the_ssh_configs_hosts_once_and_never_writes_to_it(config):
    before = (config.read_text(), config.stat().st_mtime_ns)
    hosts = await service.all_hosts()
    assert [(h.name, str(h.address)) for h in hosts] == [
        ("nas", "groso@192.168.0.10:2222"),
        ("pi", "lo9i@lo9i-pi.local"),
    ]
    assert (config.read_text(), config.stat().st_mtime_ns) == before
    # After that the list is the only source: the config's changes don't reach it on their own.
    config.write_text("Host other\n  HostName other.local\n")
    assert [h.name for h in await service.all_hosts()] == ["nas", "pi"]
    await service.delete_host("nas")
    await service.delete_host("pi")
    assert await service.all_hosts() == []  # Not imported again.


async def test_names_are_readable_and_details_and_passwords_encrypted(config):
    await service.add_host("box", Details("box.example", "admin", 2200, "~/.ssh/box", [("Compression", "yes")], SECRET))
    stored = " ".join(str(v) for row in _rows() for v in row)
    assert "box" in stored and "pi" in stored
    for secret in ("box.example", "admin", "2200", "~/.ssh/box", "Compression", SECRET, "lo9i-pi.local", "groso"):
        assert secret not in stored
    assert (await service.details("box")).password == SECRET


async def test_reimporting_adds_only_what_the_list_doesnt_have(config):
    await service.all_hosts()
    changed = Details("pi.lan", "pi", 22)
    await service.change_host("pi", "pi", changed)
    await service.delete_host("nas")
    config.write_text(config.read_text() + "\nHost web\n  HostName web.local\n")
    assert await service.reimport() == ["nas", "web"]
    assert await service.details("pi") == changed  # Left as the user made it.
    assert await service.reimport() == []


async def test_a_database_from_before_keeps_what_the_user_set_on_its_hosts(config):
    conn = db.connect()
    db.set_hidden(conn, "nas", True)
    db.connected(conn, "pi")
    db.set_hidden(conn, "gone", True)  # A host the config doesn't have any more.
    conn.close()
    hosts = {h.name: h for h in await service.all_hosts()}
    assert set(hosts) == {"nas", "pi"} and hosts["nas"].hidden and hosts["pi"].last_connected


async def test_without_the_key_or_with_another_nothing_is_read(config, monkeypatch):
    await service.all_hosts()
    monkeypatch.delenv("LO9I_DATA_KEY")
    with pytest.raises(LockedError, match="lo9i didn't give the hosts plugin its key"):
        await service.all_hosts()
    monkeypatch.setenv("LO9I_DATA_KEY", Fernet.generate_key().decode())
    with pytest.raises(LockedError, match="encrypted with another key"):
        await service.all_hosts()


async def test_renaming_a_host_takes_its_tunnels_and_deleting_it_deletes_them(config):
    await service.all_hosts()
    saved = await service.save_tunnel(Tunnel(None, "pi", "Web", "local", 8080, "localhost", 80))
    await service.change_host("pi", "raspberry", await service.details("pi"))
    assert [t.id for t in await service.tunnels("raspberry")] == [saved.id]
    with pytest.raises(UnknownHostError):
        await service.host("pi")
    with pytest.raises(DuplicateHostError, match="There's a host named nas already"):
        await service.change_host("raspberry", "nas", await service.details("raspberry"))
    with pytest.raises(DuplicateHostError):
        await service.add_host("nas", Details("x"))
    await service.delete_host("raspberry")
    assert await service.tunnels() == []


def test_a_host_from_the_form():
    options = "# a comment\n\nServerAliveInterval=30\n"
    name, found = parse(" pi ", " pi.local ", "lo9i", "", "~/.ssh/id pi", options, "pw")
    assert name == "pi"
    assert found == Details("pi.local", "lo9i", 22, "~/.ssh/id pi", [("ServerAliveInterval", "30")], "pw")
    # Written for ssh, a path with a space quoted; the password never.
    assert config_block(name, found) == (
        "Host pi\n  HostName pi.local\n  User lo9i\n  Port 22\n"
        '  IdentityFile "~/.ssh/id pi"\n  ServerAliveInterval 30\n'
    )


@pytest.mark.parametrize(
    ("fields", "wrong"),
    [
        ({"name": "work box"}, "name"),
        ({"name": "-oProxyCommand=x"}, "name"),
        ({"name": "me@pi"}, "name"),
        ({"hostname": ""}, "hostname"),
        ({"hostname": "-oProxyCommand=x"}, "hostname"),
        ({"user": "a b"}, "user"),
        ({"port": "70000"}, "port"),
        ({"identity_file": "-----BEGIN OPENSSH PRIVATE KEY-----"}, "identity_file"),
        ({"options": "Host other"}, "options"),
        ({"options": "Include ~/.ssh/config"}, "options"),
        ({"options": "Match all"}, "options"),
        ({"options": "HostName elsewhere"}, "options"),
        ({"options": "just-a-word"}, "options"),
        ({"password": "two\nlines"}, "password"),
    ],
)
def test_what_cant_be_used_says_which_field(fields, wrong):
    values = {"name": "pi", "hostname": "pi.local"} | fields
    with pytest.raises(InvalidHostError) as e:
        parse(**values)
    assert set(e.value.errors) == {wrong}


def test_importing_keeps_only_the_key_files_path(config, tmp_path):
    key = tmp_path / "id_pi"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret material\n")
    config.write_text(f"Host pi\n  HostName pi.local\n  IdentityFile {key}\n")
    [(_, found)] = sshconfig.hosts()
    assert found.identity_file == str(key) and "secret material" not in found.to_json()
