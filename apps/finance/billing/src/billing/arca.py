"""ARCA's (ex AFIP) web services: WSAA, which gives a ticket to a certificate; WSFEv1, which authorizes invoices;
and the padrón A5 (constancia de inscripción), which says who a CUIT is. The SOAP is written by hand: the few calls
used don't need a WSDL client.

Every call goes through a `Post` (url, SOAPAction, body) -> response body, so tests replace ARCA with a fake.
"""

import base64
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cache
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

AR = ZoneInfo("America/Argentina/Buenos_Aires")

URLS = {
    "homologacion": {
        "wsaa": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
        "wsfe": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
        "padron": "https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA5",
    },
    "produccion": {
        "wsaa": "https://wsaa.afip.gov.ar/ws/services/LoginCms",
        "wsfe": "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
        "padron": "https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA5",
    },
}
ENVIRONMENTS = tuple(URLS)

FACTURA_A = 1
CONCEPT_SERVICES = 2
DOC_CUIT = 80
IVA_21 = 5  # AlicIva's id for 21%
PESOS = "PES"

# WSAA's names for the services a ticket is for.
INVOICES = "wsfe"
PADRON = "ws_sr_padron_a5"

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
WSAA_NS = "http://wsaa.view.sua.dvadac.desein.afip.gov"
FE = "http://ar.gov.afip.dif.FEV1/"
PADRON_NS = "http://a5.soap.ws.server.puc.sr/"

# WSFEv1's "no such invoice" in FECompConsultar.
NOT_FOUND = 602

Post = Callable[[str, str, bytes], bytes]


class ArcaError(Exception):
    """Talking to ARCA went wrong."""


class RefusedError(ArcaError):
    """ARCA answered with errors: it did nothing."""

    def __init__(self, messages: list["Message"]):
        self.messages = messages
        super().__init__("; ".join(str(m) for m in messages))


class NotSentError(ArcaError):
    """The request never reached ARCA: it did nothing."""


class NoAnswerError(ArcaError):
    """The request was sent and no answer came back: ARCA may have acted on it."""


@dataclass(frozen=True)
class Message:
    code: int | str
    message: str

    def __str__(self) -> str:
        hint = _HINTS.get(str(self.code).rpartition(":")[2])
        return f"{self.code}: {self.message}" + (f" ({hint})" if hint else "")


# What WSAA's faults mean for the user.
_HINTS = {
    "coe.alreadyAuthenticated": "ARCA already gave this certificate a ticket that is still valid, for up to 12"
    " hours, and this plugin doesn't have it: wait until it expires",
    "coe.notAuthorized": "the certificate isn't authorized for this service in ARCA: wsfe issues invoices,"
    " ws_sr_padron_a5 looks CUITs up",
    "cms.cert.expired": "the certificate expired: create a new one in ARCA",
    "cms.cert.untrusted": "ARCA didn't issue this certificate for this environment: homologación and"
    " producción certificates are different",
}


# --- HTTP ---


@cache
def _tls() -> ssl.SSLContext:
    """httpx's roots (certifi), at OpenSSL's security level 1: servicios1.afip.gov.ar (WSFEv1 in producción)
    still offers a Diffie-Hellman key the default level refuses."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.set_ciphers("DEFAULT:@SECLEVEL=1")
    return context


def post(url: str, action: str, body: bytes) -> bytes:
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{action}"'}
    try:
        response = httpx.post(url, content=body, headers=headers, verify=_tls(), timeout=httpx.Timeout(60, connect=20))
    except httpx.ConnectError as e:
        raise NotSentError(f"couldn't reach ARCA at {url}: {e}") from e
    except httpx.ConnectTimeout as e:
        raise NotSentError(f"couldn't reach ARCA at {url} in time") from e
    except httpx.TransportError as e:
        raise NoAnswerError(f"ARCA at {url} didn't answer: {e!r}") from e
    # SOAP faults come with status 500, read from the body; anything else that isn't SOAP is no answer.
    return response.content


def _envelope(body: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="{SOAP}"><soap:Body>{body}'
        "</soap:Body></soap:Envelope>"
    ).encode()


def _call(send: Post, url: str, action: str, body: str) -> ET.Element:
    """The first element in the answer's Body. Raises RefusedError for a SOAP fault."""
    answer = send(url, action, _envelope(body))
    try:
        root = ET.fromstring(answer)
    except ET.ParseError:
        raise NoAnswerError(f"ARCA's answer isn't SOAP: {answer[:200]!r}") from None
    found = root.find(f"{{{SOAP}}}Body")
    if found is None or len(found) == 0:
        raise NoAnswerError(f"ARCA's answer has no body: {answer[:200]!r}")
    first = found[0]
    if first.tag == f"{{{SOAP}}}Fault":
        raise RefusedError([Message(first.findtext("faultcode") or "", _text(first.findtext("faultstring")))])
    return first


