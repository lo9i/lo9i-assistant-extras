"""Web UI over the service layer. lo9i starts it (see server.yaml) and shows it in its web app:

    BILLING_DB=/path/billing.db BILLING_INVOICES=/path/invoices billing-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has no login: the daemon lets only the
user's paired devices through. The daemon serves it under a path (/billing/) and says which in
X-Forwarded-Prefix; pages link relative to it (<base> in base.html).

Pages are server-rendered; htmx swaps a page's main section after each action. A form that fails is re-rendered
with its error and the values typed, in place of the form (HX-Retarget), so nothing is lost. Issuing asks the user
to confirm in the browser first, and shows ARCA's answer, or why nothing was issued, above the month.

Routes that wait on ARCA (issuing, checking the setup) run in a thread: an answer can take up to a minute.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import socket
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import arca, certificate_files, db, pdf, service
from ..service import BillingError

HERE = Path(__file__).parent
# What the daemon may send as X-Forwarded-Prefix: path segments, like /billing.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
# How ARCA is reached; tests put a fake here.
SEND: arca.Post = arca.post

ENVIRONMENT_NAMES = {"homologacion": "Homologación", "produccion": "Producción"}
# What service.missing names, as the page says it.
FIELD_LABELS = {
    "cuit": "your CUIT",
    "business_name": "razón social",
    "address": "domicilio comercial",
    "iva_condition": "IVA condition",
    "activity_start": "inicio de actividades",
    "iibb": "Ingresos Brutos",
    "point_of_sale": "point of sale",
    "certificate_path": "ARCA certificate",
    "key_path": "private key",
}


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it again after a deploy changes it
    instead of using its cached copy."""
    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


def money(amount: float | Decimal | None) -> str:
    """$ 4.537.500,00, as written in Argentina."""
    return "" if amount is None else f"$ {pdf.pesos(amount)}"


def day(iso: str | None) -> str:
    """2026-10-31 -> 31/10/2026."""
    return date.fromisoformat(iso).strftime("%d/%m/%Y") if iso else ""


def month_name(ym: str) -> str:
    """2026-11 -> November 2026."""
    try:
        y, m = ym[:7].split("-")
        return f"{MONTH_NAMES[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return ym


def since(effective_from: str | None) -> str:
    """When a monthly amount applies from: from the start, from a month, or from a day."""
    if not effective_from:
        return "From the start"
    if effective_from.endswith("-01"):
        return f"From {month_name(effective_from)}"
    return f"From {day(effective_from)}"


def plain(amount: float | None) -> str:
    """An amount as an input's value: 7500000 -> "7.500.000", 0.5 -> "0,50"."""
    if amount is None:
        return ""
    text = pdf.pesos(amount)
    return text.removesuffix(",00")


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals |= {
    "asset": asset_url,
    "ENVIRONMENTS": ENVIRONMENT_NAMES,
    "IVA_CONDITIONS": {k: label for k, (_, label) in service.IVA_CONDITIONS.items()},
    "EMAIL_TEXT": service.EMAIL_TEXT,
    "EMAIL_FIELDS": service.EMAIL_FIELDS,
}
templates.env.filters |= {
    "money": money,
    "day": day,
    "month_name": month_name,
    "since": since,
    "plain": plain,
    "field": lambda key: FIELD_LABELS.get(key, key),
}


@contextlib.contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# --- Form parsing ---


def _text(form: dict, key: str) -> str:
    return str(form.get(key) or "").strip()


def _opt(form: dict, key: str) -> str | None:
    return _text(form, key) or None


def _amount(form: dict, key: str) -> float | None:
    """Written the Argentine way, 7.500.000 or 7.500.000,50, or plainly, 7500000.50."""
    v = _text(form, key).removeprefix("$").replace(" ", "")
    if not v:
        return None
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", v):
        v = v.replace(".", "")
    try:
        return float(Decimal(v))
    except InvalidOperation:
        raise service.InvalidError(f'{key} must be an amount like 7.500.000, got "{_text(form, key)}"') from None


def _int(form: dict, key: str) -> int | None:
    v = _text(form, key)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        raise service.InvalidError(f'{key} must be a whole number, got "{v}"') from None


# --- Rendering ---


