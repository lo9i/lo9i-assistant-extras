"""A fake ARCA that answers the SOAP the plugin sends, a test certificate, and a set-up database."""

import base64
import re
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from billing import arca, db, service

ISSUER_CUIT = "20123456786"
CUSTOMER_CUIT = "30712345671"
# 2026-11-02 10:00 in Buenos Aires.
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=arca.AR)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_certificate(tmp_path, name="cert", key=None):
    """A self-signed certificate like ARCA's (serialNumber "CUIT ..."), and its key, as PEM files."""
    key = key or _key()
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "lo9i-billing"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {ISSUER_CUIT}"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2028, 1, 1, tzinfo=UTC))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def _soap(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<soap:Body>{body}</soap:Body></soap:Envelope>"
    ).encode()


def _fault(code: str, message: str) -> bytes:
    return _soap(
        f'<soap:Fault><faultcode xmlns:ns1="http://xml.apache.org/axis/">{code}</faultcode>'
        f"<faultstring>{message}</faultstring></soap:Fault>"
    )


# The fake padrón's people, as A5 answers them: a company that's responsable inscripto, a monotributista, and a
# CUIT the padrón can't give a constancia for.
PEOPLE = {
    CUSTOMER_CUIT: (
        "<datosGenerales><domicilioFiscal><codPostal>1638</codPostal><descripcionProvincia>BUENOS AIRES"
        "</descripcionProvincia><direccion>AV DEL LIBERTADOR 500</direccion><localidad>VICENTE LOPEZ</localidad>"
        "<tipoDomicilio>FISCAL</tipoDomicilio></domicilioFiscal><estadoClave>ACTIVO</estadoClave>"
        f"<idPersona>{CUSTOMER_CUIT}</idPersona><razonSocial>ACME SOCIEDAD ANONIMA</razonSocial>"
        "<tipoClave>CUIT</tipoClave><tipoPersona>JURIDICA</tipoPersona></datosGenerales>"
        "<datosRegimenGeneral><impuesto><descripcionImpuesto>GANANCIAS SOCIEDADES</descripcionImpuesto>"
        "<idImpuesto>10</idImpuesto></impuesto><impuesto><descripcionImpuesto>IVA</descripcionImpuesto>"
        "<idImpuesto>30</idImpuesto></impuesto></datosRegimenGeneral>"
    ),
    "20111111112": (
        "<datosGenerales><apellido>PEREZ</apellido><domicilioFiscal><codPostal>1043</codPostal>"
        "<descripcionProvincia>CIUDAD AUTONOMA BUENOS AIRES</descripcionProvincia>"
        "<direccion>AV CORRIENTES 1234 Piso:5</direccion></domicilioFiscal><estadoClave>ACTIVO</estadoClave>"
        "<idPersona>20111111112</idPersona><nombre>JUAN</nombre><tipoPersona>FISICA</tipoPersona></datosGenerales>"
        "<datosMonotributo><categoriaMonotributo><descripcionCategoria>D LOCACIONES DE SERVICIO"
        "</descripcionCategoria><idCategoria>4</idCategoria><idImpuesto>20</idImpuesto></categoriaMonotributo>"
        "<impuesto><idImpuesto>20</idImpuesto></impuesto></datosMonotributo>"
    ),
    "30700000008": (
        "<errorConstancia><apellido>GLOBEX SRL</apellido><error>La CUIT no registra impuestos activos</error>"
        "<idPersona>30700000008</idPersona></errorConstancia>"
    ),
}


