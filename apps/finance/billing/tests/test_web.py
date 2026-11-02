import os
import stat
import tempfile
from pathlib import Path

import pytest
from conftest import CUSTOMER_CUIT, make_certificate, set_up
from fastapi.testclient import TestClient

from billing import db, service
from billing.web import app as web
from billing.web.app import create_app, last_month, listen

HX = {"HX-Request": "true"}


@pytest.fixture
def path(tmp_path, monkeypatch, fake, certificate):
    p = str(tmp_path / "billing.db")
    monkeypatch.setenv("BILLING_DB", p)
    monkeypatch.setenv("BILLING_INVOICES", str(tmp_path / "invoices"))
    monkeypatch.setattr(web, "SEND", fake)
    conn = db.connect(p)
    set_up(conn, certificate)
    conn.close()
    return p


@pytest.fixture
def client(path):
    return TestClient(create_app())


def invoices(path):
    conn = db.connect(path)
    try:
        return service.list_invoices(conn)
    finally:
        conn.close()


HALF = {"customer": "acme", "period_from": "2026-10-01", "period_to": "2026-10-15", "month": "2026-10"}


def test_the_month_shows_each_half_to_issue(client, fake):
    page = client.get("/?month=2026-10").text
    assert "Acme S.A." in page and page.count("$ 4.537.500,00") >= 2
    assert "01/10/2026 to 15/10/2026" in page and "16/10/2026 to 31/10/2026" in page
    assert "$ 3.750.000,00 + IVA $ 787.500,00" in page
    assert "$ 9.075.000,00" in page  # to issue
    assert "Homologación" in page and "no fiscal value" in page
    assert 'hx-confirm="Issue a Factura A to Acme S.A. for 01/10/2026 to 15/10/2026, $ 4.537.500,00' in page
    assert fake.calls == []


def test_last_month_by_default(client):
    assert service.today().replace(day=1).strftime("%Y-%m") > last_month(service.today())
    assert f'value="{last_month(service.today())}"' in client.get("/").text


def test_issuing_a_half_from_the_page(client, path, fake):
    page = client.post("/invoices", headers=HX, data=HALF).text
    assert "Issued Factura A 00002-00000001 to Acme S.A., CAE 76000000000001." in page
    assert 'href="invoices/1/pdf"' in page
    again = client.post("/invoices", headers=HX, data=HALF).text
    assert "Already issued as Factura A 00002-00000001" in again
    assert fake.calls.count("FECAESolicitar") == 1
    pdf = client.get("/invoices/1/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF-")
    assert "HOMOLOGACION-FA-00002-00000001.pdf" in pdf.headers["content-disposition"]


def test_a_rejection_shows_why_and_offers_to_try_again(client, fake):
    fake.reject = (10013, "Para comprobantes clase A el receptor debe ser CUIT")
    page = client.post("/invoices", headers=HX, data=HALF).text
    assert "ARCA rejected invoice 1, nothing was issued" in page
    assert "10013: Para comprobantes clase A" in page and "Issue again" in page
    assert "Other invoices" not in page  # it's in its customer's card, once


def test_an_invoice_opens_at_its_month(client):
    client.post("/invoices", headers=HX, data=HALF)
    page = client.get("/invoices/1").text
    assert 'id="invoice-1"' in page and "focused" in page and 'value="2026-10"' in page
    assert client.get("/invoices/9").status_code == 404
    assert client.get("/invoices/9/pdf").status_code == 404


