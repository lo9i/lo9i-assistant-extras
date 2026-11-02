"""The issuer's setup, customers with their monthly amounts, and invoices: previewing a month's, and issuing
one with ARCA without ever issuing it twice.

Issuing (create_invoice) never issues two invoices for one customer and period in an environment:

1. An invoice already authorized for that period is returned as it is; one overlapping the period refuses.
2. Its row is saved as `sent`, with the number it asks for (ARCA's last + 1), before it's sent.
3. ARCA's answer makes it `authorized` (with its CAE) or `rejected` (nothing issued, the number freed).
4. Without an answer it stays `sent`. Before issuing anything else, ARCA is asked for that number
   (FECompConsultar): if ARCA has it, for this customer and amount, it's recorded as authorized; otherwise it
   goes back to draft, and is issued anew when its period is.
"""

import calendar
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from . import arca, pdf

CENT = Decimal("0.01")
IVA_RATE = Decimal("0.21")

# A customer's IVA condition: its id in WSFEv1 (FEParamGetCondicionIvaReceptor) and how invoices name it.
IVA_CONDITIONS = {
    "responsable_inscripto": (1, "IVA Responsable Inscripto"),
    "exento": (4, "IVA Sujeto Exento"),
    "consumidor_final": (5, "Consumidor Final"),
    "monotributo": (6, "Responsable Monotributo"),
    "monotributo_social": (13, "Monotributista Social"),
    "no_alcanzado": (15, "IVA No Alcanzado"),
}
# Who can receive a Factura A: responsables inscriptos, and monotributistas since RG 5003.
FACTURA_A_RECEIVERS = ("responsable_inscripto", "monotributo", "monotributo_social")

# What issuing needs, besides a certificate and key for the environment.
REQUIRED = ("cuit", "business_name", "address", "iva_condition", "activity_start", "iibb", "point_of_sale")
DEFAULTS = {
    "environment": "homologacion",
    "iva_condition": "responsable_inscripto",  # the only one that issues a Factura A
    "payment_days": "10",
    "sale_condition": "Cuenta corriente",
}

TICKET_MARGIN = timedelta(minutes=5)


class BillingError(Exception):
    pass


class InvalidError(BillingError):
    pass


class NotFoundError(BillingError):
    pass


class NotReadyError(BillingError):
    """The setup lacks something issuing needs."""


class AlreadyIssuedError(BillingError):
    pass


class RejectedError(BillingError):
    """ARCA rejected the invoice: nothing was issued."""


class UnconfirmedError(BillingError):
    """The invoice was sent and it's not known whether ARCA issued it."""


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def today() -> date:
    return datetime.now(arca.AR).date()


# --- Input checks ---


def slugify(s: str) -> str:
    """Lowercase ASCII slug, e.g. "Peñalolén S.A." -> "penalolen-s-a"."""
    ascii_ = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "-".join(p for p in re.split(r"[^a-z0-9]+", ascii_.lower()) if p)


def cuit(value: str | int) -> str:
    """The CUIT's 11 digits, checked with its verification digit."""
    digits = re.sub(r"[\s.-]", "", str(value))
    if not re.fullmatch(r"\d{11}", digits):
        raise InvalidError(f"a CUIT has 11 digits, like 20-12345678-6: got {value!r}")
    total = sum(int(d) * w for d, w in zip(digits[:10], (5, 4, 3, 2, 7, 6, 5, 4, 3, 2), strict=True))
    check = (11 - total % 11) % 11
    if check == 10 or check != int(digits[10]):
        raise InvalidError(f"CUIT {value} is wrong: its verification digit doesn't match")
    return digits