class FakeArca:
    """WSAA and WSFEv1, as far as the plugin uses them. Switches make the next call fail in each way a real
    one can."""

    def __init__(self):
        self.calls: list[str] = []
        self.logins: list[str] = []  # each login's URL
        self.last: dict[tuple[int, int], int] = {}
        self.issued: dict[tuple[int, int, int], dict] = {}
        self.already_authenticated = False
        self.unreachable = False  # the next FECAESolicitar never reaches ARCA
        self.lose_answer = False  # the next FECAESolicitar is authorized, and its answer lost
        self.lose_request = False  # the next FECAESolicitar is lost on its way
        self.reject = None  # the next FECAESolicitar is rejected with these (code, message)
        # The points of sale enabled for web services: number -> EmisionTipo.
        self.points_of_sale = {2: "CAE - Ri Iva", 9: "CAE - Monotributo"}
        self.tokens: dict[str, str] = {}  # each ticket's token, and the service it's for
        self.people = dict(PEOPLE)  # the padrón: CUIT -> getPersona_v2's personaReturn

    def __call__(self, url: str, action: str, body: bytes) -> bytes:
        envelope = ET.fromstring(body)
        operation = envelope.find("{*}Body")[0]
        name = operation.tag.split("}")[1]
        self.calls.append(name)
        if name == "loginCms":
            return self._login(url, operation)
        if name == "getPersona_v2":
            return self._padron(url, action, operation)
        assert action == arca.FE + name
        assert url in (u["wsfe"] for u in arca.URLS.values())
        assert operation.findtext("{*}Auth/{*}Cuit") == ISSUER_CUIT
        assert self.tokens[operation.findtext("{*}Auth/{*}Token")] == arca.INVOICES
        return getattr(self, "_" + name)(operation)

    def _login(self, url, operation):
        assert url in (u["wsaa"] for u in arca.URLS.values())
        if self.already_authenticated:
            return _fault(
                "ns1:coe.alreadyAuthenticated", "El CEE ya posee un TA valido para el acceso al WSN solicitado"
            )
        cms = base64.b64decode(operation.findtext("{*}in0"))
        # The request travels inside the signature (nodetach).
        tra = re.search(rb"<\?xml.*</loginTicketRequest>", cms, re.S).group()
        request = ET.fromstring(tra)
        service = request.findtext("service")
        assert service in (arca.INVOICES, arca.PADRON)
        generated = datetime.fromisoformat(request.findtext("header/generationTime"))
        expires = generated + timedelta(minutes=10, hours=12)
        self.logins.append(url)
        n = len(self.logins)
        self.tokens[f"token-{n}"] = service
        ticket = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><loginTicketResponse version="1.0"><header>'
            f"<source>CN=wsaahomo</source><destination>SERIALNUMBER=CUIT {ISSUER_CUIT}</destination>"
            f"<uniqueId>{n}</uniqueId><generationTime>{generated.isoformat()}</generationTime>"
            f"<expirationTime>{expires.isoformat(timespec='milliseconds')}</expirationTime></header>"
            f"<credentials><token>token-{n}</token><sign>sign-{n}</sign></credentials></loginTicketResponse>"
        )
        return _soap(
            f'<loginCmsResponse xmlns="{arca.WSAA_NS}"><loginCmsReturn>{escape(ticket)}</loginCmsReturn>'
            "</loginCmsResponse>"
        )

    def _padron(self, url, action, op):
        assert action == "" and url in (u["padron"] for u in arca.URLS.values())
        # Unqualified, as A5's schema has them.
        assert op.find("token") is not None and self.tokens[op.findtext("token")] == arca.PADRON
        assert op.findtext("cuitRepresentada") == ISSUER_CUIT
        person = self.people.get(op.findtext("idPersona"))
        if person is None:
            return _fault("soap:Server", "No existe persona con ese Id")
        return _soap(
            f'<ns2:getPersona_v2Response xmlns:ns2="{arca.PADRON_NS}"><personaReturn>{person}</personaReturn>'
            "</ns2:getPersona_v2Response>"
        )

    def _FECompUltimoAutorizado(self, op):  # noqa: N802 - named after ARCA's operations
        key = (int(op.findtext("{*}PtoVta")), int(op.findtext("{*}CbteTipo")))
        if key[0] not in self.points_of_sale:
            return _soap(
                f'<FECompUltimoAutorizadoResponse xmlns="{arca.FE}"><FECompUltimoAutorizadoResult>'
                f"<PtoVta>{key[0]}</PtoVta><CbteTipo>{key[1]}</CbteTipo><Errors><Err><Code>11002</Code><Msg>El punto"
                " de venta no se encuentra habilitado a usar en el presente WS. Ver metodo FEParamGetPtosVenta</Msg>"
                "</Err></Errors></FECompUltimoAutorizadoResult></FECompUltimoAutorizadoResponse>"
            )
        return _soap(
            f'<FECompUltimoAutorizadoResponse xmlns="{arca.FE}"><FECompUltimoAutorizadoResult>'
            f"<PtoVta>{key[0]}</PtoVta><CbteTipo>{key[1]}</CbteTipo><CbteNro>{self.last.get(key, 0)}</CbteNro>"
            "</FECompUltimoAutorizadoResult></FECompUltimoAutorizadoResponse>"
        )

    def _FEParamGetPtosVenta(self, op):  # noqa: N802 - named after ARCA's operations
        if not self.points_of_sale:
            inner = (
                "<Errors><Err><Code>602</Code><Msg>Sin Resultados: - Metodo FEParamGetPtosVenta</Msg></Err></Errors>"
            )
        else:
            inner = (
                "<ResultGet>"
                + "".join(
                    f"<PtoVenta><Nro>{n}</Nro><EmisionTipo>{kind}</EmisionTipo><Bloqueado>N</Bloqueado>"
                    "<FchBaja>NULL</FchBaja></PtoVenta>"
                    for n, kind in self.points_of_sale.items()
                )
                + "</ResultGet>"
            )
        return _soap(
            f'<FEParamGetPtosVentaResponse xmlns="{arca.FE}"><FEParamGetPtosVentaResult>{inner}'
            "</FEParamGetPtosVentaResult></FEParamGetPtosVentaResponse>"
        )

    def _FECAESolicitar(self, op):  # noqa: N802 - named after ARCA's operations
        if self.unreachable:
            self.unreachable = False
            raise arca.NotSentError("couldn't reach ARCA: connection refused")
        if self.lose_request:
            self.lose_request = False
            raise arca.NoAnswerError("read timed out")
        head = op.find("{*}FeCAEReq/{*}FeCabReq")
        detail = op.find("{*}FeCAEReq/{*}FeDetReq/{*}FECAEDetRequest")
        pto, tipo = int(head.findtext("{*}PtoVta")), int(head.findtext("{*}CbteTipo"))
        fields = {child.tag.split("}")[1]: child.text for child in detail if len(child) == 0}
        fields["AlicIva"] = {c.tag.split("}")[1]: c.text for c in detail.find("{*}Iva/{*}AlicIva")}
        fields["order"] = [child.tag.split("}")[1] for child in detail]
        number = int(fields["CbteDesde"])
        problems = []
        if self.reject:
            problems, self.reject = [self.reject], None
        if number != self.last.get((pto, tipo), 0) + 1:
            problems.append((10016, "El numero o fecha del comprobante no se corresponde con el proximo a autorizar."))
        if float(fields["ImpTotal"]) != round(float(fields["ImpNeto"]) + float(fields["ImpIVA"]), 2):
            problems.append((10048, "ImpTotal debe ser igual a la suma de ImpNeto, ImpIVA, ImpTrib..."))
        detail_xml = (
            f"<Concepto>{fields['Concepto']}</Concepto><DocTipo>{fields['DocTipo']}</DocTipo>"
            f"<DocNro>{fields['DocNro']}</DocNro><CbteDesde>{number}</CbteDesde><CbteHasta>{number}</CbteHasta>"
            f"<CbteFch>{fields['CbteFch']}</CbteFch>"
        )
        if problems:
            obs = "".join(f"<Obs><Code>{c}</Code><Msg>{escape(m)}</Msg></Obs>" for c, m in problems)
            return self._cae_answer(
                pto,
                tipo,
                "R",
                f"<FECAEDetResponse>{detail_xml}<Resultado>R</Resultado>"
                f"<Observaciones>{obs}</Observaciones><CAE></CAE><CAEFchVto></CAEFchVto></FECAEDetResponse>",
            )
        self.last[(pto, tipo)] = number
        cae = str(76000000000000 + number)
        due = (datetime.strptime(fields["CbteFch"], "%Y%m%d") + timedelta(days=10)).strftime("%Y%m%d")
        self.issued[(pto, tipo, number)] = fields | {"CAE": cae, "CAEFchVto": due}
        if self.lose_answer:
            self.lose_answer = False
            raise arca.NoAnswerError("read timed out")
        return self._cae_answer(
            pto,
            tipo,
            "A",
            f"<FECAEDetResponse>{detail_xml}<Resultado>A</Resultado>"
            f"<CAE>{cae}</CAE><CAEFchVto>{due}</CAEFchVto></FECAEDetResponse>",
        )

    def _cae_answer(self, pto, tipo, result, detail):
        return _soap(
            f'<FECAESolicitarResponse xmlns="{arca.FE}"><FECAESolicitarResult><FeCabResp><Cuit>{ISSUER_CUIT}</Cuit>'
            f"<PtoVta>{pto}</PtoVta><CbteTipo>{tipo}</CbteTipo><FchProceso>20261102100501</FchProceso>"
            f"<CantReg>1</CantReg><Resultado>{result}</Resultado><Reproceso>N</Reproceso></FeCabResp>"
            f"<FeDetResp>{detail}</FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
        )

    def _FECompConsultar(self, op):  # noqa: N802 - named after ARCA's operations
        req = op.find("{*}FeCompConsReq")
        key = (int(req.findtext("{*}PtoVta")), int(req.findtext("{*}CbteTipo")), int(req.findtext("{*}CbteNro")))
        found = self.issued.get(key)
        if found is None:
            inner = (
                "<Errors><Err><Code>602</Code><Msg>No existen datos en nuestros registros para los parametros"
                " ingresados.</Msg></Err></Errors>"
            )
        else:
            inner = (
                f"<ResultGet><Concepto>2</Concepto><DocTipo>80</DocTipo><DocNro>{found['DocNro']}</DocNro>"
                f"<CbteDesde>{key[2]}</CbteDesde><CbteHasta>{key[2]}</CbteHasta><CbteFch>{found['CbteFch']}</CbteFch>"
                f"<ImpTotal>{found['ImpTotal']}</ImpTotal><FchServDesde>{found['FchServDesde']}</FchServDesde>"
                f"<FchServHasta>{found['FchServHasta']}</FchServHasta><Resultado>A</Resultado>"
                f"<CodAutorizacion>{found['CAE']}</CodAutorizacion><EmisionTipo>CAE</EmisionTipo>"
                f"<FchVto>{found['CAEFchVto']}</FchVto><FchProceso>20261102100501</FchProceso>"
                f"<PtoVta>{key[0]}</PtoVta><CbteTipo>{key[1]}</CbteTipo></ResultGet>"
            )
        return _soap(
            f'<FECompConsultarResponse xmlns="{arca.FE}"><FECompConsultarResult>{inner}'
            "</FECompConsultarResult></FECompConsultarResponse>"
        )


