"""The page shows what the channel wrote: the QR code to scan, the linked account, or a button for a new QR code,
which leaves the channel a request."""

import pytest
from starlette.testclient import TestClient

from lo9i_whatsapp.link import NEW_QR_FILE, Link
from lo9i_whatsapp.web.app import create_app


@pytest.fixture
def page(tmp_path):
    return TestClient(create_app(tmp_path), headers={"X-Forwarded-Prefix": "/whatsapp"})


def test_before_the_channel_runs_the_page_says_so(page):
    response = page.get("/")
    assert response.status_code == 200 and "WhatsApp isn't running" in response.text
    assert '<base href="/whatsapp/">' in response.text


async def test_the_qr_code_to_scan_then_the_linked_account(page, tmp_path):
    link = Link(tmp_path)
    await link.waiting("2@AbCdEf,123,456")
    panel = page.get("/panel").text
    assert "<svg" in panel and "Linked devices" in panel
    await link.linked("+5491112345678", "Ani")
    panel = page.get("/panel").text
    assert "<svg" not in panel and "+5491112345678" in panel and "Message yourself" in panel


async def test_an_expired_qr_code_can_be_asked_for_again(page, tmp_path):
    link = Link(tmp_path)
    await link.waiting("2@old")
    page.post("/new-qr")
    assert not (tmp_path / NEW_QR_FILE).exists()
    await link.expired()
    assert "Show a new QR code" in page.get("/panel").text
    page.post("/new-qr")
    assert link.asked_for_new_qr() and not (tmp_path / NEW_QR_FILE).exists()
