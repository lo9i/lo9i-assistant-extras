"""The hosts page's own hosts: adding, changing and deleting them, importing the SSH config's again, and what it
shows when they can't be read. The forms come into its dialog; a saved one has the page load again."""

import json
from urllib.parse import urlencode

import pytest
from conftest import FakeSpawn
from fastapi.testclient import TestClient

from hosts import service
from hosts.details import Details

FORM = {"Content-Type": "application/x-www-form-urlencoded", "HX-Request": "true"}
SECRET = "correct horse battery staple"


@pytest.fixture
def client(fake_herdr, spawn: FakeSpawn):
    from hosts.web.app import create_app

    with TestClient(create_app(fake_herdr, running=spawn.tunnels())) as client:
        yield client


def _post(client, path: str, **fields):
    values = {"name": "box", "hostname": "box.example", "user": "admin", "port": "", "identity_file": "",
              "options": "", "password": ""} | fields  # fmt: skip
    return client.post(path, content=urlencode(values), headers=FORM)


def _event(response, name: str) -> dict:
    return json.loads(response.headers["HX-Trigger"])[name]


def test_the_page_offers_adding_and_importing_and_says_where_the_hosts_are(client):
    page = client.get("/").text
    assert 'hx-get="new-host" hx-target="#host-form"' in page and '<div id="host-form"></div>' in page
    assert 'hx-post="import"' in page and "Re-import from SSH config" in page
    assert "Kept here, encrypted." in page
    details = client.get("/hosts/pi/details").text
    assert 'hx-get="hosts/pi/edit"' in details and 'hx-delete="hosts/pi"' in details
    assert "Your SSH config stays as it is." in details


def test_a_host_added_is_kept_and_the_page_loads_again_with_it(client):
    form = client.get("/new-host").text
    assert 'hx-post="new-host"' in form and 'type="password"' in form
    response = _post(client, "/new-host", password=SECRET, options="ServerAliveInterval 30")
    assert response.status_code == 200 and response.text == ""
    assert _event(response, "hosts-reload") == {"message": "box is saved.", "host": "box"}
    page = client.get("/?host=box").text
    assert 'data-host="box"' in page and "admin@box.example" in page and "its password" in page


def test_what_cant_be_used_comes_back_in_the_form_without_the_password(client):
    response = _post(client, "/new-host", name="work box", identity_file="-----BEGIN KEY-----", password=SECRET)
    assert "HX-Trigger" not in response.headers
    assert 'class="field-error"' in response.text and "Never the key itself." in response.text
    assert SECRET not in response.text and 'value="box.example"' in response.text
    taken = _post(client, "/new-host", name="pi")
    assert "There&#39;s a host named pi already." in taken.text


async def _box() -> Details:
    return await service.details("box")


def test_editing_keeps_the_saved_password_unless_one_is_typed_or_removed(client):
    _post(client, "/new-host", password=SECRET)
    form = client.get("/hosts/box/edit").text
    assert SECRET not in form and "Saved. Type a new one to change it" in form and 'name="remove_password"' in form
    assert 'value="box.example"' in form and 'hx-post="hosts/box"' in form
    _post(client, "/hosts/box", hostname="box.lan")
    found = client.portal.call(_box)
    assert (found.hostname, found.password) == ("box.lan", SECRET)
    _post(client, "/hosts/box", password="new one")
    assert client.portal.call(_box).password == "new one"
    _post(client, "/hosts/box", remove_password="1")
    assert client.portal.call(_box).password == ""


def test_renaming_a_host_keeps_its_tunnels_and_deleting_it_takes_them(client):
    tunnel = {"name": "Web", "kind": "local", "listen_port": "18080", "target_host": "localhost", "target_port": "80"}
    client.post("/hosts/pi/tunnels", content=urlencode(tunnel), headers=FORM)
    response = _post(client, "/hosts/pi", name="raspberry", hostname="lo9i-pi.local", user="lo9i")
    assert _event(response, "hosts-reload")["host"] == "raspberry"
    assert "Web" in client.get("/hosts/raspberry/tunnels").text
    assert client.get("/hosts/pi/details").status_code == 404
    deleted = client.delete("/hosts/raspberry", headers=FORM)
    assert _event(deleted, "hosts-reload") == {"message": "raspberry is deleted, with its tunnels.", "host": ""}
    assert 'data-host="raspberry"' not in client.get("/").text
    assert client.portal.call(service.tunnels) == []


def test_importing_again_adds_what_the_list_doesnt_have_and_says_so(client, config):
    client.get("/")  # The first look imports.
    nothing = client.post("/import", headers=FORM)
    assert _event(nothing, "toast")["message"].startswith("Every host in your SSH config is here already.")
    config.write_text(config.read_text() + "\nHost web\n  HostName web.local\n")
    added = client.post("/import", headers=FORM)
    assert _event(added, "hosts-reload") == {"message": "Added from your SSH config: web.", "host": "web"}


def test_hosts_that_cant_be_read_say_why_in_plain_words(client, monkeypatch):
    monkeypatch.delenv("LO9I_DATA_KEY")
    page = client.get("/")
    assert page.status_code == 503 and "Your hosts can't be opened right now" in page.text
    assert "lo9i didn&#39;t give the hosts plugin its key" in page.text and "Traceback" not in page.text
    partial = client.get("/hosts/pi/details", headers={"HX-Request": "true"})
    assert partial.status_code == 503 and partial.text.startswith("lo9i didn't give the hosts plugin its key")


def test_the_pages_notes_are_never_red_text():
    from pathlib import Path

    css = (Path(__file__).parent.parent / "src" / "hosts" / "web" / "static" / "app.css").read_text()
    toast = css[css.index(".toast {") :]
    toast = toast[: toast.index("}")]
    assert "danger" not in toast and "color: var(--color-text-primary)" in toast