def _render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def _sentence(text: str) -> str:
    """With a period at the end, unless it has one already: "Saved Acme S.A.", not "Saved Acme S.A.."."""
    return text if text.endswith(".") else text + "."


def _told(response: HTMLResponse, message: str, ok: bool = True) -> HTMLResponse:
    """The response, and a message the page shows at the bottom of the screen whatever is scrolled into view
    (base.html), so a save says it saved even when the form's top is out of view."""
    response.headers["HX-Trigger"] = json.dumps({"billing:told": {"message": message, "ok": ok}})
    return response


def _form_error(request: Request, template: str, target: str, **ctx: Any) -> HTMLResponse:
    """Re-render a failed form, with its error, in place of the form."""
    resp = _render(request, template, **ctx)
    resp.headers["HX-Retarget"] = target
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


def _page(request: Request, page: str, partial: str, **ctx: Any) -> HTMLResponse:
    """The whole page, or only its main section for an htmx request."""
    return _render(request, partial if request.headers.get("HX-Request") else page, **ctx)


# --- Invoices ---


def last_month(today: date) -> str:
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _shift(month: str, months: int) -> str:
    y, m = (int(p) for p in month.split("-"))
    total = y * 12 + m - 1 + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _invoices_ctx(conn: sqlite3.Connection, month: str | None, **extra: Any) -> dict[str, Any]:
    """The month's two halves per customer with a monthly amount (service.prepare_month), the month's other
    invoices, and the totals."""
    month = month or last_month(service.today())
    preview = service.prepare_month(conn, None, month)
    cards: dict[str, dict[str, Any]] = {}
    for entry in preview["invoices"]:
        cards.setdefault(entry["customer"]["id"], {"customer": entry["customer"], "halves": []})["halves"].append(entry)
    shown = {(e["customer"]["id"], e["period_from"], e["period_to"]) for e in preview["invoices"]}
    invoices = service.list_invoices(conn, month=month)
    issued = [i for i in invoices if i["status"] == "authorized"]
    issuer = service.issuer(conn)
    steps = [
        {
            "title": "Your details",
            "text": "CUIT, razón social, domicilio, inicio de actividades, Ingresos Brutos and point of sale, as"
            " your invoices show them.",
            "done": not any(k in issuer["missing"] for k in service.REQUIRED),
            "href": "settings",
            "action": "Open Settings",
        },
        {
            "title": "ARCA certificate",
            "text": f"The certificate and private key ARCA gives you for web services, for"
            f" {ENVIRONMENT_NAMES[issuer['environment']]}. Only their paths are kept.",
            "done": not ({"certificate_path", "key_path"} & set(issuer["missing"])),
            "href": "settings#arca",
            "action": "Add it",
        },
        {
            "title": "A customer",
            "text": "Who you bill each month, with the monthly net amount before IVA.",
            "done": bool(cards),
            "href": "customers",
            "action": "Add one",
        },
    ]
    return {
        "nav": "invoices",
        "month": month,
        "previous": _shift(month, -1),
        "next": _shift(month, 1),
        "issuer": issuer,
        "steps": steps,
        "set_up": all(s["done"] for s in steps),
        "cards": list(cards.values()),
        "others": [i for i in invoices if (i["customer_id"], i["period_from"], i["period_to"]) not in shown],
        "issued_total": sum(i["total"] for i in issued),
        "issued_count": len(issued),
        "to_issue_total": preview["total_to_issue"],
        "to_issue_count": sum(1 for e in preview["invoices"] if "create_invoice" in e),
        "without_amount": [c["name"] for c in service.list_customers(conn) if not c["prices"]],
        **extra,
    }