def _text(value: str | None) -> str:
    """ARCA's messages sometimes come as UTF-8 read as Latin-1."""
    value = (value or "").strip()
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


# --- WSAA ---


def load_certificate(data: bytes) -> x509.Certificate:
    if b"-----BEGIN" in data:
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)


def load_key(data: bytes):
    if b"-----BEGIN" in data:
        return serialization.load_pem_private_key(data, password=None)
    return serialization.load_der_private_key(data, password=None)


def login_request(service: str, now: datetime) -> bytes:
    """WSAA's ticket request (TRA), valid from ten minutes ago (clocks differ) to ten minutes from now."""
    now = now.astimezone(AR)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><loginTicketRequest version="1.0"><header>'
        f"<uniqueId>{int(now.timestamp())}</uniqueId>"
        f"<generationTime>{(now - timedelta(minutes=10)).isoformat(timespec='seconds')}</generationTime>"
        f"<expirationTime>{(now + timedelta(minutes=10)).isoformat(timespec='seconds')}</expirationTime>"
        f"</header><service>{escape(service)}</service></loginTicketRequest>"
    ).encode()


def sign(data: bytes, certificate: bytes, key: bytes) -> bytes:
    """A CMS (PKCS#7) signature in DER with `data` inside, as `openssl smime -sign -nodetach -outform DER`."""
    builder = pkcs7.PKCS7SignatureBuilder().set_data(data)
    builder = builder.add_signer(load_certificate(certificate), load_key(key), hashes.SHA256())
    return builder.sign(serialization.Encoding.DER, [])


@dataclass(frozen=True)
class Ticket:
    token: str
    sign: str
    expires_at: datetime


def login(environment: str, cms: bytes, send: Post = post) -> Ticket:
    """A ticket for the service named in the signed request `cms`."""
    body = (
        f'<wsaa:loginCms xmlns:wsaa="{WSAA_NS}"><wsaa:in0>{base64.b64encode(cms).decode()}</wsaa:in0></wsaa:loginCms>'
    )
    answer = _call(send, URLS[environment]["wsaa"], "", body)
    ticket = ET.fromstring((answer.findtext("{*}loginCmsReturn") or "").encode())
    return Ticket(
        token=ticket.findtext("credentials/token") or "",
        sign=ticket.findtext("credentials/sign") or "",
        expires_at=datetime.fromisoformat(ticket.findtext("header/expirationTime") or ""),
    )


# --- WSFEv1 ---


@dataclass(frozen=True)
class Voucher:
    """One invoice as FECAESolicitar takes it: one line of 21% IVA, in pesos."""

    point_of_sale: int
    cbte_tipo: int
    number: int
    concept: int
    doc_tipo: int
    doc_nro: str
    issue_date: date
    net: Decimal
    iva: Decimal
    total: Decimal
    service_from: date
    service_to: date
    due_date: date
    receiver_iva_condition: int
    alicuota: int = IVA_21


@dataclass
class Authorization:
    """FECAESolicitar's answer for one invoice."""

    approved: bool
    cae: str | None = None
    cae_due: date | None = None
    processed_at: datetime | None = None
    observations: list[Message] = field(default_factory=list)
    errors: list[Message] = field(default_factory=list)


@dataclass(frozen=True)
class Found:
    """An invoice ARCA has, from FECompConsultar."""

    doc_nro: str
    issue_date: date
    total: Decimal
    service_from: date | None
    service_to: date | None
    approved: bool
    cae: str
    cae_due: date | None
    processed_at: datetime | None


