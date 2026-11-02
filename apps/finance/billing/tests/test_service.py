import base64
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import CUSTOMER_CUIT, ISSUER_CUIT, NOW, make_certificate

from billing import pdf, service

# --- Amounts and periods ---


def test_half_a_month_is_half_the_monthly_net_plus_iva():
    assert service.amounts(Decimal("3750000.00")) == (
        Decimal("3750000.00"),
        Decimal("787500.00"),
        Decimal("4537500.00"),
    )


@pytest.mark.parametrize(
    "month, second_half_ends",
    [
        ("2026-10", date(2026, 10, 31)),
        ("2026-11", date(2026, 11, 30)),
        ("2028-02", date(2028, 2, 29)),
        ("2027-02", date(2027, 2, 28)),
    ],
)
def test_a_month_splits_on_the_15th(month, second_half_ends):
    first, second = service.halves(month)
    assert first == (second_half_ends.replace(day=1), second_half_ends.replace(day=15))
    assert second == (second_half_ends.replace(day=16), second_half_ends)


def test_halves_of_an_odd_amount_add_up_to_the_month(conn):
    service.update_customer(conn, "acme", monthly_net=1000.01, effective_from="2026-10")
    c = service.find_customer(conn, "acme")
    (a, b), (c1, d) = service.halves("2026-10")
    first, second = service.default_net(conn, c, a, b), service.default_net(conn, c, c1, d)
    assert (first, second) == (Decimal("500.01"), Decimal("500.00"))
    assert service.default_net(conn, c, a, d) == Decimal("1000.01")


def test_another_period_needs_an_amount(conn):
    c = service.find_customer(conn, "acme")
    with pytest.raises(service.InvalidError, match="net_amount"):
        service.default_net(conn, c, date(2026, 10, 5), date(2026, 10, 20))


def test_a_raise_applies_from_its_month(conn):
    service.update_customer(conn, "acme", monthly_net=8_000_000, effective_from="2027-01")
    december = service.prepare_month(conn, "acme", "2026-12", on=NOW.date())
    january = service.prepare_month(conn, "acme", "2027-01", on=NOW.date())
    assert [i["net"] for i in december["invoices"]] == [3_750_000, 3_750_000]
    assert [i["net"] for i in january["invoices"]] == [4_000_000, 4_000_000]
    customer = service.list_customers(conn)[0]
    assert customer["monthly_net"] == 7_500_000
    assert customer["prices"] == [
        {"effective_from": None, "monthly_net": 7_500_000},
        {"effective_from": "2027-01-01", "monthly_net": 8_000_000},
    ]


# --- Preparing a month ---


def test_prepare_month_previews_last_month_without_asking_arca(conn, fake):
    preview = service.prepare_month(conn, on=NOW.date())
    assert fake.calls == []
    assert preview["month"] == "2026-10"
    assert preview["missing_setup"] == []
    first, second = preview["invoices"]
    assert (first["period_from"], first["period_to"]) == ("2026-10-01", "2026-10-15")
    assert (second["period_from"], second["period_to"]) == ("2026-10-16", "2026-10-31")
    for i in (first, second):
        assert (i["net"], i["iva"], i["total"]) == (3_750_000, 787_500, 4_537_500)
        assert i["status"] == "not issued"
        assert i["issue_date"] == "2026-11-02" and i["due_date"] == "2026-11-12"
    assert first["create_invoice"] == {"customer": "acme", "period_from": "2026-10-01", "period_to": "2026-10-15"}
    assert first["description"] == "Servicios de desarrollo de software (01/10/2026 al 15/10/2026)"
    assert preview["total_to_issue"] == 9_075_000


def test_prepare_month_shows_what_was_issued(conn, issue):
    issue()
    first, second = service.prepare_month(conn, "acme", "2026-10", on=NOW.date())["invoices"]
    assert first["status"] == "authorized" and first["invoice"]["full_number"] == "00002-00000001"
    assert second["status"] == "not issued"


# --- Issuing ---