def _invoice_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def invoices(request: Request, month: str | None = None) -> HTMLResponse:
        with _db() as conn:
            return _page(request, "invoices.html", "_invoices.html", **_invoices_ctx(conn, month or None))

    @app.post("/invoices", response_class=HTMLResponse)
    async def issue(request: Request) -> HTMLResponse:
        """Issue one of the month's invoices, as create_invoice does for the assistant."""
        form = dict(await request.form())
        return await run_in_threadpool(_issue, request, form)

    @app.get("/invoices/{id}", response_class=HTMLResponse)
    def invoice_page(request: Request, id: int) -> HTMLResponse:
        """The month of the invoice with it picked out, where lo9i's Home links one."""
        with _db() as conn:
            invoice = service.get_invoice(conn, id, db.invoices_dir())
            ctx = _invoices_ctx(conn, invoice["period_from"][:7], focus=id)
            if invoice["environment"] != ctx["issuer"]["environment"]:
                ctx["elsewhere"] = invoice
            return _render(request, "invoices.html", **ctx)

    @app.get("/invoices/{id}/pdf")
    def invoice_pdf(id: int) -> Response:
        with _db() as conn:
            invoice = service.get_invoice(conn, id, db.invoices_dir())
        if invoice["status"] != "authorized":
            raise service.NotFoundError(f"invoice {id} wasn't issued, so it has no PDF")
        if not invoice.get("pdf_path"):
            raise BillingError(invoice.get("pdf_error") or "its PDF couldn't be made")
        path = Path(invoice["pdf_path"])
        return FileResponse(path, media_type="application/pdf", content_disposition_type="inline", filename=path.name)


def _issue(request: Request, form: dict) -> HTMLResponse:
    month = _opt(form, "month")
    with db.issuing(), _db() as conn:
        try:
            invoice = service.create_invoice(
                conn,
                _text(form, "customer"),
                _text(form, "period_from"),
                _text(form, "period_to"),
                pdf_dir=db.invoices_dir(),
                send=SEND,
            )
        except BillingError as e:
            return _render(request, "_invoices.html", **_invoices_ctx(conn, month, banner=str(e)))
        if invoice["already_issued"]:
            notice = f"Already issued as Factura A {invoice['full_number']}: nothing was issued again."
        else:
            notice = f"Issued Factura A {invoice['full_number']} to {invoice['customer_name']}, CAE {invoice['cae']}."
        return _render(request, "_invoices.html", **_invoices_ctx(conn, month, notice=notice, focus=invoice["id"]))


# --- Customers ---