class Wsfe:
    def __init__(self, environment: str, token: str, sign: str, cuit: str, send: Post = post):
        self.url = URLS[environment]["wsfe"]
        self.auth = (
            f"<ar:Auth><ar:Token>{escape(token)}</ar:Token><ar:Sign>{escape(sign)}</ar:Sign>"
            f"<ar:Cuit>{escape(cuit)}</ar:Cuit></ar:Auth>"
        )
        self.send = send

    def _call(self, operation: str, body: str) -> ET.Element:
        answer = _call(
            self.send, self.url, FE + operation, f'<ar:{operation} xmlns:ar="{FE}">{self.auth}{body}</ar:{operation}>'
        )
        result = answer.find(f"{{*}}{operation}Result")
        if result is None:
            raise NoAnswerError(f"ARCA's answer to {operation} has no result")
        return result

    def last_number(self, point_of_sale: int, cbte_tipo: int) -> int:
        """The number of the last invoice of this type ARCA authorized at this point of sale; 0 for none."""
        result = self._call(
            "FECompUltimoAutorizado", f"<ar:PtoVta>{point_of_sale}</ar:PtoVta><ar:CbteTipo>{cbte_tipo}</ar:CbteTipo>"
        )
        if errors := _messages(result, "Errors/{*}Err"):
            raise RefusedError(errors)
        return int(result.findtext("{*}CbteNro") or 0)

    def authorize(self, v: Voucher) -> Authorization:
        """FECAESolicitar for one invoice. Errors and rejections come back in the Authorization."""
        detail = (
            f"<ar:Concepto>{v.concept}</ar:Concepto><ar:DocTipo>{v.doc_tipo}</ar:DocTipo>"
            f"<ar:DocNro>{v.doc_nro}</ar:DocNro><ar:CbteDesde>{v.number}</ar:CbteDesde>"
            f"<ar:CbteHasta>{v.number}</ar:CbteHasta><ar:CbteFch>{_date(v.issue_date)}</ar:CbteFch>"
            f"<ar:ImpTotal>{v.total:.2f}</ar:ImpTotal><ar:ImpTotConc>0.00</ar:ImpTotConc>"
            f"<ar:ImpNeto>{v.net:.2f}</ar:ImpNeto><ar:ImpOpEx>0.00</ar:ImpOpEx><ar:ImpTrib>0.00</ar:ImpTrib>"
            f"<ar:ImpIVA>{v.iva:.2f}</ar:ImpIVA><ar:FchServDesde>{_date(v.service_from)}</ar:FchServDesde>"
            f"<ar:FchServHasta>{_date(v.service_to)}</ar:FchServHasta>"
            f"<ar:FchVtoPago>{_date(v.due_date)}</ar:FchVtoPago><ar:MonId>{PESOS}</ar:MonId>"
            f"<ar:MonCotiz>1</ar:MonCotiz>"
            f"<ar:CondicionIVAReceptorId>{v.receiver_iva_condition}</ar:CondicionIVAReceptorId>"
            f"<ar:Iva><ar:AlicIva><ar:Id>{v.alicuota}</ar:Id><ar:BaseImp>{v.net:.2f}</ar:BaseImp>"
            f"<ar:Importe>{v.iva:.2f}</ar:Importe></ar:AlicIva></ar:Iva>"
        )
        result = self._call(
            "FECAESolicitar",
            "<ar:FeCAEReq><ar:FeCabReq><ar:CantReg>1</ar:CantReg>"
            f"<ar:PtoVta>{v.point_of_sale}</ar:PtoVta><ar:CbteTipo>{v.cbte_tipo}</ar:CbteTipo></ar:FeCabReq>"
            f"<ar:FeDetReq><ar:FECAEDetRequest>{detail}</ar:FECAEDetRequest></ar:FeDetReq></ar:FeCAEReq>",
        )
        errors = _messages(result, "Errors/{*}Err")
        response = result.find("{*}FeDetResp/{*}FECAEDetResponse")
        if response is None:
            return Authorization(approved=False, errors=errors)
        observations = _messages(response, "Observaciones/{*}Obs")
        approved = response.findtext("{*}Resultado") == "A" and bool(response.findtext("{*}CAE"))
        return Authorization(
            approved=approved,
            cae=response.findtext("{*}CAE") or None,
            cae_due=_parse_date(response.findtext("{*}CAEFchVto")),
            processed_at=_parse_datetime(result.findtext("{*}FeCabResp/{*}FchProceso")),
            observations=observations,
            errors=errors,
        )

    def lookup(self, point_of_sale: int, cbte_tipo: int, number: int) -> Found | None:
        """The invoice ARCA has with this number, or None."""
        result = self._call(
            "FECompConsultar",
            f"<ar:FeCompConsReq><ar:CbteTipo>{cbte_tipo}</ar:CbteTipo><ar:CbteNro>{number}</ar:CbteNro>"
            f"<ar:PtoVta>{point_of_sale}</ar:PtoVta></ar:FeCompConsReq>",
        )
        errors = _messages(result, "Errors/{*}Err")
        if any(e.code == NOT_FOUND for e in errors):
            return None
        if errors:
            raise RefusedError(errors)
        got = result.find("{*}ResultGet")
        if got is None:
            return None
        return Found(
            doc_nro=got.findtext("{*}DocNro") or "",
            issue_date=_parse_date(got.findtext("{*}CbteFch")),
            total=Decimal(got.findtext("{*}ImpTotal") or "0"),
            service_from=_parse_date(got.findtext("{*}FchServDesde")),
            service_to=_parse_date(got.findtext("{*}FchServHasta")),
            approved=got.findtext("{*}Resultado") == "A",
            cae=got.findtext("{*}CodAutorizacion") or "",
            cae_due=_parse_date(got.findtext("{*}FchVto")),
            processed_at=_parse_datetime(got.findtext("{*}FchProceso")),
        )