def test_numbers_follow_arcas_last(conn, fake, issue):
    fake.last[(2, 1)] = 41
    first = issue()
    second = issue(period_from="2026-10-16", period_to="2026-10-31")
    assert (first["number"], second["number"]) == (42, 43)
    assert first["full_number"] == "00002-00000042"
    assert first["status"] == "authorized" and first["already_issued"] is False
    assert first["cae"] == "76000000000042" and first["cae_due"] == "2026-11-12"
    sent = fake.issued[(2, 1, 42)]
    assert sent["Concepto"] == "2" and sent["DocTipo"] == "80" and sent["DocNro"] == CUSTOMER_CUIT
    assert (sent["CbteFch"], sent["FchServDesde"], sent["FchServHasta"], sent["FchVtoPago"]) == (
        "20261102",
        "20261001",
        "20261015",
        "20261112",
    )
    assert (sent["ImpNeto"], sent["ImpIVA"], sent["ImpTotal"]) == ("3750000.00", "787500.00", "4537500.00")
    assert sent["AlicIva"] == {"Id": "5", "BaseImp": "3750000.00", "Importe": "787500.00"}
    assert (sent["MonId"], sent["MonCotiz"], sent["CondicionIVAReceptorId"]) == ("PES", "1", "1")


def test_fields_go_in_wsfev1s_order(fake, issue):
    issue()
    order = fake.issued[(2, 1, 1)]["order"]
    expected = [
        "Concepto",
        "DocTipo",
        "DocNro",
        "CbteDesde",
        "CbteHasta",
        "CbteFch",
        "ImpTotal",
        "ImpTotConc",
        "ImpNeto",
        "ImpOpEx",
        "ImpTrib",
        "ImpIVA",
        "FchServDesde",
        "FchServHasta",
        "FchVtoPago",
        "MonId",
        "MonCotiz",
        "CondicionIVAReceptorId",
        "Iva",
    ]
    assert order == expected


def test_the_same_period_is_issued_once(conn, fake, issue):
    first = issue()
    again = issue()
    with_amount = issue(net_amount=3_750_000)
    assert fake.calls.count("FECAESolicitar") == 1
    assert again["already_issued"] is True and again["id"] == first["id"] and again["number"] == first["number"]
    assert with_amount["already_issued"] is True
    assert len(service.list_invoices(conn)) == 1


def test_a_different_amount_for_an_issued_period_is_refused(fake, issue):
    issue()
    with pytest.raises(service.AlreadyIssuedError, match="credit note"):
        issue(net_amount=4_000_000)
    assert fake.calls.count("FECAESolicitar") == 1


def test_an_overlapping_period_is_refused(fake, issue):
    issue()
    with pytest.raises(service.AlreadyIssuedError, match="overlaps"):
        issue(period_from="2026-10-01", period_to="2026-10-31")
    with pytest.raises(service.AlreadyIssuedError, match="overlaps"):
        issue(period_from="2026-10-10", period_to="2026-10-20", net_amount=100)
    assert fake.calls.count("FECAESolicitar") == 1


def test_each_environment_has_its_own_invoices(conn, fake, issue, certificate):
    issue()
    service.setup(conn, environment="produccion", certificate_path=str(certificate[0]), key_path=str(certificate[1]))
    issued = issue()
    assert issued["environment"] == "produccion" and issued["already_issued"] is False
    assert len(fake.logins) == 2
    assert fake.logins[1] == "https://wsaa.afip.gov.ar/ws/services/LoginCms"


def test_a_lost_answer_is_checked_with_arca_before_issuing_again(conn, fake, issue):
    fake.lose_answer = True
    with pytest.raises(service.UnconfirmedError, match="never issues it twice"):
        issue()
    (pending,) = service.list_invoices(conn)
    assert pending["status"] == "sent" and pending["number"] == 1

    recovered = issue()
    assert recovered["status"] == "authorized" and recovered["already_issued"] is True
    assert recovered["number"] == 1 and recovered["cae"] == "76000000000001"
    assert fake.calls.count("FECAESolicitar") == 1
    assert fake.calls.count("FECompConsultar") == 1
    assert issue(period_from="2026-10-16", period_to="2026-10-31")["number"] == 2


