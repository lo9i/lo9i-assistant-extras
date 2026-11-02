import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from conftest import NOW
from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs7

from billing import arca


def test_the_ticket_request_is_valid_twenty_minutes_around_now():
    tra = ET.fromstring(arca.login_request("wsfe", NOW))
    assert tra.findtext("service") == "wsfe"
    assert tra.findtext("header/generationTime") == "2026-11-02T09:50:00-03:00"
    assert tra.findtext("header/expirationTime") == "2026-11-02T10:10:00-03:00"
    assert int(tra.findtext("header/uniqueId")) < 2**32


def test_the_signature_carries_the_request_and_the_certificate(certificate):
    cert_path, key_path = certificate
    tra = arca.login_request("wsfe", NOW)
    cms = arca.sign(tra, cert_path.read_bytes(), key_path.read_bytes())
    assert cms[0] == 0x30  # DER
    assert tra in cms  # nodetach
    (signer,) = pkcs7.load_der_pkcs7_certificates(cms)
    assert signer == x509.load_pem_x509_certificate(cert_path.read_bytes())


@pytest.mark.skipif(not shutil.which("openssl"), reason="no openssl")
def test_openssl_verifies_the_signature(certificate, tmp_path):
    cert_path, key_path = certificate
    tra = arca.login_request("wsfe", NOW)
    signed = tmp_path / "tra.cms"
    signed.write_bytes(arca.sign(tra, cert_path.read_bytes(), key_path.read_bytes()))
    out = subprocess.run(
        ["openssl", "cms", "-verify", "-inform", "DER", "-in", str(signed), "-CAfile", str(cert_path)],
        capture_output=True,
        check=True,
    )
    assert out.stdout == tra


def test_login_reads_the_ticket(fake, certificate):
    cert_path, key_path = certificate
    cms = arca.sign(arca.login_request("wsfe", NOW), cert_path.read_bytes(), key_path.read_bytes())
    ticket = arca.login("homologacion", cms, fake)
    assert (ticket.token, ticket.sign) == ("token-1", "sign-1")
    assert ticket.expires_at == NOW + timedelta(hours=12)
    assert fake.logins == ["https://wsaahomo.afip.gov.ar/ws/services/LoginCms"]


def test_a_fault_is_refused_with_a_hint(fake, certificate):
    fake.already_authenticated = True
    with pytest.raises(arca.RefusedError) as e:
        arca.login("homologacion", b"x", fake)
    assert e.value.messages[0].code == "ns1:coe.alreadyAuthenticated"
    assert "wait until it expires" in str(e.value)


def test_errors_on_last_number_are_refused():
    def send(url, action, body):
        return (
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<FECompUltimoAutorizadoResponse xmlns="{arca.FE}"><FECompUltimoAutorizadoResult><PtoVta>2</PtoVta>'
            "<CbteTipo>1</CbteTipo><CbteNro>0</CbteNro><Errors><Err><Code>600</Code>"
            "<Msg>ValidacionDeToken: No apareció CUIT en lista de relaciones</Msg></Err></Errors>"
            "</FECompUltimoAutorizadoResult></FECompUltimoAutorizadoResponse></soap:Body></soap:Envelope>"
        ).encode()

    with pytest.raises(arca.RefusedError, match="600: ValidacionDeToken"):
        arca.Wsfe("homologacion", "t", "s", "20123456786", send).last_number(2, 1)


def test_an_answer_that_isnt_soap_is_no_answer():
    with pytest.raises(arca.NoAnswerError):
        arca.Wsfe("homologacion", "t", "s", "1", lambda *a: b"<html>502 Bad Gateway").last_number(2, 1)


def test_mojibake_is_repaired():
    assert arca._text("No apareciÃ³ CUIT") == "No apareció CUIT"
    assert arca._text("No apareció CUIT") == "No apareció CUIT"


def test_amounts_are_sent_with_two_decimals(fake):
    fake.calls.clear()
    client = arca.Wsfe("homologacion", "token-1", "s", "20123456786", fake)
    from datetime import date

    v = arca.Voucher(
        point_of_sale=2,
        cbte_tipo=1,
        number=1,
        concept=2,
        doc_tipo=80,
        doc_nro="30712345671",
        issue_date=date(2026, 11, 2),
        net=Decimal("100"),
        iva=Decimal("21"),
        total=Decimal("121"),
        service_from=date(2026, 10, 1),
        service_to=date(2026, 10, 15),
        due_date=date(2026, 11, 12),
        receiver_iva_condition=1,
    )
    result = client.authorize(v)
    assert result.approved and result.cae == "76000000000001"
    assert result.processed_at.isoformat() == "2026-11-02T10:05:01-03:00"
    assert fake.issued[(2, 1, 1)]["ImpTotal"] == "121.00"
    assert client.lookup(2, 1, 1).cae == "76000000000001"
    assert client.lookup(2, 1, 2) is None
