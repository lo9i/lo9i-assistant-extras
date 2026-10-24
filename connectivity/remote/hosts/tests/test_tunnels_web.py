import time
from urllib.parse import urlencode

import pytest
from conftest import FakeProcess, FakeSpawn, answers
from fastapi.testclient import TestClient

from hosts import db, herdr
from hosts.web.app import create_app

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def client(fake_herdr, spawn: FakeSpawn):
    # As a context: the tunnels' tasks run on its event loop from one request to the next.
    with TestClient(create_app(fake_herdr, running=spawn.tunnels())) as client:
        yield client


def _add(client, **fields) -> object:
    values = {"name": "Database", "kind": "local", "listen_port": "15432", "target_host": "db.internal",
              "target_port": "5432"} | fields  # fmt: skip
    return client.post("/hosts/pi/tunnels", content=urlencode(values), headers=FORM)


def _saved(host: str = "pi"):
    conn = db.connect()
    try:
        return db.tunnels(conn, host)
    finally:
        conn.close()


def _until(client, text: str, path: str = "/hosts/pi/tunnels") -> str:
    deadline = time.monotonic() + 3
    while True:
        page = client.get(path).text
        if text in page or time.monotonic() > deadline:
            assert text in page
            return page
        time.sleep(0.01)


def test_the_terminal_page_has_the_hosts_tunnels_beside_it(client):
    page = client.get("/hosts/pi").text
    assert 'id="tunnels-toggle"' in page and 'id="tunnels"' in page
    assert 'hx-get="hosts/pi/tunnels" hx-trigger="load"' in page
    listed = client.get("/hosts/pi/tunnels").text
    assert "No tunnels yet." in listed and 'hx-get="hosts/pi/tunnels/new"' in listed
    # The list swaps itself whole; the form goes inside its own box, which must stay for the form to post to.
    assert 'hx-target="#tunnel-form" hx-swap="innerHTML"' in listed
    form = client.get("/hosts/pi/tunnels/new").text
    assert 'hx-post="hosts/pi/tunnels"' in form and 'value="local" checked' in form


def test_a_tunnel_added_shows_in_the_list(client):
    response = _add(client)
    assert response.status_code == 200 and response.text == ""
    assert response.headers["HX-Trigger"] == "tunnels-changed"
    [saved] = _saved()
    assert (saved.name, saved.kind, saved.listen_port, saved.target_host, saved.target_port) == (
        "Database", "local", 15432, "db.internal", 5432,
    )  # fmt: skip
    listed = client.get("/hosts/pi/tunnels").text
    assert "localhost:15432 → db.internal:5432 on pi" in listed and "Off" in listed
    assert f'hx-post="hosts/pi/tunnels/{saved.id}/start"' in listed and 'aria-checked="false"' in listed


def test_the_form_comes_back_with_what_to_change(client):
    form = _add(client, name="", listen_port="99999").text
    assert "Give it a name." in form and "A port from 1 to 65535." in form
    assert 'value="db.internal"' in form  # What was typed stays.
    assert _saved() == []
    _add(client)
    assert "pi has a tunnel named &#39;Database&#39; already." in _add(client, listen_port="1").text


def test_switching_a_tunnel_on_and_off(client, spawn: FakeSpawn):
    _add(client)
    [t] = _saved()
    listed = client.post(f"/hosts/pi/tunnels/{t.id}/start").text
    assert 'aria-checked="true"' in listed and f"hosts/pi/tunnels/{t.id}/stop" in listed
    listed = _until(client, "Running")
    assert '<span class="pill pill--accent">1 on</span>' in listed  # The top bar's count.
    assert spawn.commands[0][-2:] == ["--", "pi"]
    listed = client.post(f"/hosts/pi/tunnels/{t.id}/stop").text
    assert "Off" in listed and 'aria-checked="false"' in listed
    assert spawn.started[0].terminated


def test_the_hosts_table_counts_each_hosts_tunnels_and_the_details_switch_them(client, ssh):
    ssh.answers[herdr._PROBE] = answers(127)
    page = client.get("/").text
    # Before any is added: the column, and the dialog New tunnel and the details' Add open the form in.
    assert ">Tunnels</th>" in page and '<dialog id="tunnel-dialog"' in page and '<div id="tunnel-form"></div>' in page
    _add(client)
    [t] = _saved()
    row = client.get("/hosts/pi/row").text
    assert 'class="tunnels-cell nowrap" data-value="1"' in row and "· 1 on" not in row
    assert 'hx-trigger="tunnels-changed from:body"' in row
    # The details' card has the tunnels, with their switches.
    listed = client.get("/hosts/pi/tunnels").text
    assert "Database" in listed and f'hx-post="hosts/pi/tunnels/{t.id}/start"' in listed
    switched = client.post(f"/hosts/pi/tunnels/{t.id}/start")
    assert switched.headers["HX-Trigger"] == "tunnels-changed"
    cell = client.get("/hosts/pi/tunnels/cell").text
    assert cell.startswith('<td class="tunnels-cell nowrap" data-value="1001"') and "· 1 on" in cell
    assert client.post(f"/hosts/pi/tunnels/{t.id}/stop").headers["HX-Trigger"] == "tunnels-changed"
    assert "· 1 on" not in client.get("/hosts/pi/tunnels/cell").text