def _required(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise InvalidError(f"{name} can't be empty")
    return value


def _date(value: str | date, name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise InvalidError(f"{name} must be a date like 2026-10-31: got {value!r}") from None


def _month(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", value.strip())
    if not m or not 1 <= int(m[2]) <= 12:
        raise InvalidError(f"a month is written like 2026-10: got {value!r}")
    return int(m[1]), int(m[2])


def money(value: float | str | Decimal, name: str = "amount") -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)
    except InvalidOperation:
        raise InvalidError(f"{name} must be a number: got {value!r}") from None
    if amount <= 0:
        raise InvalidError(f"{name} must be more than 0")
    return amount


def _iva_condition(value: str) -> str:
    value = value.strip().lower().replace(" ", "_")
    if value not in IVA_CONDITIONS:
        raise InvalidError(f"iva_condition must be one of {', '.join(IVA_CONDITIONS)}: got {value!r}")
    return value


def amounts(net: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Net, IVA at 21% and total."""
    iva = (net * IVA_RATE).quantize(CENT, ROUND_HALF_UP)
    return net, iva, net + iva


# --- Setup ---


def settings(conn: sqlite3.Connection) -> dict[str, str]:
    return DEFAULTS | {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def setup(
    conn: sqlite3.Connection,
    *,
    cuit_: str | None = None,
    business_name: str | None = None,
    address: str | None = None,
    iva_condition: str | None = None,
    activity_start: str | None = None,
    iibb: str | None = None,
    point_of_sale: int | None = None,
    environment: str | None = None,
    certificate_path: str | None = None,
    key_path: str | None = None,
    payment_days: int | None = None,
    sale_condition: str | None = None,
) -> dict:
    """Change the given settings; the certificate and key are those of `environment`, or the current one."""
    current = settings(conn)
    changes: dict[str, str] = {}
    if cuit_ is not None:
        changes["cuit"] = cuit(cuit_)
    if business_name is not None:
        changes["business_name"] = _required(business_name, "business_name")
    if address is not None:
        changes["address"] = _required(address, "address")
    if iva_condition is not None:
        if _iva_condition(iva_condition) != "responsable_inscripto":
            raise InvalidError("only a responsable_inscripto issues a Factura A, the invoice this plugin issues")
        changes["iva_condition"] = "responsable_inscripto"
    if activity_start is not None:
        changes["activity_start"] = _date(activity_start, "activity_start").isoformat()
    if iibb is not None:
        changes["iibb"] = _required(iibb, "iibb")
    if point_of_sale is not None:
        if not 1 <= point_of_sale <= 99998:
            raise InvalidError("point_of_sale is a number from 1 to 99998")
        changes["point_of_sale"] = str(point_of_sale)
    if environment is not None:
        if environment not in arca.ENVIRONMENTS:
            raise InvalidError(f"environment must be {' or '.join(arca.ENVIRONMENTS)}")
        changes["environment"] = environment
    if payment_days is not None:
        if not 0 <= payment_days <= 365:
            raise InvalidError("payment_days is a number of days from 0 to 365")
        changes["payment_days"] = str(payment_days)
    if sale_condition is not None:
        changes["sale_condition"] = _required(sale_condition, "sale_condition")
    env = changes.get("environment", current["environment"])
    changes |= _certificate(current, env, certificate_path, key_path)
    with _transaction(conn):
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            list(changes.items()),
        )
    return issuer(conn)


def set_certificate(
    conn: sqlite3.Connection, environment: str, certificate_path: str | None, key_path: str | None
) -> dict:
    """The certificate and key of `environment`, without switching to it."""
    if environment not in arca.ENVIRONMENTS:
        raise InvalidError(f"environment must be {' or '.join(arca.ENVIRONMENTS)}")
    changes = _certificate(settings(conn), environment, certificate_path, key_path)
    with _transaction(conn):
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            list(changes.items()),
        )
    return issuer(conn)


def _certificate(current: dict[str, str], env: str, certificate_path: str | None, key_path: str | None) -> dict:
    """The settings for `env`'s certificate and key, checked; the one not given stays as it is."""
    if certificate_path is None and key_path is None:
        return {}
    cert = _path(certificate_path) if certificate_path is not None else current.get(f"certificate_path.{env}")
    key = _path(key_path) if key_path is not None else current.get(f"key_path.{env}")
    if cert and key:
        _check_pair(cert, key)
    elif cert:
        _read_certificate(cert)
    elif key:
        _read_key(key)
    return {f"certificate_path.{env}": cert or "", f"key_path.{env}": key or ""}


def _path(value: str) -> str:
    """Only the file's path is kept, never what's in it."""
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise InvalidError(f"give the file's full path: got {value!r}")
    if not path.is_file():
        raise InvalidError(f"there's no file at {path}")
    return str(path)


def _read_certificate(path: str):
    try:
        return arca.load_certificate(Path(path).read_bytes())
    except OSError as e:
        raise NotReadyError(f"can't read the certificate at {path}: {e.strerror}") from None
    except ValueError:
        raise InvalidError(f"{path} isn't a certificate (PEM or DER)") from None


def _read_key(path: str):
    try:
        return arca.load_key(Path(path).read_bytes())
    except OSError as e:
        raise NotReadyError(f"can't read the private key at {path}: {e.strerror}") from None
    except TypeError:
        raise InvalidError(f"the private key at {path} has a password; save it without one") from None
    except ValueError:
        raise InvalidError(f"{path} isn't a private key (PEM or DER)") from None


def _public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _check_pair(certificate: str, key: str) -> None:
    if _public(_read_certificate(certificate).public_key()) != _public(_read_key(key).public_key()):
        raise InvalidError(f"the private key at {key} isn't the certificate's ({certificate})")


def _certificate_info(path: str, now: datetime) -> dict:
    try:
        cert = _read_certificate(path)
    except BillingError as e:
        return {"error": str(e)}
    serial = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
    return {
        "subject": cert.subject.rfc4514_string({NameOID.SERIAL_NUMBER: "serialNumber"}),
        "cuit": re.sub(r"\D", "", str(serial[0].value)) if serial else None,
        "valid_until": cert.not_valid_after_utc.astimezone(arca.AR).isoformat(timespec="minutes"),
        "expired": cert.not_valid_after_utc < now,
    }


# The certificate's name in the request (its CN); ARCA's own name for it, the alias or DN name, is chosen there.
REQUEST_NAME = "lo9i-billing"


def certificate_request(conn: sqlite3.Connection, environment: str | None = None) -> dict:
    """The request (CSR) ARCA issues `environment`'s certificate for, made from its private key with the issuer's
    CUIT and razón social. Changes nothing: the certificate ARCA gives back is set as any other."""
    values = settings(conn)
    env = environment or values["environment"]
    if env not in arca.ENVIRONMENTS:
        raise InvalidError(f"environment must be {' or '.join(arca.ENVIRONMENTS)}")
    key_path = values.get(f"key_path.{env}")
    lacking = [k for k in ("cuit", "business_name") if not values.get(k)] + ([] if key_path else ["key_path"])
    if lacking:
        raise NotReadyError(f"the certificate request for {env} needs {', '.join(lacking)} in setup")
    try:
        pem = arca.certificate_request(_read_key(key_path), values["cuit"], values["business_name"], REQUEST_NAME)
    except ValueError as e:
        raise InvalidError(f"the private key at {key_path}: {e}") from None
    return {
        "environment": env,
        "subject": f"C=AR, O={values['business_name']}, CN={REQUEST_NAME}, serialNumber=CUIT {values['cuit']}",
        "file_name": f"arca-{env}.csr",
        "csr": pem.decode(),
    }


def missing(values: dict[str, str]) -> list[str]:
    env = values["environment"]
    needed = [*REQUIRED, f"certificate_path.{env}", f"key_path.{env}"]
    return [k.split(".")[0] for k in needed if not values.get(k)]


def issuer(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The setup, each environment's certificate, and what issuing still needs."""
    now = now or datetime.now(arca.AR)
    values = settings(conn)
    env = values["environment"]
    certificates = {}
    for e in arca.ENVIRONMENTS:
        cert, key = values.get(f"certificate_path.{e}"), values.get(f"key_path.{e}")
        if cert or key:
            certificates[e] = {"certificate_path": cert, "key_path": key} | (
                _certificate_info(cert, now) if cert else {}
            )
    lacking = missing(values)
    return {
        **{k: values.get(k) for k in REQUIRED},
        "point_of_sale": int(values["point_of_sale"]) if values.get("point_of_sale") else None,
        "environment": env,
        "payment_days": int(values["payment_days"]),
        "sale_condition": values["sale_condition"],
        "certificates": certificates,
        "urls": arca.URLS[env],
        "missing": lacking,
        "ready": not lacking,
    }


def _ready(conn: sqlite3.Connection) -> dict[str, str]:
    values = settings(conn)
    if lacking := missing(values):
        raise NotReadyError(f"billing isn't set up for {values['environment']}: setup needs {', '.join(lacking)}")
    return values


# --- Customers ---

# The text of an invoice's email when the customer has none of its own, and the placeholders a text can use.
EMAIL_TEXT = """Hola,

Adjunto la Factura A {numero} por los servicios del {periodo}, por {total}. Vence el {vencimiento}.

Saludos,
{emisor}"""
EMAIL_FIELDS = {
    "cliente": "the customer's razón social",
    "numero": "the invoice's number, like 00002-00000042",
    "periodo": "the period, like 01/10/2026 al 15/10/2026",
    "total": "the total, like $ 4.537.500,00",
    "vencimiento": "the payment due date",
    "emisor": "your razón social",
}
EMAIL = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")


def emails(value: str | list[str]) -> list[str]:
    """Addresses separated by commas, semicolons or spaces, each checked; each once."""
    found = re.split(r"[\s,;]+", value) if isinstance(value, str) else value
    out: list[str] = []
    for address in (a.strip() for a in found):
        if not address:
            continue
        if not EMAIL.fullmatch(address):
            raise InvalidError(f"{address!r} isn't an email address")
        if address.lower() not in (a.lower() for a in out):
            out.append(address)
    return out


def _email_text(value: str) -> str:
    """'' when it's the standard text, so a customer without one of its own follows it if it changes."""
    text = value.replace("\r\n", "\n").strip()
    if text == EMAIL_TEXT:
        return ""
    unknown = {f for f in re.findall(r"{(\w+)}", text) if f not in EMAIL_FIELDS}
    if unknown:
        raise InvalidError(
            f"the email text has {', '.join('{' + f + '}' for f in sorted(unknown))}: its placeholders are"
            f" {', '.join('{' + f + '}' for f in EMAIL_FIELDS)}"
        )
    return text


def _customer(conn: sqlite3.Connection, row: sqlite3.Row, on: date | None = None) -> dict:
    prices = conn.execute(
        "SELECT effective_from, monthly_net FROM prices WHERE customer_id = ? ORDER BY effective_from", (row["id"],)
    ).fetchall()
    on = on or today()
    current = price_on(conn, row["id"], on)
    return {
        "id": row["id"],
        "name": row["name"],
        "cuit": row["cuit"],
        "iva_condition": row["iva_condition"],
        "address": row["address"],
        "email": row["email"],
        "emails": emails(row["email"]),
        "email_text": row["email_text"] or EMAIL_TEXT,
        "description": row["description"],
        "monthly_net": float(current) if current is not None else None,
        "prices": [
            {"effective_from": p["effective_from"] or None, "monthly_net": float(Decimal(p["monthly_net"]))}
            for p in prices
        ],
    }


def find_customer(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """By id, or by CUIT."""
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (ref.strip(),)).fetchone()
    digits = re.sub(r"[\s.-]", "", ref)
    if row is None and digits.isdigit():
        row = conn.execute("SELECT * FROM customers WHERE cuit = ?", (digits,)).fetchone()
    if row is None:
        ids = [r["id"] for r in conn.execute("SELECT id FROM customers ORDER BY id")]
        raise NotFoundError(f"no customer {ref!r}; customers: {', '.join(ids) or 'none yet (add_customer)'}")
    return row


def list_customers(conn: sqlite3.Connection) -> list[dict]:
    return [_customer(conn, r) for r in conn.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE")]


def add_customer(
    conn: sqlite3.Connection,
    name: str,
    cuit_: str,
    *,
    iva_condition: str = "responsable_inscripto",
    address: str = "",
    email: str | list[str] = "",
    email_text: str = "",
    description: str = "Servicios",
    monthly_net: float | None = None,
    effective_from: str | None = None,
    id: str | None = None,
) -> dict:
    """A monthly amount without effective_from applies from the start."""
    name = _required(name, "name")
    digits = cuit(cuit_)
    customer_id = slugify(id or name)
    if not customer_id:
        raise InvalidError("id must contain letters or digits")
    if effective_from is not None and monthly_net is None:
        raise InvalidError("effective_from goes with monthly_net")
    price = _price(monthly_net, effective_from, "") if monthly_net is not None else None
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM customers WHERE id = ? OR cuit = ?", (customer_id, digits)).fetchone():
            raise InvalidError(f"there's already a customer {customer_id!r} or with CUIT {digits}: update_customer")
        conn.execute(
            "INSERT INTO customers (id, name, cuit, iva_condition, address, email, email_text, description,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                customer_id,
                name,
                digits,
                _iva_condition(iva_condition),
                address.strip(),
                ", ".join(emails(email)),
                _email_text(email_text),
                _required(description, "description"),
            ),
        )
        if price:
            conn.execute(
                "INSERT INTO prices (customer_id, effective_from, monthly_net) VALUES (?, ?, ?)", (customer_id, *price)
            )
    return _customer(conn, find_customer(conn, customer_id))


def update_customer(
    conn: sqlite3.Connection,
    ref: str,
    *,
    name: str | None = None,
    cuit_: str | None = None,
    iva_condition: str | None = None,
    address: str | None = None,
    email: str | list[str] | None = None,
    email_text: str | None = None,
    description: str | None = None,
    monthly_net: float | None = None,
    effective_from: str | None = None,
) -> dict:
    """A new monthly amount applies from effective_from, the 1st of this month when absent, and replaces one
    from that same date. Issued invoices keep the customer as it was."""
    row = find_customer(conn, ref)
    changes: dict[str, Any] = {}
    if name is not None:
        changes["name"] = _required(name, "name")
    if cuit_ is not None:
        changes["cuit"] = cuit(cuit_)
    if iva_condition is not None:
        changes["iva_condition"] = _iva_condition(iva_condition)
    if address is not None:
        changes["address"] = address.strip()
    if email is not None:
        changes["email"] = ", ".join(emails(email))
    if email_text is not None:
        changes["email_text"] = _email_text(email_text)
    if description is not None:
        changes["description"] = _required(description, "description")
    if effective_from is not None and monthly_net is None:
        raise InvalidError("effective_from goes with monthly_net")
    price = _price(monthly_net, effective_from, today().replace(day=1).isoformat()) if monthly_net is not None else None
    with _transaction(conn):
        if changes:
            assignments = ", ".join(f"{k} = ?" for k in changes)
            try:
                conn.execute(f"UPDATE customers SET {assignments} WHERE id = ?", (*changes.values(), row["id"]))
            except sqlite3.IntegrityError:
                raise InvalidError(f"another customer has CUIT {changes.get('cuit')}") from None
        if price:
            conn.execute(
                "INSERT INTO prices (customer_id, effective_from, monthly_net) VALUES (?, ?, ?)"
                " ON CONFLICT (customer_id, effective_from) DO UPDATE SET monthly_net = excluded.monthly_net",
                (row["id"], *price),
            )
    return _customer(conn, find_customer(conn, row["id"]))


def _price(monthly_net: float, effective_from: str | None, default: str) -> tuple[str, str]:
    """(effective_from, monthly_net) as stored. A month (2027-01) starts on its 1st."""
    if effective_from is None:
        start = default
    elif re.fullmatch(r"\d{4}-\d{2}", effective_from.strip()):
        year, month = _month(effective_from)
        start = date(year, month, 1).isoformat()
    else:
        start = _date(effective_from, "effective_from").isoformat()
    return start, str(money(monthly_net, "monthly_net"))


def remove_price(conn: sqlite3.Connection, ref: str, effective_from: str | None) -> dict:
    """Remove the monthly amount from `effective_from` (None: the one from the start). Issued invoices keep
    their amounts."""
    row = find_customer(conn, ref)
    start = _price(1, effective_from, "")[0] if effective_from else ""
    removed = conn.execute(
        "DELETE FROM prices WHERE customer_id = ? AND effective_from = ?", (row["id"], start)
    ).rowcount
    if not removed:
        raise NotFoundError(f"{row['name']} has no monthly amount from {start or 'the start'}")
    return _customer(conn, row)


def price_on(conn: sqlite3.Connection, customer_id: str, day: date) -> Decimal | None:
    """The monthly net amount in effect on `day`."""
    row = conn.execute(
        "SELECT monthly_net FROM prices WHERE customer_id = ? AND effective_from <= ?"
        " ORDER BY effective_from DESC LIMIT 1",
        (customer_id, day.isoformat()),
    ).fetchone()
    return Decimal(row["monthly_net"]) if row else None


# --- Periods ---


def halves(month: str) -> list[tuple[date, date]]:
    """The 1st to the 15th, and the 16th to the month's last day."""
    year, m = _month(month)
    last = calendar.monthrange(year, m)[1]
    return [(date(year, m, 1), date(year, m, 15)), (date(year, m, 16), date(year, m, last))]


def default_net(conn: sqlite3.Connection, customer: sqlite3.Row, start: date, end: date) -> Decimal:
    """The monthly amount in effect on `start` for a whole month, or its half for a half: the first half
    rounded to the cent, the second what's left."""
    monthly = price_on(conn, customer["id"], start)
    if monthly is None:
        raise InvalidError(f"{customer['name']} has no monthly amount on {start}: give net_amount, or set monthly_net")
    last = calendar.monthrange(start.year, start.month)[1]
    same_month = (start.year, start.month) == (end.year, end.month)
    first_half = (monthly / 2).quantize(CENT, ROUND_HALF_UP)
    if same_month and start.day == 1 and end.day == last:
        return monthly
    if same_month and start.day == 1 and end.day == 15:
        return first_half
    if same_month and start.day == 16 and end.day == last:
        return monthly - first_half
    raise InvalidError("the period isn't a month or one of its halves (1-15, 16-end): give net_amount")


def _description(customer: sqlite3.Row, start: date, end: date) -> str:
    return f"{customer['description']} ({start:%d/%m/%Y} al {end:%d/%m/%Y})"


# --- Invoices ---


def _invoice(row: sqlite3.Row) -> dict:
    customer = json.loads(row["customer"])
    number = row["number"]
    return {
        "id": row["id"],
        "environment": row["environment"],
        "status": row["status"],
        "type": "Factura A",
        "point_of_sale": row["point_of_sale"],
        "number": number,
        "full_number": f"{row['point_of_sale']:05d}-{number:08d}" if number is not None else None,
        "customer_id": row["customer_id"],
        "customer_name": customer.get("name"),
        "customer_email": customer.get("email"),
        "period_from": row["period_from"],
        "period_to": row["period_to"],
        "issue_date": row["issue_date"],
        "due_date": row["due_date"],
        "description": row["description"],
        "net": float(Decimal(row["net"])),
        "iva": float(Decimal(row["iva"])),
        "total": float(Decimal(row["total"])),
        "cae": row["cae"],
        "cae_due": row["cae_due"],
        "observations": json.loads(row["observations"]),
        "errors": json.loads(row["errors"]),
        "pdf_path": row["pdf_path"],
    }


def _get(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"no invoice {invoice_id}")
    return row


def _update(conn: sqlite3.Connection, invoice_id: int, **values: Any) -> sqlite3.Row:
    assignments = ", ".join(f"{k} = ?" for k in values)
    conn.execute(
        f"UPDATE invoices SET {assignments}, updated_at = datetime('now') WHERE id = ?", (*values.values(), invoice_id)
    )
    return _get(conn, invoice_id)


def _messages(messages: list[arca.Message]) -> str:
    return json.dumps([{"code": m.code, "message": m.message} for m in messages], ensure_ascii=False)


def list_invoices(
    conn: sqlite3.Connection,
    customer: str | None = None,
    month: str | None = None,
    status: str | None = None,
    environment: str | None = None,
) -> list[dict]:
    """Ordered by period. In the current environment unless another is given."""
    sql, args = "SELECT * FROM invoices WHERE environment = ?", [environment or settings(conn)["environment"]]
    if customer:
        sql, args = sql + " AND customer_id = ?", [*args, find_customer(conn, customer)["id"]]
    if month:
        year, m = _month(month)
        sql, args = sql + " AND period_from LIKE ?", [*args, f"{year:04d}-{m:02d}-%"]
    if status:
        sql, args = sql + " AND status = ?", [*args, status]
    return [_invoice(r) for r in conn.execute(sql + " ORDER BY period_from, id", args)]


def get_invoice(conn: sqlite3.Connection, invoice_id: int, pdf_dir: Path) -> dict:
    """With its PDF, made again when it's missing, and the email that sends it."""
    row = _get(conn, invoice_id)
    out = _invoice(row)
    if row["status"] == "authorized":
        out |= _ensure_pdf(conn, row, pdf_dir)
        out["email"] = invoice_email(conn, row)
    return out


def invoice_email(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """The email that sends an issued invoice: to the customer's addresses as they are now, with its text (or the
    standard one) filled in. The PDF goes attached."""
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (row["customer_id"],)).fetchone()
    issuer = json.loads(row["issuer"])
    invoice = _invoice(row)
    start, end, due = (date.fromisoformat(row[k]) for k in ("period_from", "period_to", "due_date"))
    values = {
        "cliente": json.loads(row["customer"])["name"],
        "numero": invoice["full_number"],
        "periodo": f"{start:%d/%m/%Y} al {end:%d/%m/%Y}",
        "total": f"$ {pdf.pesos(Decimal(row['total']))}",
        "vencimiento": f"{due:%d/%m/%Y}",
        "emisor": issuer["business_name"],
    }
    text = (customer["email_text"] if customer is not None else "") or EMAIL_TEXT
    return {
        "to": emails(customer["email"]) if customer is not None else [],
        "subject": f"Factura A {invoice['full_number']} - {issuer['business_name']}",
        "body": re.sub(r"{(\w+)}", lambda m: values.get(m[1], m[0]), text),
    }


def prepare_month(
    conn: sqlite3.Connection, customer: str | None = None, month: str | None = None, on: date | None = None
) -> dict:
    """What issuing `month`'s two invoices would issue, for one customer or every one with a monthly amount.
    Issues nothing."""
    on = on or today()
    month = month or (on.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    values = settings(conn)
    env = values["environment"]
    if customer:
        customers = [find_customer(conn, customer)]
    else:
        customers = conn.execute(
            "SELECT * FROM customers WHERE id IN (SELECT customer_id FROM prices) ORDER BY name COLLATE NOCASE"
        ).fetchall()
    due = on + timedelta(days=int(values["payment_days"]))
    out, to_issue = [], Decimal(0)
    for c in customers:
        for start, end in halves(month):
            entry: dict[str, Any] = {
                "customer": {"id": c["id"], "name": c["name"], "cuit": c["cuit"], "email": c["email"]},
                "period_from": start.isoformat(),
                "period_to": end.isoformat(),
            }
            existing = _find(conn, env, c["id"], start, end)
            if existing is not None and existing["status"] == "authorized":
                out.append(entry | {"status": "authorized", "invoice": _invoice(existing)})
                continue
            try:
                net, iva, total = amounts(default_net(conn, c, start, end))
            except InvalidError as e:
                out.append(entry | {"status": "can't prepare", "error": str(e)})
                continue
            if c["iva_condition"] not in FACTURA_A_RECEIVERS:
                out.append(entry | {"status": "can't prepare", "error": _not_a_receiver(c)})
                continue
            to_issue += total
            entry |= {
                "status": existing["status"] if existing is not None else "not issued",
                "issue_date": on.isoformat(),
                "due_date": due.isoformat(),
                "description": _description(c, start, end),
                "net": float(net),
                "iva": float(iva),
                "total": float(total),
                "create_invoice": {"customer": c["id"], "period_from": start.isoformat(), "period_to": end.isoformat()},
            }
            if existing is not None and existing["status"] == "sent":
                entry["note"] = "sent before without an answer: create_invoice checks with ARCA first"
            elif existing is not None and existing["status"] == "rejected":
                entry["last_errors"] = json.loads(existing["errors"]) + json.loads(existing["observations"])
            out.append(entry)
    return {
        "month": month,
        "environment": env,
        "invoice_type": "Factura A",
        "point_of_sale": int(values["point_of_sale"]) if values.get("point_of_sale") else None,
        "missing_setup": missing(values),
        "total_to_issue": float(to_issue),
        "invoices": out,
    }


def _not_a_receiver(customer: sqlite3.Row) -> str:
    return (
        f"{customer['name']} is {customer['iva_condition']}, and a Factura A goes only to"
        f" {', '.join(FACTURA_A_RECEIVERS)}"
    )


def _find(conn: sqlite3.Connection, env: str, customer_id: str, start: date, end: date) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE environment = ? AND customer_id = ? AND period_from = ? AND period_to = ?",
        (env, customer_id, start.isoformat(), end.isoformat()),
    ).fetchone()


def create_invoice(
    conn: sqlite3.Connection,
    customer: str,
    period_from: str,
    period_to: str,
    *,
    net_amount: float | None = None,
    issue_date: str | None = None,
    due_date: str | None = None,
    description: str | None = None,
    pdf_dir: Path,
    send: arca.Post = arca.post,
    now: datetime | None = None,
) -> dict:
    """Issue a Factura A for services in the period, or return the one already issued for it."""
    now = now or datetime.now(arca.AR)
    values = _ready(conn)
    env = values["environment"]
    c = find_customer(conn, customer)
    start, end = _date(period_from, "period_from"), _date(period_to, "period_to")
    if end < start:
        raise InvalidError("period_to is before period_from")
    given = money(net_amount, "net_amount") if net_amount is not None else None

    clients: dict[str, arca.Wsfe] = {}

    def wsfe() -> arca.Wsfe:
        if not clients:
            clients["wsfe"] = _wsfe(conn, values, send, now)
        return clients["wsfe"]

    # Invoices sent without an answer first: each is issued or not, and its number is ARCA's or free again.
    for pending in conn.execute("SELECT * FROM invoices WHERE environment = ? AND status = 'sent'", (env,)).fetchall():
        _reconcile(conn, pending, wsfe())

    existing = _find(conn, env, c["id"], start, end)
    overlapping = conn.execute(
        "SELECT * FROM invoices WHERE environment = ? AND customer_id = ? AND status = 'authorized'"
        " AND period_from <= ? AND period_to >= ? AND id != ?",
        (env, c["id"], end.isoformat(), start.isoformat(), existing["id"] if existing is not None else 0),
    ).fetchone()
    if overlapping is not None:
        raise AlreadyIssuedError(
            f"invoice {overlapping['id']} ({_invoice(overlapping)['full_number']}) covers"
            f" {overlapping['period_from']} to {overlapping['period_to']}, which overlaps this period: nothing was"
            " issued"
        )
    if existing is not None and existing["status"] == "authorized":
        if given is not None and Decimal(existing["net"]) != given:
            raise AlreadyIssuedError(
                f"invoice {existing['id']} was already issued for this period with net {existing['net']}:"
                " nothing was issued. Changing it takes a credit note, which this plugin doesn't issue."
            )
        return _invoice(existing) | _ensure_pdf(conn, existing, pdf_dir) | {"already_issued": True}

    if c["iva_condition"] not in FACTURA_A_RECEIVERS:
        raise InvalidError(_not_a_receiver(c))
    issued_on = _date(issue_date, "issue_date") if issue_date else now.astimezone(arca.AR).date()
    due = _date(due_date, "due_date") if due_date else issued_on + timedelta(days=int(values["payment_days"]))
    if due < issued_on:
        raise InvalidError("due_date is before issue_date")
    net, iva, total = amounts(given if given is not None else default_net(conn, c, start, end))
    point_of_sale = int(values["point_of_sale"])
    fields = {
        "cbte_tipo": arca.FACTURA_A,
        "point_of_sale": point_of_sale,
        "number": None,
        "issue_date": issued_on.isoformat(),
        "due_date": due.isoformat(),
        "description": description.strip() if description and description.strip() else _description(c, start, end),
        "net": str(net),
        "iva": str(iva),
        "total": str(total),
        "status": "draft",
        "observations": "[]",
        "errors": "[]",
        "issuer": json.dumps(_issuer_snapshot(values), ensure_ascii=False),
        "customer": json.dumps(_customer_snapshot(c), ensure_ascii=False),
    }
    if existing is None:
        cur = conn.execute(
            f"INSERT INTO invoices (environment, customer_id, period_from, period_to, {', '.join(fields)},"
            " created_at, updated_at)"
            f" VALUES (?, ?, ?, ?, {', '.join('?' * len(fields))}, datetime('now'), datetime('now'))",
            (env, c["id"], start.isoformat(), end.isoformat(), *fields.values()),
        )
        row = _get(conn, cur.lastrowid)
    else:
        row = _update(conn, existing["id"], **fields)

    client = wsfe()
    try:
        number = client.last_number(point_of_sale, arca.FACTURA_A) + 1
    except arca.RefusedError as e:
        if _not_enabled(e):
            raise NotReadyError(f"{_points_of_sale_note(client, point_of_sale, env)} Nothing was issued.") from None
        raise BillingError(f"couldn't get the next number from ARCA, nothing was issued: {e}") from None
    except arca.ArcaError as e:
        raise BillingError(f"couldn't get the next number from ARCA, nothing was issued: {e}") from None
    # Saved before sending: without an answer, the next try asks ARCA about this number.
    try:
        row = _update(conn, row["id"], number=number, status="sent")
    except sqlite3.IntegrityError:
        raise BillingError(
            f"ARCA's next number, {number}, is already an invoice here: is the point of sale or the CUIT right?"
        ) from None
    voucher = arca.Voucher(
        point_of_sale=point_of_sale,
        cbte_tipo=arca.FACTURA_A,
        number=number,
        concept=arca.CONCEPT_SERVICES,
        doc_tipo=arca.DOC_CUIT,
        doc_nro=c["cuit"],
        issue_date=issued_on,
        net=net,
        iva=iva,
        total=total,
        service_from=start,
        service_to=end,
        due_date=due,
        receiver_iva_condition=IVA_CONDITIONS[c["iva_condition"]][0],
    )
    try:
        result = client.authorize(voucher)
    except arca.NotSentError as e:
        _update(conn, row["id"], number=None, status="draft")
        raise BillingError(f"{e}. Nothing was issued; try again.") from None
    except arca.NoAnswerError as e:
        raise UnconfirmedError(
            f"invoice {row['id']} was sent to ARCA as number {number} and no answer came back ({e}), so it may"
            " be issued. Call create_invoice again with the same customer and period: it asks ARCA first and"
            " never issues it twice."
        ) from None
    except arca.RefusedError as e:
        result = arca.Authorization(approved=False, errors=e.messages)

    if not result.approved:
        _update(
            conn,
            row["id"],
            number=None,
            status="rejected",
            errors=_messages(result.errors),
            observations=_messages(result.observations),
        )
        reasons = "; ".join(str(m) for m in [*result.errors, *result.observations]) or "no reason given"
        raise RejectedError(f"ARCA rejected invoice {row['id']}, nothing was issued: {reasons}")
    row = _update(
        conn,
        row["id"],
        status="authorized",
        cae=result.cae,
        cae_due=result.cae_due.isoformat() if result.cae_due else None,
        processed_at=result.processed_at.isoformat() if result.processed_at else None,
        observations=_messages(result.observations),
    )
    return _invoice(row) | _ensure_pdf(conn, row, pdf_dir) | {"already_issued": False}


def _reconcile(conn: sqlite3.Connection, row: sqlite3.Row, client: arca.Wsfe) -> sqlite3.Row:
    """A `sent` invoice as ARCA has it: authorized if ARCA has its number for this customer and amount,
    otherwise back to draft (that number wasn't issued for it)."""
    try:
        found = client.lookup(row["point_of_sale"], row["cbte_tipo"], row["number"])
    except arca.ArcaError as e:
        raise UnconfirmedError(
            f"invoice {row['id']} was sent as number {row['number']} without an answer, and ARCA can't say now"
            f" whether it issued it ({e}). Nothing was sent again; try later."
        ) from None
    customer = json.loads(row["customer"])
    if (
        found is not None
        and found.approved
        and found.doc_nro == customer["cuit"]
        and found.total == Decimal(row["total"])
        and found.issue_date == date.fromisoformat(row["issue_date"])
        and found.service_from == date.fromisoformat(row["period_from"])
        and found.service_to == date.fromisoformat(row["period_to"])
    ):
        return _update(
            conn,
            row["id"],
            status="authorized",
            cae=found.cae,
            cae_due=found.cae_due.isoformat() if found.cae_due else None,
            processed_at=found.processed_at.isoformat() if found.processed_at else None,
        )
    return _update(conn, row["id"], number=None, status="draft")


def _issuer_snapshot(values: dict[str, str]) -> dict:
    return {k: values[k] for k in (*REQUIRED, "sale_condition")} | {
        "iva_condition_label": IVA_CONDITIONS[values["iva_condition"]][1]
    }


def _customer_snapshot(c: sqlite3.Row) -> dict:
    return {
        "name": c["name"],
        "cuit": c["cuit"],
        "iva_condition": c["iva_condition"],
        "iva_condition_label": IVA_CONDITIONS[c["iva_condition"]][1],
        "address": c["address"],
        "email": c["email"],
    }


def _ensure_pdf(conn: sqlite3.Connection, row: sqlite3.Row, pdf_dir: Path) -> dict:
    """{"pdf_path"}, made when it's missing; or {"pdf_error"}: the invoice stays issued either way."""
    if row["pdf_path"] and Path(row["pdf_path"]).is_file():
        return {"pdf_path": row["pdf_path"]}
    prefix = "HOMOLOGACION-" if row["environment"] == "homologacion" else ""
    path = pdf_dir / f"{prefix}FA-{row['point_of_sale']:05d}-{row['number']:08d}.pdf"
    try:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf.render(_invoice(row) | {"issuer": json.loads(row["issuer"]), "customer": json.loads(row["customer"])}, path)
    except Exception as e:
        return {"pdf_path": None, "pdf_error": f"couldn't make the PDF: {e!r}; get_invoice tries again"}
    _update(conn, row["id"], pdf_path=str(path))
    return {"pdf_path": str(path)}


# --- ARCA's tickets ---


def _wsfe(conn: sqlite3.Connection, values: dict[str, str], send: arca.Post, now: datetime) -> arca.Wsfe:
    env = values["environment"]
    ticket = ticket_for(conn, env, values[f"certificate_path.{env}"], values[f"key_path.{env}"], send, now)
    return arca.Wsfe(env, ticket.token, ticket.sign, values["cuit"], send)


def ticket_for(
    conn: sqlite3.Connection,
    env: str,
    certificate_path: str,
    key_path: str,
    send: arca.Post,
    now: datetime,
    service: str = arca.INVOICES,
) -> arca.Ticket:
    """WSAA's ticket for `service`, kept until a few minutes before it expires: WSAA gives a certificate no
    other for a service while one is valid."""
    try:
        certificate = Path(certificate_path).read_bytes()
    except OSError as e:
        raise NotReadyError(f"can't read the certificate at {certificate_path}: {e.strerror}") from None
    fingerprint = arca.load_certificate(certificate).fingerprint(hashes.SHA256()).hex()
    row = conn.execute(
        "SELECT * FROM tickets WHERE environment = ? AND service = ? AND certificate = ?", (env, service, fingerprint)
    ).fetchone()
    if row is not None and datetime.fromisoformat(row["expires_at"]) > now + TICKET_MARGIN:
        return arca.Ticket(row["token"], row["sign"], datetime.fromisoformat(row["expires_at"]))
    try:
        key = Path(key_path).read_bytes()
    except OSError as e:
        raise NotReadyError(f"can't read the private key at {key_path}: {e.strerror}") from None
    try:
        ticket = arca.login(env, arca.sign(arca.login_request(service, now), certificate, key), send)
    except arca.ArcaError as e:
        raise BillingError(f"ARCA's sign-in (WSAA, {env}, for {service}) failed: {e}") from None
    conn.execute(
        "INSERT INTO tickets (environment, service, certificate, token, sign, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (environment, service, certificate) DO UPDATE SET token = excluded.token,"
        " sign = excluded.sign, expires_at = excluded.expires_at",
        (env, service, fingerprint, ticket.token, ticket.sign, ticket.expires_at.isoformat()),
    )
    return ticket


def check(conn: sqlite3.Connection, send: arca.Post = arca.post, now: datetime | None = None) -> dict:
    """Signs in to ARCA and asks for the last Factura A at the point of sale: the setup works if it answers."""
    now = now or datetime.now(arca.AR)
    values = _ready(conn)
    client = _wsfe(conn, values, send, now)
    point_of_sale = int(values["point_of_sale"])
    try:
        last = client.last_number(point_of_sale, arca.FACTURA_A)
        enabled = [p for p in client.points_of_sale() if p.usable]
    except arca.RefusedError as e:
        if _not_enabled(e):
            raise NotReadyError(_points_of_sale_note(client, point_of_sale, values["environment"])) from None
        raise BillingError(f"ARCA's invoicing service (WSFEv1) refused: {e}") from None
    except arca.ArcaError as e:
        raise BillingError(f"ARCA's invoicing service (WSFEv1) refused: {e}") from None
    return {
        "environment": values["environment"],
        "point_of_sale": point_of_sale,
        "last_factura_a": last,
        "next_factura_a": last + 1,
        "points_of_sale": [{"number": p.number, "kind": p.kind} for p in enabled],
    }


def _not_enabled(error: arca.RefusedError) -> bool:
    return any(m.code == arca.POINT_OF_SALE_NOT_ENABLED for m in error.messages)


def _points_of_sale_note(client: arca.Wsfe, point_of_sale: int, env: str) -> str:
    """Why a point of sale ARCA hasn't enabled for web services can't issue, and which ones can."""
    said = f"Point of sale {point_of_sale} isn't enabled for web services in ARCA ({env})"
    try:
        usable = [p for p in client.points_of_sale() if p.usable]
    except arca.ArcaError:
        return f"{said}: in Settings, pick one of type RECE para aplicativo y web services."
    if not usable:
        return (
            f"{said}, and none is: create one of type RECE para aplicativo y web services in ARCA (Administración de"
            " puntos de venta y domicilios), then pick it in Settings."
        )
    listed = ", ".join(f"{p.number} ({p.kind})" for p in usable)
    return (
        f"{said}. The ones that are: {listed}. Pick one in Settings; a Factura A is issued at one of a responsable"
        " inscripto (Ri Iva)."
    )


def lookup_cuit(conn: sqlite3.Connection, value: str, send: arca.Post = arca.post, now: datetime | None = None) -> dict:
    """Who a CUIT is, from ARCA's padrón A5: name, fiscal address and IVA condition, for filling a customer in.
    Needs the issuer's CUIT and the environment's certificate, authorized in ARCA for ws_sr_padron_a5."""
    wanted = cuit(value)
    now = now or datetime.now(arca.AR)
    values = settings(conn)
    env = values["environment"]
    lacking = [k for k in missing(values) if k in ("cuit", "certificate_path", "key_path")]
    if lacking:
        raise NotReadyError(f"looking a CUIT up in ARCA needs {', '.join(lacking)} in setup")
    ticket = ticket_for(conn, env, values[f"certificate_path.{env}"], values[f"key_path.{env}"], send, now, arca.PADRON)
    try:
        person = arca.padron_person(env, ticket.token, ticket.sign, values["cuit"], wanted, send)
    except arca.ArcaError as e:
        raise BillingError(f"ARCA's padrón ({arca.PADRON}, {env}) refused: {e}") from None
    if person is None:
        return {"cuit": wanted, "found": False}
    return {
        "cuit": wanted,
        "found": True,
        "name": person.name,
        "address": person.address,
        "iva_condition": person.iva_condition,
        "iva_condition_label": IVA_CONDITIONS[person.iva_condition][1] if person.iva_condition else None,
        "active": person.active,
        "notes": person.notes,
    }