def test_without_setup_nothing_can_be_issued(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DB", str(tmp_path / "new.db"))
    conn = db.connect()
    service.add_customer(conn, "Acme", CUSTOMER_CUIT, monthly_net=100)
    conn.close()
    page = TestClient(create_app()).get("/?month=2026-10").text
    assert "Set up billing" in page and 'href="settings">Open Settings' in page
    assert '<li class="done">' in page  # the customer step: Acme has a monthly amount
    assert "disabled" in page and "IVA condition" not in page.split("<script>")[0]


def test_a_new_install_shows_the_steps_and_no_zeros(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DB", str(tmp_path / "new.db"))
    page = TestClient(create_app()).get("/").text
    assert page.count('<span class="step-mark">') == 3 and "done" not in page
    assert "$ 0,00" not in page and "isn't set up" not in page


# --- Customers ---


def test_adding_a_customer_with_an_argentine_amount(client, path):
    client.post(
        "/customers",
        headers=HX,
        data={
            "name": "Globex SRL",
            "cuit": "30-70000000-8",
            "iva_condition": "monotributo",
            "monthly_net": "1.234.567,89",
        },
    )
    page = client.get("/customers").text
    assert "Globex SRL" in page and "Responsable Monotributo" in page and "$ 1.234.567,89" in page


def test_a_wrong_customer_keeps_what_was_typed(client):
    resp = client.post("/customers", headers=HX, data={"name": "Bad", "cuit": "20-12345678-7"})
    assert resp.headers["HX-Retarget"] == "#add-customer"
    assert "verification digit" in resp.text and 'value="Bad"' in resp.text


def test_a_raise_from_a_later_month(client):
    client.post("/customers/acme/prices", headers=HX, data={"monthly_net": "8.000.000", "effective_from": "2099-01"})
    page = client.get("/customers").text
    assert "$ 8.000.000,00" in page and "From January 2099" in page and "From the start" in page
    # Today's amount is the one marked now.
    assert page.index('$ 7.500.000,00 <span class="tag">now</span>') > 0
    client.post("/customers/acme/prices/delete", headers=HX, data={"effective_from": "2099-01-01"})
    assert "$ 8.000.000,00" not in client.get("/customers").text


def test_editing_a_customer(client):
    assert 'value="Acme S.A."' in client.get("/customers/acme/edit").text
    client.post(
        "/customers/acme",
        headers=HX,
        data={"name": "Acme SA", "cuit": CUSTOMER_CUIT, "iva_condition": "responsable_inscripto", "description": "Dev"},
    )
    assert "Acme SA" in client.get("/customers").text


# --- Settings ---


def test_settings_show_the_certificate(client):
    page = client.get("/settings").text
    assert 'value="20123456786"' in page and "For CUIT 20123456786." in page and "Valid until 31/12/2027" in page


def test_a_key_of_another_certificate_saves_nothing(client, tmp_path):
    _, other = make_certificate(tmp_path, "other")
    page = client.post("/settings", headers=HX, data={"business_name": "Changed", "key_homologacion": str(other)}).text
    assert "isn&#39;t the certificate&#39;s" in page
    assert 'value="Ana Pérez Software"' in client.get("/settings").text


def test_produccion_gets_its_certificate_without_switching(client, tmp_path):
    cert, key = make_certificate(tmp_path, "prod")
    page = client.post(
        "/settings", headers=HX, data={"certificate_produccion": str(cert), "key_produccion": str(key)}
    ).text
    assert "Saved." in page
    shown = service.issuer(db.connect())
    assert shown["environment"] == "homologacion" and shown["certificates"]["produccion"]["certificate_path"] == str(
        cert
    )


def test_a_certificate_and_key_picked_with_browse_are_kept_only_for_the_user(client, tmp_path, path):
    cert, key = make_certificate(tmp_path, "picked")
    assert "Browse…" in client.get("/settings").text
    files = {
        "certificate_file_produccion": ("arca.crt", cert.read_bytes()),
        "key_file_produccion": ("arca.key", key.read_bytes()),
    }
    # The fields hold the files' names, as the page fills them; the files win.
    data = {"certificate_produccion": "arca.crt", "key_produccion": "arca.key"}
    page = client.post("/settings", headers=HX, data=data, files=files).text
    assert "Saved." in page
    kept = service.issuer(db.connect())["certificates"]["produccion"]
    folder = Path(path).parent / "certificates"
    assert Path(kept["certificate_path"]).parent == folder and kept["subject"]
    assert Path(kept["key_path"]).read_bytes() == key.read_bytes()
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(kept["key_path"]).st_mode) == 0o600
    assert f'value="{kept["key_path"]}"' in page


def test_a_picked_key_of_another_certificate_saves_nothing_and_isnt_kept(client, tmp_path, path):
    _, other = make_certificate(tmp_path, "other")
    files = {"key_file_homologacion": ("other.key", other.read_bytes())}
    page = client.post("/settings", headers=HX, data={"key_homologacion": "other.key"}, files=files).text
    assert "isn&#39;t the certificate&#39;s" in page and 'value="other.key"' not in page
    assert list((Path(path).parent / "certificates").iterdir()) == []
    # Replacing a kept one removes it.
    cert, key = make_certificate(tmp_path, "first")
    first = {
        "certificate_file_produccion": ("a.crt", cert.read_bytes()),
        "key_file_produccion": ("a.key", key.read_bytes()),
    }
    client.post("/settings", headers=HX, files=first)
    cert, key = make_certificate(tmp_path, "second")
    second = {
        "certificate_file_produccion": ("b.crt", cert.read_bytes()),
        "key_file_produccion": ("b.key", key.read_bytes()),
    }
    client.post("/settings", headers=HX, files=second)
    kept = sorted(p.read_bytes() for p in (Path(path).parent / "certificates").iterdir())
    assert kept == sorted([cert.read_bytes(), key.read_bytes()])


def test_the_certificate_request_is_shown_to_copy_and_downloaded(client):
    assert "Make the certificate request" in client.get("/settings").text
    shown = client.get("/settings/request/homologacion", headers=HX).text
    assert "BEGIN CERTIFICATE REQUEST" in shown and "In WSASS" in shown
    assert 'href="settings/request/homologacion/file"' in shown
    file = client.get("/settings/request/homologacion/file")
    assert file.headers["content-disposition"] == 'attachment; filename="arca-homologacion.csr"'
    assert file.text.startswith("-----BEGIN CERTIFICATE REQUEST-----")
    assert "needs key_path" in client.get("/settings/request/produccion", headers=HX).text


def test_check_with_arca(client, fake):
    fake.last[(2, 1)] = 7
    assert "last Factura A is 7; the next is 8" in client.post("/settings/check", headers=HX).text


# --- Serving ---


def test_links_follow_the_prefix_lo9i_serves_it_under(client):
    page = client.get("/", headers={"X-Forwarded-Prefix": "/billing"}).text
    assert '<base href="/billing/">' in page


def test_the_socket_is_only_the_users():
    with tempfile.TemporaryDirectory() as d:
        sock = listen(str(Path(d) / "web.sock"))
        try:
            assert stat.S_IMODE(os.stat(Path(d) / "web.sock").st_mode) == 0o600
        finally:
            sock.close()


def test_a_cuit_is_looked_up_for_the_form(client, fake):
    assert "data-lookup" in client.get("/customers").text
    found = client.get("/customers/lookup", params={"cuit": "20-11111111-2"}).json()
    assert found["name"] == "PEREZ JUAN" and found["iva_condition"] == "monotributo"
    assert found["message"] == "From ARCA: PEREZ JUAN, Responsable Monotributo."
    nobody = client.get("/customers/lookup", params={"cuit": "20333333334"}).json()
    assert nobody == {"found": False, "message": "ARCA's padrón has no one with this CUIT."}
    wrong = client.get("/customers/lookup", params={"cuit": "20-12345678-7"}).json()
    assert not wrong["found"] and "verification digit" in wrong["message"]
    unsure = client.get("/customers/lookup", params={"cuit": "30700000008"}).json()
    assert "doesn't say an IVA condition (La CUIT no registra impuestos activos): pick it." in unsure["message"]
    assert "Its CUIT isn't active." in unsure["message"]


def test_a_picked_iva_condition_isnt_replaced(client):
    # An edited customer's condition counts as picked, so a lookup leaves it.
    assert 'name="iva_condition" data-chosen' in client.get("/customers/acme/edit").text
    assert 'name="iva_condition" >' in client.get("/customers").text.split('id="add-customer"')[1]
