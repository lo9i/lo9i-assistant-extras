import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from expenses import currency, db, service
from expenses.web.app import create_app, listen

HX = {"HX-Request": "true"}


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "web.db")
    monkeypatch.setenv("EXPENSES_DB", p)
    conn = db.connect(p)
    service.add_asset(conn, "Boni", "house")
    service.add_obligation(conn, "Luz", "utilities", asset_id="boni")
    currency.set(conn, "ARS", "es_AR")
    conn.close()
    return p


@pytest.fixture
def client(path):
    return TestClient(create_app())


def bills(path):
    conn = db.connect(path)
    try:
        return service.list_bills(conn)
    finally:
        conn.close()


def test_amounts_show_in_the_currency_and_its_locale(client):
    client.post("/bills", headers=HX, data={"amount": "1.160.000", "due_date": "2026-10-10", "category": "tax"})
    assert "$1.160.000,00" in client.get("/").text


def test_the_first_visit_asks_for_the_currency_guessed_from_the_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPENSES_DB", str(tmp_path / "new.db"))
    c = TestClient(create_app())
    first = c.get("/", headers={"X-Forwarded-Prefix": "/expenses"}, follow_redirects=False)
    assert (first.status_code, first.headers["location"]) == (303, "/expenses/settings")
    assert c.get("/settings", headers={"X-Forwarded-Prefix": "/expenses"}).status_code == 200
    assert c.post("/bills", headers=HX).headers["HX-Redirect"] == "/settings"
    page = c.get("/settings", headers={"Accept-Language": "en-GB,en;q=0.8"}).text
    assert '<option value="GBP" selected>' in page and 'value="en_GB"' in page
    assert "Saved" in c.post("/settings", headers=HX, data={"currency": "gbp", "locale": "en_GB"}).text
    c.post("/bills", headers=HX, data={"amount": "45,230.50", "due_date": "2026-10-10", "category": "tax"})
    assert "£45,230.50" in c.get("/").text


def test_amounts_are_read_the_way_the_locale_writes_them(client):
    r = client.post("/bills", headers=HX, data={"amount": "45230.50", "due_date": "2026-10-10", "category": "tax"})
    assert "write it like 45230,5" in r.text


def test_a_wrong_currency_or_locale_is_refused(client):
    assert "an ISO 4217 currency code" in client.post("/settings", headers=HX, data={"currency": "XYZ", "locale": "es_AR"}).text
    assert "a locale, like es_AR" in client.post("/settings", headers=HX, data={"currency": "ARS", "locale": "nope_ZZ"}).text


def test_pages_link_under_the_prefix_the_daemon_gives(client):
    page = client.get("/assets", headers={"X-Forwarded-Prefix": "/expenses"}).text
    assert '<base href="/expenses/">' in page and 'href="static/app.css?v=' in page
    assert '<base href="/">' in client.get("/").text
    styles = client.get("/static/app.css", headers={"X-Forwarded-Prefix": "/expenses"})
    assert styles.status_code == 200 and "text/css" in styles.headers["content-type"]
    # Anything that isn't plain path segments is ignored.
    assert '<base href="/">' in client.get("/", headers={"X-Forwarded-Prefix": '/x"><script>'}).text