def _messages(element: ET.Element, path: str) -> list[Message]:
    out = []
    for m in element.findall("{*}" + path):
        code = m.findtext("{*}Code") or ""
        out.append(Message(int(code) if code.isdigit() else code, _text(m.findtext("{*}Msg"))))
    return out


def _date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value or value == "NULL":
        return None
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_datetime(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value or value == "NULL":
        return None
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=AR)


# --- Padrón A5 ---

# The padrón's impuestos that say a CUIT's IVA condition.
IVA_TAX, IVA_EXEMPT_TAX = "30", "32"


@dataclass(frozen=True)
class Person:
    """Who a CUIT is, as the padrón's constancia de inscripción says."""

    cuit: str
    name: str
    address: str
    # responsable_inscripto, monotributo or exento; None when the padrón says none of them.
    iva_condition: str | None
    active: bool
    notes: list[str]


def padron_person(environment: str, token: str, sign: str, cuit: str, wanted: str, send: Post = post) -> Person | None:
    """`wanted`'s constancia, asked as `cuit` (getPersona_v2); None when the padrón has no one with that CUIT."""
    body = (
        f'<a5:getPersona_v2 xmlns:a5="{PADRON_NS}"><token>{escape(token)}</token><sign>{escape(sign)}</sign>'
        f"<cuitRepresentada>{escape(cuit)}</cuitRepresentada><idPersona>{escape(wanted)}</idPersona></a5:getPersona_v2>"
    )
    try:
        answer = _call(send, URLS[environment]["padron"], "", body)
    except RefusedError as e:
        if any("no existe persona" in m.message.lower() for m in e.messages):
            return None
        raise
    found = answer.find("{*}personaReturn")
    if found is None:
        raise NoAnswerError("the padrón's answer has no personaReturn")
    general = found.find("{*}datosGenerales")
    failed = found.find("{*}errorConstancia")
    notes = [_text(e.text) for e in found.findall("{*}errorConstancia/{*}error")]
    if general is None and failed is None:
        return None
    who = general if general is not None else failed
    name = _text(who.findtext("{*}razonSocial")) or " ".join(
        p for p in (_text(who.findtext("{*}apellido")), _text(who.findtext("{*}nombre"))) if p
    )
    return Person(
        cuit=wanted,
        name=name,
        address=_address(general.find("{*}domicilioFiscal")) if general is not None else "",
        iva_condition=_iva_condition(found),
        active=general is not None and general.findtext("{*}estadoClave") == "ACTIVO",
        notes=notes,
    )


def _address(place: ET.Element | None) -> str:
    """Calle y número, localidad (código postal), provincia; CP and the code when there's no localidad."""
    if place is None:
        return ""
    street, town = _text(place.findtext("{*}direccion")), _text(place.findtext("{*}localidad"))
    postcode, province = _text(place.findtext("{*}codPostal")), _text(place.findtext("{*}descripcionProvincia"))
    town = f"{town} ({postcode})" if town and postcode else town or (f"CP {postcode}" if postcode else "")
    return ", ".join(p for p in (street, town, province) if p)


def _iva_condition(found: ET.Element) -> str | None:
    if found.find("{*}datosMonotributo") is not None:
        return "monotributo"
    taxes = {t.findtext("{*}idImpuesto") for t in found.findall("{*}datosRegimenGeneral/{*}impuesto")}
    if IVA_TAX in taxes:
        return "responsable_inscripto"
    if IVA_EXEMPT_TAX in taxes:
        return "exento"
    return None
