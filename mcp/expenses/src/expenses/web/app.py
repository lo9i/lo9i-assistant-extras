"""Web UI over the service layer. lo9i starts it (see server.yaml) and shows
it in its web app:

    EXPENSES_DB=/path/expenses.db expenses-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has
no login: the daemon lets only the user's paired devices through. The daemon
serves it under a path (/expenses/) and says which in X-Forwarded-Prefix;
pages link relative to it (<base> in base.html).

Pages are server-rendered; htmx swaps a page's main section after each
action. A form that fails is re-rendered with its error and the values typed,
in place of the form (HX-Retarget), so nothing is lost.
"""

import argparse
import contextlib
import hashlib
import os
import re
import socket
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, recurring, service
from ..models import Bill, Obligation
from ..service import ExpensesError, ValidationError

HERE = Path(__file__).parent
# What the daemon may send as X-Forwarded-Prefix: path segments, like /expenses.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_ars(amount: float) -> str:
    """Argentine style: dot for thousands, comma for decimals, e.g. 45.230,50."""
    return f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it
    again after a deploy changes it instead of using its cached copy."""
    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["asset"] = asset_url
templates.env.filters["ars"] = format_ars
templates.env.globals["MONTHS"] = MONTHS
templates.env.globals["ASSET_KINDS"] = service.ASSET_KINDS
templates.env.globals["EVERY_MONTHS"] = service.EVERY_MONTHS


async def get_conn() -> AsyncIterator[sqlite3.Connection]:
    """A connection per request, with this month's recurring bills in place."""
    conn = db.connect()
    try:
        recurring.generate(conn)
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


# --- Form parsing ---


def _text(form: dict, key: str) -> str:
    return str(form.get(key) or "").strip()


def _opt(form: dict, key: str) -> str | None:
    return _text(form, key) or None


def _amount(form: dict, key: str) -> float | None:
    """Accepts 45230.50, 45230,50 and 45.230,50."""
    v = _text(form, key)
    if not v:
        return None
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        raise ValidationError(f'{key} must be a number, got "{_text(form, key)}"') from None


def _int(form: dict, key: str) -> int | None:
    v = _text(form, key)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        raise ValidationError(f'{key} must be a whole number, got "{v}"') from None


def _metadata(form: dict) -> dict[str, str]:
    """`key: value` lines."""
    out = {}
    for line in _text(form, "metadata").splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValidationError(f'metadata lines must look like "key: value", got "{line}"')
        out[key] = value
    return out


def metadata_text(metadata: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in metadata.items())


templates.env.filters["metadata_text"] = metadata_text


def input_number(value: Any) -> str:
    """An amount as an input value: 1160000.0 -> "1160000". Typed text (a
    failed form) passes through."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return "" if value is None or not isinstance(value, str) else value


templates.env.filters["num"] = input_number


def _render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def _form_error(request: Request, template: str, target: str, **ctx: Any) -> HTMLResponse:
    """Re-render a failed form, with its error, in place of the form."""
    resp = _render(request, template, **ctx)
    resp.headers["HX-Retarget"] = target
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


def _page(request: Request, page: str, partial: str, **ctx: Any) -> HTMLResponse:
    """The whole page, or only its main section for an htmx request."""
    return _render(request, partial if request.headers.get("HX-Request") else page, **ctx)


# --- Month ---


@dataclass
class Filters:
    scope: str = "month"
    asset: str = ""
    category: str = ""
    status: str = ""

    @classmethod
    def read(cls, values: Any) -> "Filters":
        """From the filter form. Its fields are prefixed with f_ because
        actions send them along with a bill's own status and category."""
        return cls(
            scope="all" if values.get("f_scope") == "all" else "month",
            asset=str(values.get("f_asset") or ""),
            category=str(values.get("f_category") or ""),
            # Unpaid by default. "Any status" sends an empty value, so it sticks.
            status=str(values.get("f_status", "pending")),
        )

    def keep(self, b: Bill) -> bool:
        return (
            (not self.asset or (b.asset_id or "none") == self.asset)
            and (not self.category or b.category == self.category)
            and (not self.status or b.status == self.status)
        )


def _labels(conn: sqlite3.Connection) -> tuple[dict[int, Obligation], dict[str, str]]:
    obligations = {o.id: o for o in service.list_obligations(conn)}
    assets = {a.id: a.name for a in service.list_assets(conn)}
    return obligations, assets


def _month_ctx(conn: sqlite3.Connection, filters: Filters, **extra: Any) -> dict[str, Any]:
    bills = service.month_bills(conn) if filters.scope == "month" else service.list_bills(conn)
    bills = [b for b in bills if filters.keep(b)]
    obligations, assets = _labels(conn)
    return {
        "filters": filters,
        "bills": bills,
        "totals": service.totals(bills),
        "obligations": obligations,
        "assets": assets,
        "categories": service.list_categories(conn),
        "add": {},
        **extra,
    }