def test_a_lost_request_is_issued_again_once_arca_says_it_doesnt_have_it(conn, fake, issue):
    fake.lose_request = True
    with pytest.raises(service.UnconfirmedError):
        issue()
    issued = issue()
    assert issued["status"] == "authorized" and issued["already_issued"] is False and issued["number"] == 1
    assert fake.calls.count("FECompConsultar") == 1
    assert len(fake.issued) == 1


def test_a_number_taken_by_another_invoice_isnt_this_ones(conn, fake, issue):
    fake.lose_request = True
    with pytest.raises(service.UnconfirmedError):
        issue()
    # Meanwhile number 1 went to another invoice.
    fake.last[(2, 1)] = 1
    fake.issued[(2, 1, 1)] = {
        "DocNro": "20111111112",
        "CbteFch": "20261102",
        "ImpTotal": "121.00",
        "FchServDesde": "20261001",
        "FchServHasta": "20261001",
        "CAE": "1",
        "CAEFchVto": "20261112",
    }
    issued = issue()
    assert issued["number"] == 2 and issued["already_issued"] is False


def test_an_invoice_issued_without_answer_blocks_overlapping_ones(fake, issue):
    fake.lose_answer = True
    with pytest.raises(service.UnconfirmedError):
        issue()
    with pytest.raises(service.AlreadyIssuedError, match="00002-00000001"):
        issue(period_from="2026-10-01", period_to="2026-10-31")
    assert fake.calls.count("FECAESolicitar") == 1


def test_another_period_settles_what_was_sent_first(conn, fake, issue):
    fake.lose_request = True
    with pytest.raises(service.UnconfirmedError):
        issue()
    # ARCA never got number 1, so the second half takes it, and the first half the next.
    second = issue(period_from="2026-10-16", period_to="2026-10-31")
    first = issue()
    assert (second["number"], first["number"]) == (1, 2)
    assert [i["status"] for i in service.list_invoices(conn)] == ["authorized", "authorized"]


def test_when_arca_cant_be_reached_nothing_is_issued(conn, fake, issue):
    fake.unreachable = True
    with pytest.raises(service.BillingError, match="Nothing was issued"):
        issue()
    assert service.list_invoices(conn)[0]["status"] == "draft"
    assert issue()["number"] == 1
    assert "FECompConsultar" not in fake.calls


def test_a_rejection_is_kept_and_the_period_can_be_issued_later(conn, fake, issue):
    fake.reject = (10013, "Para comprobantes clase A el receptor debe ser CUIT")
    with pytest.raises(service.RejectedError, match="10013"):
        issue()
    preview = service.prepare_month(conn, "acme", "2026-10", on=NOW.date())["invoices"][0]
    assert preview["status"] == "rejected" and preview["last_errors"][0]["code"] == 10013
    (rejected,) = service.list_invoices(conn)
    assert rejected["status"] == "rejected" and rejected["number"] is None
    assert rejected["errors"] == [] and rejected["observations"][0]["code"] == 10013
    issued = issue()
    assert issued["id"] == rejected["id"] and issued["number"] == 1 and issued["observations"] == []


def test_only_factura_a_receivers_are_invoiced(conn, issue):
    service.update_customer(conn, "acme", iva_condition="consumidor_final")
    with pytest.raises(service.InvalidError, match="Factura A"):
        issue()
    assert service.prepare_month(conn, "acme", "2026-10")["invoices"][0]["status"] == "can't prepare"


def test_issuing_needs_the_setup(certificate, fake, tmp_path):
    from billing import db

    conn = db.connect(":memory:")
    service.add_customer(conn, "Acme", CUSTOMER_CUIT, monthly_net=100)
    with pytest.raises(service.NotReadyError, match="cuit, business_name"):
        service.create_invoice(conn, "acme", "2026-10-01", "2026-10-15", pdf_dir=tmp_path, send=fake, now=NOW)
    assert fake.calls == []


# --- ARCA's tickets ---