def test_new_tunnel_on_the_hosts_page_picks_its_host_in_the_form(client):
    page = client.get("/").text
    assert 'hx-get="tunnels/new" hx-target="#tunnel-form"' in page and ">New tunnel</button>" in page
    form = client.get("/tunnels/new").text
    assert 'hx-post="tunnels"' in form and '<select class="input" name="host"' in form
    assert '<option value="pi" >pi</option>' in form and '<option value="nas" >nas</option>' in form
    values = {"name": "Database", "kind": "local", "listen_port": "15432", "target_host": "db", "target_port": "5432"}
    unpicked = client.post("/tunnels", content=urlencode(values), headers=FORM).text
    assert "Pick the host it goes through." in unpicked and 'value="Database"' in unpicked and _saved() == []
    wrong = client.post("/tunnels", content=urlencode(values | {"host": "pi", "listen_port": "0"}), headers=FORM).text
    assert "A port from 1 to 65535." in wrong and '<option value="pi" selected>pi</option>' in wrong
    saved = client.post("/tunnels", content=urlencode(values | {"host": "pi"}), headers=FORM)
    assert saved.headers["HX-Trigger"] == "tunnels-changed" and [t.name for t in _saved("pi")] == ["Database"]


def test_the_form_closes_its_dialog_when_cancelled(client):
    form = client.get("/hosts/pi/tunnels/new").text
    assert "this.closest('dialog')?.close()" in form


def test_a_failed_tunnel_says_why_in_plain_words(client, spawn: FakeSpawn):
    spawn.processes.append(
        lambda: FakeProcess(["debug1: Offering public key", "lo9i@lo9i-pi.local: Permission denied (publickey)."], 255)
    )
    _add(client)
    [t] = _saved()
    client.post(f"/hosts/pi/tunnels/{t.id}/start")
    listed = _until(client, "Failed")
    assert "didn&#39;t accept this computer&#39;s SSH key" in listed
    assert "debug1" not in listed and "(publickey)" not in listed
    assert 'aria-checked="false"' in listed


def test_changing_a_tunnel_thats_on_runs_it_again_as_changed(client, spawn: FakeSpawn):
    _add(client)
    [t] = _saved()
    form = client.get(f"/hosts/pi/tunnels/{t.id}/edit").text
    assert f'hx-post="hosts/pi/tunnels/{t.id}"' in form and 'value="15432"' in form and "Edit “Database”" in form
    client.post(f"/hosts/pi/tunnels/{t.id}/start")
    _until(client, "Running")
    values = {"name": "Database", "kind": "local", "listen_port": "25432", "target_host": "db.internal",
              "target_port": "5432"}  # fmt: skip
    response = client.post(f"/hosts/pi/tunnels/{t.id}", content=urlencode(values), headers=FORM)
    assert response.headers["HX-Trigger"] == "tunnels-changed"
    _until(client, "localhost:25432")
    assert spawn.started[0].terminated and "127.0.0.1:25432:db.internal:5432" in spawn.commands[-1]


def test_deleting_a_tunnel_stops_it(client, spawn: FakeSpawn):
    _add(client)
    [t] = _saved()
    client.post(f"/hosts/pi/tunnels/{t.id}/start")
    listed = client.delete(f"/hosts/pi/tunnels/{t.id}").text
    assert "No tunnels yet." in listed and _saved() == []
    assert spawn.started[0].terminated


def test_tunnels_are_reached_only_through_their_own_host(client):
    _add(client)
    [t] = _saved()
    assert client.post(f"/hosts/nas/tunnels/{t.id}/start").status_code == 404
    assert client.delete(f"/hosts/nas/tunnels/{t.id}").status_code == 404
    assert client.get("/hosts/pi/tunnels/999/edit").status_code == 404
    assert client.get("/hosts/elsewhere/tunnels").status_code == 404
    assert _add(client).status_code == 200
    assert client.post("/hosts/elsewhere/tunnels", content="name=x", headers=FORM).status_code == 404


def test_closing_the_page_stops_every_tunnel(fake_herdr, spawn: FakeSpawn):
    with TestClient(create_app(fake_herdr, running=spawn.tunnels())) as client:
        _add(client)
        _add(client, name="Proxy", kind="socks", listen_port="1080")
        for t in _saved():
            client.post(f"/hosts/pi/tunnels/{t.id}/start")
        _until(client, "Running")
    assert len(spawn.started) == 2 and all(p.terminated for p in spawn.started)


def test_the_assistants_tools_get_the_tunnels_as_json(client):
    _add(client)
    _add(client, name="Proxy", kind="socks", listen_port="1080")
    database, proxy = _saved()
    listed = client.get("/api/tunnels").json()
    assert listed[1] == {
        "id": proxy.id, "host": "pi", "name": "Proxy", "type": "socks", "listen_port": 1080,
        "description": "SOCKS proxy at localhost:1080, through pi", "state": "stopped",
    }  # fmt: skip
    started = client.post(f"/api/hosts/pi/tunnels/{database.id}/start").json()
    assert started["state"] == "starting" and started["target_port"] == 5432
    stopped = client.post(f"/api/hosts/pi/tunnels/{database.id}/stop").json()
    assert stopped["state"] == "stopped"
    assert client.post(f"/api/hosts/nas/tunnels/{database.id}/start").status_code == 404