def _customers_ctx(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    """Customers, each price marked `current` when it's the one in effect today."""
    today = service.today()
    customers = service.list_customers(conn)
    for c in customers:
        started = [p for p in c["prices"] if (p["effective_from"] or "") <= today.isoformat()]
        for p in c["prices"]:
            p["current"] = bool(started) and p is started[-1]
    return {"nav": "customers", "customers": customers, "today": today, **extra}


def _customer_args(form: dict) -> dict[str, Any]:
    return {
        "name": _text(form, "name"),
        "iva_condition": _text(form, "iva_condition") or "responsable_inscripto",
        "address": _text(form, "address"),
        "email": _text(form, "email"),
        "email_text": _text(form, "email_text"),
        "description": _text(form, "description") or "Servicios",
    }


def _customer_routes(app: FastAPI) -> None:
    @app.get("/customers", response_class=HTMLResponse)
    def customers(request: Request) -> HTMLResponse:
        with _db() as conn:
            return _page(request, "customers.html", "_customers.html", **_customers_ctx(conn))

    @app.get("/customers/lookup")
    def lookup(cuit: str) -> JSONResponse:
        """Who a CUIT is in ARCA's padrón, for the customer form to fill the fields still empty (base.html), and
        what to say under the CUIT."""
        with _db() as conn:
            return JSONResponse(_lookup(conn, cuit))

    @app.get("/customers/{id}/edit", response_class=HTMLResponse)
    def edit_customer(request: Request, id: str) -> HTMLResponse:
        with _db() as conn:
            customer = service.list_customers(conn)
            found = next((c for c in customer if c["id"] == id), None)
            if found is None:
                raise service.NotFoundError(f"no customer {id!r}")
            return _render(request, "_edit_customer.html", customer=found, form=None)

    @app.post("/customers", response_class=HTMLResponse)
    async def add_customer(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        with _db() as conn:
            try:
                args = _customer_args(form)
                added = service.add_customer(
                    conn,
                    args.pop("name"),
                    _text(form, "cuit"),
                    monthly_net=_amount(form, "monthly_net"),
                    effective_from=_opt(form, "effective_from"),
                    **args,
                )
            except BillingError as e:
                ctx = _customers_ctx(conn, form=form, error=str(e), open=True)
                return _told(_form_error(request, "_add_customer.html", "#add-customer", **ctx), str(e), ok=False)
            return _told(
                _render(request, "_customers.html", **_customers_ctx(conn)), _sentence(f"Added {added['name']}")
            )

    @app.post("/customers/{id}", response_class=HTMLResponse)
    async def save_customer(request: Request, id: str) -> HTMLResponse:
        form = dict(await request.form())
        with _db() as conn:
            try:
                saved = service.update_customer(conn, id, cuit_=_text(form, "cuit"), **_customer_args(form))
            except BillingError as e:
                customer = next(c for c in service.list_customers(conn) if c["id"] == id)
                failed = _form_error(
                    request, "_edit_customer.html", f"#customer-{id}", customer=customer, form=form, error=str(e)
                )
                return _told(failed, str(e), ok=False)
            return _told(
                _render(request, "_customers.html", **_customers_ctx(conn)), _sentence(f"Saved {saved['name']}")
            )

    @app.post("/customers/{id}/prices", response_class=HTMLResponse)
    async def add_price(request: Request, id: str) -> HTMLResponse:
        form = dict(await request.form())
        with _db() as conn:
            try:
                amount = _amount(form, "monthly_net")
                if amount is None:
                    raise service.InvalidError("monthly_net is required")
                saved = service.update_customer(
                    conn, id, monthly_net=amount, effective_from=_opt(form, "effective_from")
                )
            except BillingError as e:
                return _told(
                    _render(request, "_customers.html", **_customers_ctx(conn, banner=str(e))), str(e), ok=False
                )
            return _told(
                _render(request, "_customers.html", **_customers_ctx(conn)),
                f"{saved['name']}'s new monthly amount is saved.",
            )

    @app.post("/customers/{id}/prices/delete", response_class=HTMLResponse)
    async def remove_price(request: Request, id: str) -> HTMLResponse:
        form = dict(await request.form())
        with _db() as conn:
            try:
                kept = service.remove_price(conn, id, _opt(form, "effective_from"))
            except BillingError as e:
                return _told(
                    _render(request, "_customers.html", **_customers_ctx(conn, banner=str(e))), str(e), ok=False
                )
            return _told(
                _render(request, "_customers.html", **_customers_ctx(conn)), _sentence(f"Removed from {kept['name']}")
            )


def _lookup(conn: sqlite3.Connection, cuit: str) -> dict[str, Any]:
    try:
        found = service.lookup_cuit(conn, cuit, SEND)
    except service.InvalidError as e:
        return {"found": False, "message": str(e)[0].upper() + str(e)[1:] + "."}
    except service.NotReadyError:
        return {"found": False, "message": "Looking CUITs up in ARCA needs your CUIT and certificate in Settings."}
    except BillingError as e:
        return {"found": False, "message": f"Couldn't ask ARCA: {e}"}
    if not found["found"]:
        return {"found": False, "message": "ARCA's padrón has no one with this CUIT."}
    said = f"From ARCA: {found['name']}"
    if found["iva_condition_label"]:
        said += f", {found['iva_condition_label']}."
    else:
        notes = "; ".join(found["notes"])
        said += f". It doesn't say an IVA condition{f' ({notes})' if notes else ''}: pick it."
    if not found["active"]:
        said += " Its CUIT isn't active."
    return found | {"message": said}


# --- Settings ---


def _settings_ctx(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    return {"nav": "settings", "issuer": service.issuer(conn), **extra}


def _settings_routes(app: FastAPI) -> None:
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        with _db() as conn:
            return _page(request, "settings.html", "_settings.html", **_settings_ctx(conn))

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> HTMLResponse:
        """The certificates first: one that's wrong saves nothing. A file picked with Browse wins over the path
        typed beside it, which the page fills with the file's name."""
        form, picked = await _settings_form(request)
        with _db() as conn:
            try:
                _save_certificates(conn, form, picked)
                service.setup(
                    conn,
                    cuit_=_opt(form, "cuit"),
                    business_name=_opt(form, "business_name"),
                    address=_opt(form, "address"),
                    iva_condition="responsable_inscripto",
                    activity_start=_opt(form, "activity_start"),
                    iibb=_opt(form, "iibb"),
                    point_of_sale=_int(form, "point_of_sale"),
                    environment=_opt(form, "environment"),
                    payment_days=_int(form, "payment_days"),
                    sale_condition=_opt(form, "sale_condition"),
                )
            except BillingError as e:
                return _render(request, "_settings.html", **_settings_ctx(conn, form=form, banner=str(e)))
            finally:
                certificate_files.forget_unused(service.settings(conn))
            return _render(request, "_settings.html", **_settings_ctx(conn, saved=True))

    @app.get("/settings/request/{environment}", response_class=HTMLResponse)
    def certificate_request(request: Request, environment: str) -> HTMLResponse:
        """The request ARCA issues the environment's certificate for, to copy or download (_request.html)."""
        with _db() as conn:
            try:
                return _render(request, "_request.html", **service.certificate_request(conn, environment))
            except BillingError as e:
                return _render(request, "_request.html", error=str(e))

    @app.get("/settings/request/{environment}/file")
    def certificate_request_file(environment: str) -> Response:
        with _db() as conn:
            made = service.certificate_request(conn, environment)
        disposition = f'attachment; filename="{made["file_name"]}"'
        return Response(made["csr"], media_type="application/pkcs10", headers={"Content-Disposition": disposition})

    @app.post("/settings/check", response_class=HTMLResponse)
    def check(request: Request) -> HTMLResponse:
        """Sign in to ARCA and read the point of sale's last Factura A, as check_arca does."""
        with _db() as conn:
            try:
                return _render(request, "_check.html", result=service.check(conn, SEND))
            except BillingError as e:
                return _render(request, "_check.html", error=str(e))


# The file inputs of each environment's certificate and key, and the path field each fills.
_PICKS = {
    f"{kind}_file_{env}": (env, kind, f"{kind}_{env}") for env in arca.ENVIRONMENTS for kind in ("certificate", "key")
}


async def _settings_form(request: Request) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    """The fields typed, and each file picked by its input's name with its name and content. A picked file's path
    field, which holds only its name, is left out, so a form shown again shows the path saved."""
    form = dict(await request.form())
    picked = {}
    for name, (_, _, field) in _PICKS.items():
        upload = form.pop(name, None)
        if isinstance(upload, UploadFile) and upload.filename:
            picked[name] = (upload.filename, await upload.read())
            form.pop(field, None)
    return form, picked


def _save_certificates(conn: sqlite3.Connection, form: dict, picked: dict[str, tuple[str, bytes]]) -> None:
    current = service.settings(conn)
    paths = {field: _opt(form, field) for _, _, field in _PICKS.values()}
    for name, (file_name, content) in picked.items():
        env, kind, field = _PICKS[name]
        paths[field] = certificate_files.keep(env, kind, file_name, content)
    for env in arca.ENVIRONMENTS:
        cert, key = paths[f"certificate_{env}"], paths[f"key_{env}"]
        cert = cert if cert != current.get(f"certificate_path.{env}") else None
        key = key if key != current.get(f"key_path.{env}") else None
        if cert or key:
            service.set_certificate(conn, env, cert, key)


# --- Serving under lo9i ---


class ForwardedPrefix:
    """Takes the path the daemon serves this under from X-Forwarded-Prefix (the daemon strips it from the
    request path), so pages can link to it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1")
            if prefix and PREFIX.match(prefix):
                # ASGI's path includes root_path; static files (and url_for) count on it.
                scope = {
                    **scope,
                    "root_path": prefix,
                    "path": prefix + scope["path"],
                    "raw_path": prefix.encode() + scope.get("raw_path", scope["path"].encode()),
                }
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # Errors outside a form (an invoice that isn't there): the page shows the message (see base.html).
    @app.exception_handler(BillingError)
    async def billing_error(request: Request, exc: BillingError) -> Response:
        status = 404 if isinstance(exc, service.NotFoundError) else 400
        return Response(str(exc), status_code=status, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _invoice_routes(app)
    _customer_routes(app)
    _settings_routes(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="billing-web")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()
    db.connect().close()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(), fd=sock.fileno())


def listen(path: str) -> socket.socket:
    """A Unix socket only this user can connect to. The page has no login, so other users on the computer must
    not reach it (uvicorn's own `uds` makes the socket world-writable)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(128)
    return sock
