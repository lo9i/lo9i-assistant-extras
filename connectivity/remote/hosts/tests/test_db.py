import sqlite3

import pytest

from hosts import db
from hosts.db import DuplicateTunnelError
from hosts.tunnels import Tunnel


def test_a_database_from_before_tunnels_gets_their_table_and_keeps_its_hosts(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE hosts (name TEXT PRIMARY KEY, hidden INTEGER NOT NULL DEFAULT 0, last_connected TEXT)")
    old.execute("INSERT INTO hosts VALUES ('nas', 1, '2026-10-01T10:00:00+00:00')")
    old.commit()
    old.close()

    conn = db.connect(path)
    assert db.hidden(conn) == {"nas"}
    assert db.last_connected(conn) == {"nas": "2026-10-01T10:00:00+00:00"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert db.tunnels(conn) == []
    conn.close()
    # Connecting again changes nothing.
    conn = db.connect(path)
    assert db.hidden(conn) == {"nas"} and conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


def test_a_new_database_has_both_tables():
    conn = db.connect(":memory:")
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"hosts", "tunnels"} <= names


def test_tunnels_are_kept_changed_and_deleted():
    conn = db.connect(":memory:")
    database = db.save_tunnel(conn, Tunnel(None, "pi", "database", "local", 15432, "db.internal", 5432))
    proxy = db.save_tunnel(conn, Tunnel(None, "pi", "Proxy", "socks", 1080))
    db.save_tunnel(conn, Tunnel(None, "nas", "Web", "remote", 9000, "localhost", 3000))
    assert database.id is not None and proxy.id is not None
    assert database == Tunnel(database.id, "pi", "database", "local", 15432, "db.internal", 5432)
    assert proxy.target_host == "" and proxy.target_port is None

    # A host's, by name whatever its case; or every host's, by host.
    assert [t.name for t in db.tunnels(conn, "pi")] == ["database", "Proxy"]
    assert [(t.host, t.name) for t in db.tunnels(conn)] == [("nas", "Web"), ("pi", "database"), ("pi", "Proxy")]

    moved = db.save_tunnel(conn, Tunnel(database.id, "pi", "Database", "local", 25432, "db.internal", 5432))
    assert moved.id == database.id and db.tunnel(conn, database.id) == moved

    db.delete_tunnel(conn, proxy.id)
    assert db.tunnel(conn, proxy.id) is None
    assert [t.name for t in db.tunnels(conn, "pi")] == ["Database"]


def test_a_hosts_tunnels_have_names_of_their_own():
    conn = db.connect(":memory:")
    db.save_tunnel(conn, Tunnel(None, "pi", "Web", "local", 8080, "localhost", 80))
    db.save_tunnel(conn, Tunnel(None, "nas", "Web", "local", 8081, "localhost", 80))  # Another host's: fine.
    with pytest.raises(DuplicateTunnelError, match="pi has a tunnel named 'Web' already"):
        db.save_tunnel(conn, Tunnel(None, "pi", "Web", "socks", 1080))
    other = db.save_tunnel(conn, Tunnel(None, "pi", "Other", "socks", 1080))
    with pytest.raises(DuplicateTunnelError):
        db.save_tunnel(conn, Tunnel(other.id, "pi", "Web", "socks", 1080))


def test_only_the_three_types_are_kept():
    conn = db.connect(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tunnels (host, name, kind, listen_port) VALUES ('pi', 'x', 'udp', 1)")