@pytest.fixture
def fake():
    return FakeArca()


@pytest.fixture
def certificate(tmp_path):
    return make_certificate(tmp_path)


def set_up(conn, certificate):
    cert_path, key_path = certificate
    service.setup(
        conn,
        cuit_="20-12345678-6",
        business_name="Ana Pérez Software",
        address="Av. Corrientes 1234, CABA",
        iva_condition="responsable_inscripto",
        activity_start="2015-03-01",
        iibb="901-123456-7",
        point_of_sale=2,
        certificate_path=str(cert_path),
        key_path=str(key_path),
    )
    service.add_customer(
        conn,
        "Acme S.A.",
        CUSTOMER_CUIT,
        address="Av. Libertador 500, CABA",
        email="pagos@acme.example",
        description="Servicios de desarrollo de software",
        monthly_net=7_500_000,
        id="acme",
    )


@pytest.fixture
def conn(certificate):
    conn = db.connect(":memory:")
    set_up(conn, certificate)
    yield conn
    conn.close()


@pytest.fixture
def issue(conn, fake, tmp_path):
    """create_invoice against the fake, on NOW unless `now` says otherwise."""

    def issue(customer="acme", period_from="2026-10-01", period_to="2026-10-15", now=NOW, **kwargs):
        return service.create_invoice(
            conn, customer, period_from, period_to, pdf_dir=tmp_path / "invoices", send=fake, now=now, **kwargs
        )

    return issue