def test_every_page_renders(client):
    for url in ("/", "/?f_scope=all&f_status=paid", "/assets", "/categories"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "<nav>" in r.text
    # htmx gets only the section.
    assert "<nav>" not in client.get("/assets", headers=HX).text


def test_add_and_pay_a_bill(client, path):
    r = client.post(
        "/bills", headers=HX, data={"obligation_id": "1", "amount": "45.230,50", "due_date": "2026-10-10"}
    )
    assert r.status_code == 200 and 'id="month"' in r.text
    (b,) = bills(path)
    assert (b.amount, b.asset_id, b.category) == (45230.5, "boni", "utilities")

    client.post(f"/bills/{b.id}/status", headers=HX, data={"status": "paid", "f_scope": "all"})
    assert bills(path)[0].status == "paid"


def test_failed_form_keeps_its_values(client):
    r = client.post("/bills", headers=HX, data={"amount": "12", "due_date": "2026-10-10", "notes": "kept"})
    assert r.headers["HX-Retarget"] == "#add-bill"
    assert "category is required" in r.text
    assert 'value="kept"' in r.text


def test_edit_a_bill(client, path):
    client.post("/bills", headers=HX, data={"amount": "10", "due_date": "2026-10-10", "category": "tax"})
    (b,) = bills(path)
    assert 'id="bill-1"' in client.get(f"/bills/{b.id}/edit", headers=HX).text
    client.post(
        f"/bills/{b.id}",
        headers=HX,
        data={"amount": "20", "due_date": "2026-10-11", "category": "other", "status": "paid"},
    )
    (b,) = bills(path)
    assert (b.amount, b.due_date, b.category, b.status) == (20, "2026-10-11", "other", "paid")


def test_assets_and_obligations(client, path):
    client.post("/assets", headers=HX, data={"name": "HR-V", "kind": "car", "metadata": "plate: AB123CD"})
    r = client.post(
        "/obligations",
        headers=HX,
        data={"asset_id": "hr-v", "name": "Patente", "category": "tax", "due_day": "15",
              "every_months": "2", "anchor_month": "1"},
    )
    assert "Patente" in r.text and "every 2 months from Jan" in r.text

    r = client.post(
        "/obligations", headers=HX, data={"asset_id": "none", "name": "IVA", "category": "tax", "every_months": "1"}
    )
    assert "IVA" in r.text
    conn = db.connect(path)
    assert service.get_asset(conn, "hr-v").metadata == {"plate": "AB123CD"}
    iva = next(o for o in service.list_obligations(conn) if o.name == "IVA")
    assert iva.asset_id is None


def test_obligation_error_stays_in_its_form(client):
    r = client.post(
        "/obligations", headers=HX, data={"asset_id": "boni", "name": "Gas", "category": "x", "every_months": "2"}
    )
    assert r.headers["HX-Retarget"] == "#add-ob-boni"
    assert "anchor_month" in r.text


def test_delete_conflicts_show_a_banner(client):
    client.post("/bills", headers=HX, data={"obligation_id": "1", "amount": "1", "due_date": "2026-10-10"})
    r = client.post("/assets/boni/delete", headers=HX)
    assert 'class="banner"' in r.text and "bill(s)" in r.text


def test_categories(client):
    assert "gifts" in client.post("/categories", headers=HX, data={"name": "Gifts"}).text
    assert "gifts" not in client.post("/categories/delete", headers=HX, data={"name": "gifts"}).text


def test_missing_bill_is_a_plain_error(client):
    r = client.post("/bills/99/delete", headers=HX)
    assert (r.status_code, r.text) == (404, "bill 99 not found")


def test_landing_page_shows_unpaid_by_default(client):
    client.post("/bills", headers=HX, data={"amount": "50", "due_date": "2026-10-10", "category": "tax"})
    client.post("/bills", headers=HX, data={"amount": "7", "due_date": "2026-10-11", "category": "tax"})
    client.post("/bills/2/status", headers=HX, data={"status": "paid"})

    html = client.get("/").text
    assert '<option value="pending" selected>' in html
    assert 'id="bill-1"' in html and 'id="bill-2"' not in html
    # "Any status" is an explicit empty value and shows both.
    html = client.get("/?f_status=").text
    assert 'id="bill-1"' in html and 'id="bill-2"' in html

def test_static_links_change_with_content(client):
    html = client.get("/").text
    assert '"static/app.css?v=' in html and '"static/htmx.min.js?v=' in html



def test_marking_paid_keeps_the_pending_filter(client):
    for day in ("10", "11"):
        client.post("/bills", headers=HX, data={"amount": "5", "due_date": f"2026-10-{day}", "category": "tax"})
    # What the Mark paid button sends: the new status plus the filter form.
    html = client.post(
        "/bills/1/status", headers=HX, data={"status": "paid", "f_status": "pending", "f_scope": "month"}
    ).text
    assert '<option value="pending" selected>' in html
    assert 'id="bill-1"' not in html and 'id="bill-2"' in html


def test_editing_keeps_the_category_filter(client):
    client.post("/bills", headers=HX, data={"amount": "5", "due_date": "2026-10-10", "category": "tax"})
    html = client.post(
        "/bills/1",
        headers=HX,
        data={"amount": "6", "due_date": "2026-10-10", "category": "tax", "status": "pending",
              "f_category": "", "f_status": ""},
    ).text
    assert '<option value="">All categories</option>' in html
    assert '<option value="tax" selected>' not in html


def test_the_socket_is_only_for_this_user():
    # Under /tmp: macOS limits socket paths to 104 characters, and pytest's tmp_path is longer.
    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        path = Path(folder) / "web.sock"
        path.write_text("left over")
        sock = listen(str(path))
        try:
            assert path.is_socket() and path.stat().st_mode & 0o777 == 0o600
        finally:
            sock.close()