def _bill_args(form: dict) -> dict[str, Any]:
    """add_bill/update_bill arguments from a bill form. An obligation bill
    takes its asset and category from the obligation."""
    obligation_id = _int(form, "obligation_id")
    args: dict[str, Any] = {
        "obligation_id": obligation_id,
        "provider": _text(form, "provider"),
        "notes": _text(form, "notes"),
    }
    if obligation_id is None:
        args["category"] = _opt(form, "category")
        args["asset_id"] = _opt(form, "asset_id")
    amount = _amount(form, "amount")
    if amount is None:
        raise ValidationError("amount is required")
    return {**args, "amount": amount, "due_date": _text(form, "due_date")}


def _month_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def month(request: Request, conn: Conn) -> HTMLResponse:
        ctx = _month_ctx(conn, Filters.read(request.query_params))
        return _page(request, "month.html", "_month.html", **ctx)

    @app.post("/bills", response_class=HTMLResponse)
    async def add_bill(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        filters = Filters.read(form)
        try:
            args = _bill_args(form)
            service.add_bill(conn, args.pop("amount"), args.pop("due_date"), **args)
        except ExpensesError as e:
            ctx = _month_ctx(conn, filters, add=form, add_error=str(e), add_open=True)
            return _form_error(request, "_add_bill.html", "#add-bill", **ctx)
        return _render(request, "_month.html", **_month_ctx(conn, filters))

    @app.get("/bills/{id}/edit", response_class=HTMLResponse)
    async def edit_bill(request: Request, id: int, conn: Conn) -> HTMLResponse:
        bill = service.get_bill(conn, id)
        ctx = _month_ctx(conn, Filters.read(request.query_params))
        return _render(request, "_edit_bill.html", bill=bill, form=None, **ctx)

    @app.post("/bills/{id}", response_class=HTMLResponse)
    async def save_bill(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        filters = Filters.read(form)
        try:
            args = _bill_args(form)
            if status := _text(form, "status"):
                args["status"] = status
            service.update_bill(conn, id, **args)
        except ExpensesError as e:
            bill = service.get_bill(conn, id)
            ctx = _month_ctx(conn, filters, bill=bill, form=form, error=str(e))
            return _form_error(request, "_edit_bill.html", f"#bill-{id}", **ctx)
        return _render(request, "_month.html", **_month_ctx(conn, filters))

    @app.post("/bills/{id}/status", response_class=HTMLResponse)
    async def set_status(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        service.update_bill(conn, id, status=_text(form, "status"))
        return _render(request, "_month.html", **_month_ctx(conn, Filters.read(form)))

    @app.post("/bills/{id}/delete", response_class=HTMLResponse)
    async def delete_bill(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        service.remove_bill(conn, id)
        return _render(request, "_month.html", **_month_ctx(conn, Filters.read(form)))


# --- Assets and obligations ---


NO_ASSET = "none"


def _assets_ctx(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    """Assets with their obligations, plus a group for obligations of no asset."""
    obligations = service.list_obligations(conn)
    groups = [
        {"key": a.id, "asset": a, "obligations": [o for o in obligations if o.asset_id == a.id]}
        for a in service.list_assets(conn)
    ]
    groups.append(
        {"key": NO_ASSET, "asset": None, "obligations": [o for o in obligations if o.asset_id is None]}
    )
    return {"groups": groups, "categories": service.list_categories(conn), **extra}


def _asset_args(form: dict) -> dict[str, Any]:
    return {
        "name": _text(form, "name"),
        "kind": _text(form, "kind") or "other",
        "notes": _text(form, "notes"),
        "metadata": _metadata(form),
    }


def _obligation_args(form: dict) -> dict[str, Any]:
    return {
        "name": _text(form, "name"),
        "category": _text(form, "category"),
        "notes": _text(form, "notes"),
        "metadata": _metadata(form),
        "due_day": _int(form, "due_day"),
        "every_months": _int(form, "every_months") or 1,
        "anchor_month": _int(form, "anchor_month"),
        "expected_amount": _amount(form, "expected_amount"),
    }


def _asset_routes(app: FastAPI) -> None:
    @app.get("/assets", response_class=HTMLResponse)
    async def assets(request: Request, conn: Conn) -> HTMLResponse:
        return _page(request, "assets.html", "_assets.html", **_assets_ctx(conn))

    @app.post("/assets", response_class=HTMLResponse)
    async def add_asset(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        try:
            args = _asset_args(form)
            service.add_asset(conn, args.pop("name"), args.pop("kind"), id=_opt(form, "id"), **args)
        except ExpensesError as e:
            ctx = _assets_ctx(conn, form=form, error=str(e), open=True)
            return _form_error(request, "_add_asset.html", "#add-asset", **ctx)
        return _render(request, "_assets.html", **_assets_ctx(conn))

    @app.get("/assets/{id}/edit", response_class=HTMLResponse)
    async def edit_asset(request: Request, id: str, conn: Conn) -> HTMLResponse:
        asset = service.get_asset(conn, id)
        return _render(request, "_edit_asset.html", asset=asset, form=None)

    @app.post("/assets/{id}", response_class=HTMLResponse)
    async def save_asset(request: Request, id: str, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        try:
            service.update_asset(conn, id, **_asset_args(form))
        except ExpensesError as e:
            asset = service.get_asset(conn, id)
            return _form_error(
                request, "_edit_asset.html", f"#asset-{id}", asset=asset, form=form, error=str(e)
            )
        return _render(request, "_assets.html", **_assets_ctx(conn))

    @app.post("/assets/{id}/delete", response_class=HTMLResponse)
    async def delete_asset(request: Request, id: str, conn: Conn) -> HTMLResponse:
        try:
            service.remove_asset(conn, id)
        except ExpensesError as e:
            return _render(request, "_assets.html", **_assets_ctx(conn, banner=str(e)))
        return _render(request, "_assets.html", **_assets_ctx(conn))

    @app.post("/obligations", response_class=HTMLResponse)
    async def add_obligation(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        group = _text(form, "asset_id") or NO_ASSET
        try:
            args = _obligation_args(form)
            asset_id = None if group == NO_ASSET else group
            service.add_obligation(conn, args.pop("name"), args.pop("category"), asset_id=asset_id, **args)
        except ExpensesError as e:
            ctx = _assets_ctx(conn, group=group, form=form, error=str(e), open=True)
            return _form_error(request, "_add_obligation.html", f"#add-ob-{group}", **ctx)
        return _render(request, "_assets.html", **_assets_ctx(conn))

    @app.get("/obligations/{id}/edit", response_class=HTMLResponse)
    async def edit_obligation(request: Request, id: int, conn: Conn) -> HTMLResponse:
        ob = service.get_obligation(conn, id)
        return _render(
            request, "_edit_obligation.html", ob=ob, form=None, categories=service.list_categories(conn)
        )

    @app.post("/obligations/{id}", response_class=HTMLResponse)
    async def save_obligation(request: Request, id: int, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        try:
            service.update_obligation(conn, id, **_obligation_args(form))
        except ExpensesError as e:
            ob = service.get_obligation(conn, id)
            ctx = {"ob": ob, "form": form, "error": str(e), "categories": service.list_categories(conn)}
            return _form_error(request, "_edit_obligation.html", f"#ob-{id}", **ctx)
        return _render(request, "_assets.html", **_assets_ctx(conn))

    @app.post("/obligations/{id}/delete", response_class=HTMLResponse)
    async def delete_obligation(request: Request, id: int, conn: Conn) -> HTMLResponse:
        try:
            service.remove_obligation(conn, id)
        except ExpensesError as e:
            return _render(request, "_assets.html", **_assets_ctx(conn, banner=str(e)))
        return _render(request, "_assets.html", **_assets_ctx(conn))


# --- Categories ---


def _category_routes(app: FastAPI) -> None:
    def ctx(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
        return {"categories": service.list_categories(conn), **extra}

    @app.get("/categories", response_class=HTMLResponse)
    async def categories(request: Request, conn: Conn) -> HTMLResponse:
        return _page(request, "categories.html", "_categories.html", **ctx(conn))

    @app.post("/categories", response_class=HTMLResponse)
    async def add_category(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        try:
            service.add_category(conn, _text(form, "name"))
        except ExpensesError as e:
            return _render(request, "_categories.html", **ctx(conn, banner=str(e), name=_text(form, "name")))
        return _render(request, "_categories.html", **ctx(conn))

    @app.post("/categories/delete", response_class=HTMLResponse)
    async def delete_category(request: Request, conn: Conn) -> HTMLResponse:
        form = dict(await request.form())
        try:
            service.remove_category(conn, _text(form, "name"))
        except ExpensesError as e:
            return _render(request, "_categories.html", **ctx(conn, banner=str(e)))
        return _render(request, "_categories.html", **ctx(conn))


# --- Serving under lo9i ---


class ForwardedPrefix:
    """Takes the path the daemon serves this under from X-Forwarded-Prefix
    (the daemon strips it from the request path), so pages can link to it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1")
            scope = {**scope, "root_path": prefix if PREFIX.match(prefix) else ""}
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # Errors outside a form (a bill deleted in another tab): the page shows
    # the message (see base.html).
    @app.exception_handler(ExpensesError)
    async def expenses_error(request: Request, exc: ExpensesError) -> Response:
        status = 404 if isinstance(exc, service.NotFound) else 400
        return Response(str(exc), status_code=status, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _month_routes(app)
    _asset_routes(app)
    _category_routes(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="expenses-web")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()
    db.connect().close()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(), fd=sock.fileno())


def listen(path: str) -> socket.socket:
    """A Unix socket only this user can connect to. The page has no login,
    so other users on the computer must not reach it (uvicorn's own `uds`
    makes the socket world-writable)."""
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