def test_the_ticket_is_kept_until_it_expires(conn, fake, issue):
    issue()
    issue(period_from="2026-10-16", period_to="2026-10-31")
    service.check(conn, fake, NOW + timedelta(hours=11))
    assert len(fake.logins) == 1
    # Within five minutes of its expiry (12h10m after NOW - 10m), a new one.
    service.check(conn, fake, NOW + timedelta(hours=11, minutes=56))
    assert len(fake.logins) == 2
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_a_new_certificate_signs_in_again(conn, fake, tmp_path):
    service.check(conn, fake, NOW)
    cert, key = make_certificate(tmp_path, "new")
    service.setup(conn, certificate_path=str(cert), key_path=str(key))
    service.check(conn, fake, NOW)
    assert len(fake.logins) == 2


def test_a_ticket_arca_already_gave_is_explained(conn, fake):
    fake.already_authenticated = True
    with pytest.raises(service.BillingError, match="wait until it expires"):
        service.check(conn, fake, NOW)


def test_check_says_the_next_number(conn, fake):
    fake.last[(2, 1)] = 7
    assert service.check(conn, fake, NOW) == {
        "environment": "homologacion",
        "point_of_sale": 2,
        "last_factura_a": 7,
        "next_factura_a": 8,
    }


# --- Setup ---


def test_setup_keeps_paths_never_the_key(conn, certificate):
    values = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert values["key_path.homologacion"] == str(certificate[1])
    assert not any("PRIVATE KEY" in v or "CERTIFICATE" in v for v in values.values())


def test_setup_reports_the_certificate(conn):
    shown = service.issuer(conn, NOW)
    assert shown["ready"] is True and shown["environment"] == "homologacion"
    cert = shown["certificates"]["homologacion"]
    assert cert["cuit"] == ISSUER_CUIT and cert["expired"] is False
    assert shown["urls"]["wsfe"] == "https://wswhomo.afip.gov.ar/wsfev1/service.asmx"


def test_a_key_of_another_certificate_is_refused(conn, tmp_path):
    _, other_key = make_certificate(tmp_path, "other")
    with pytest.raises(service.InvalidError, match="isn't the certificate's"):
        service.setup(conn, key_path=str(other_key))


def test_produccion_needs_its_own_certificate(conn):
    shown = service.setup(conn, environment="produccion")
    assert shown["ready"] is False and shown["missing"] == ["certificate_path", "key_path"]


@pytest.mark.parametrize("bad", ["20-12345678-7", "1234", "abc"])
def test_cuits_are_checked(conn, bad):
    with pytest.raises(service.InvalidError, match="CUIT"):
        service.add_customer(conn, "Bad", bad)


# --- The PDF and its QR ---


def test_the_qr_carries_rg_4291s_data(conn, fake, issue):
    fake.last[(2, 1)] = 41
    invoice = issue()
    row = conn.execute("SELECT issuer, customer FROM invoices").fetchone()
    full = invoice | {"issuer": json.loads(row["issuer"]), "customer": json.loads(row["customer"])}
    url = pdf.qr_url(full)
    assert url.startswith("https://www.afip.gob.ar/fe/qr/?p=")
    assert json.loads(base64.b64decode(url.split("?p=")[1])) == {
        "ver": 1,
        "fecha": "2026-11-02",
        "cuit": int(ISSUER_CUIT),
        "ptoVta": 2,
        "tipoCmp": 1,
        "nroCmp": 42,
        "importe": 4537500.0,
        "moneda": "PES",
        "ctz": 1,
        "tipoDocRec": 80,
        "nroDocRec": int(CUSTOMER_CUIT),
        "tipoCodAut": "E",
        "codAut": 76000000000042,
    }


def test_an_issued_invoice_has_its_pdf(conn, issue, tmp_path):
    invoice = issue()
    path = invoice["pdf_path"]
    assert path.endswith("invoices/HOMOLOGACION-FA-00002-00000001.pdf")
    with open(path, "rb") as f:
        assert f.read(5) == b"%PDF-"
    # A missing PDF is made again.
    import os

    os.remove(path)
    assert service.get_invoice(conn, invoice["id"], tmp_path / "invoices")["pdf_path"] == path
    assert os.path.exists(path)


def test_pesos_are_written_the_argentine_way():
    assert pdf.pesos(4537500) == "4.537.500,00"
    assert pdf.pesos(0.5) == "0,50"
