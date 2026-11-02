"""MCP server over the service layer, over stdio. lo9i starts it (see server.yaml) with BILLING_DB and
BILLING_INVOICES set:

    BILLING_DB=/path/billing.db BILLING_INVOICES=/path/invoices billing-mcp
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import arca, db, service

INSTRUCTIONS = """\
Issues Argentine electronic invoices (Factura A, services, 21% IVA, in pesos) authorized by ARCA (ex AFIP),
and makes their PDF.

- setup holds the issuer's details and, for each environment (homologacion for tests, produccion for real
  invoices), the paths of the ARCA certificate and its private key. Call it without arguments to see it and
  what's missing; check_arca tries the setup against ARCA.
- Customers have a monthly net amount with the date it applies from, so a raise is a new amount from a
  later date.
- prepare_month previews a month's two invoices per customer (1st-15th, 16th-end, each half the monthly
  amount) and issues nothing. create_invoice issues one, with the arguments prepare_month gives.
- create_invoice never issues twice for one customer and period: calling it again returns the invoice
  already issued. Issued invoices can't be deleted or changed.

Amounts are plain numbers in pesos.
"""

mcp = MCPServer("billing", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)
# Only reads when given none of its arguments: lo9i asks the user only for a call that changes something
# (lo9i's core/mcp/hub.py::read_only_with).
READ_ONLY_WITHOUT_ARGUMENTS = {"lo9i/read_only_with": []}

# How ARCA is reached; tests put a fake here.
SEND: arca.Post = arca.post


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """A connection per call. Service errors reach the model as tool errors it can act on."""
    conn = db.connect()
    try:
        yield conn
    except service.BillingError as e:
        raise ToolError(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()


def _linked(invoice: dict) -> dict:
    """With where it is on the plugin's web page, which lo9i's Home links an item to."""
    return invoice | {"page_path": f"invoices/{invoice['id']}"}


@mcp.tool(meta=READ_ONLY_WITHOUT_ARGUMENTS)
def setup(
    cuit: Annotated[str | None, Field(description="The issuer's CUIT, like 20-12345678-6")] = None,
    business_name: Annotated[str | None, Field(description="Razón social")] = None,
    address: Annotated[str | None, Field(description="Domicilio comercial")] = None,
    iva_condition: Annotated[str | None, Field(description="responsable_inscripto, the only one supported")] = None,
    activity_start: Annotated[str | None, Field(description="Inicio de actividades, YYYY-MM-DD")] = None,
    iibb: Annotated[str | None, Field(description="Ingresos Brutos number, or Exento")] = None,
    point_of_sale: Annotated[
        int | None, Field(description="ARCA point of sale for web services (RECE), like 2")
    ] = None,
    environment: Annotated[
        str | None,
        Field(description="homologacion (tests, the default) or produccion (real invoices); switches to it"),
    ] = None,
    certificate_path: Annotated[
        str | None,
        Field(description="Full path of ARCA's certificate (.crt/.pem) for `environment`, or the current one"),
    ] = None,
    key_path: Annotated[
        str | None, Field(description="Full path of the certificate's private key; only its path is kept")
    ] = None,
    payment_days: Annotated[
        int | None, Field(description="Days from the issue date to the payment due date; 10 by default")
    ] = None,
    sale_condition: Annotated[
        str | None, Field(description='Condición de venta printed on invoices; "Cuenta corriente" by default')
    ] = None,
) -> dict:
    """See or change the issuer's setup. Omitted fields stay as they are; without arguments, it only shows
    it, with each environment's certificate and what issuing still needs."""
    with _db() as conn:
        return service.setup(
            conn,
            cuit_=cuit,
            business_name=business_name,
            address=address,
            iva_condition=iva_condition,
            activity_start=activity_start,
            iibb=iibb,
            point_of_sale=point_of_sale,
            environment=environment,
            certificate_path=certificate_path,
            key_path=key_path,
            payment_days=payment_days,
            sale_condition=sale_condition,
        )


@mcp.tool(annotations=READ_ONLY)
def check_arca() -> dict:
    """Sign in to ARCA with the current environment's certificate and ask for the point of sale's last
    Factura A. Issues nothing."""
    with _db() as conn:
        return service.check(conn, SEND)


# --- Customers ---


@mcp.tool(annotations=READ_ONLY)
def list_customers() -> list[dict]:
    """All customers, with the monthly net amount in effect today and every amount with its date."""
    with _db() as conn:
        return service.list_customers(conn)


@mcp.tool()
def add_customer(
    name: Annotated[str, Field(description="Razón social")],
    cuit: str,
    iva_condition: Annotated[
        str,
        Field(
            description="responsable_inscripto, monotributo, monotributo_social, exento, consumidor_final"
            " or no_alcanzado"
        ),
    ] = "responsable_inscripto",
    address: str = "",
    email: Annotated[str, Field(description="Where invoices are emailed")] = "",
    description: Annotated[
        str, Field(description='The invoice\'s line, followed by the period, like "Servicios de desarrollo"')
    ] = "Servicios",
    monthly_net: Annotated[float | None, Field(description="Monthly net amount before IVA, in pesos")] = None,
    effective_from: Annotated[
        str | None, Field(description="YYYY-MM or YYYY-MM-DD the monthly amount applies from; absent: from the start")
    ] = None,
    id: Annotated[str | None, Field(description="Slug id; derived from name when absent")] = None,
) -> dict:
    """Add a customer."""
    with _db() as conn:
        return service.add_customer(
            conn,
            name,
            cuit,
            iva_condition=iva_condition,
            address=address,
            email=email,
            description=description,
            monthly_net=monthly_net,
            effective_from=effective_from,
            id=id,
        )


@mcp.tool()
def update_customer(
    customer: Annotated[str, Field(description="Its id or CUIT")],
    name: str | None = None,
    cuit: str | None = None,
    iva_condition: str | None = None,
    address: str | None = None,
    email: str | None = None,
    description: str | None = None,
    monthly_net: Annotated[
        float | None, Field(description="A new monthly net amount, from effective_from on; earlier months keep theirs")
    ] = None,
    effective_from: Annotated[
        str | None, Field(description="YYYY-MM or YYYY-MM-DD; absent: the 1st of this month")
    ] = None,
) -> dict:
    """Change a customer. Omitted fields stay as they are; invoices already issued keep the customer as it
    was."""
    with _db() as conn:
        return service.update_customer(
            conn,
            customer,
            name=name,
            cuit_=cuit,
            iva_condition=iva_condition,
            address=address,
            email=email,
            description=description,
            monthly_net=monthly_net,
            effective_from=effective_from,
        )


# --- Invoices ---


@mcp.tool(annotations=READ_ONLY)
def prepare_month(
    customer: Annotated[
        str | None, Field(description="Id or CUIT; absent: every customer with a monthly amount")
    ] = None,
    month: Annotated[str | None, Field(description="YYYY-MM; absent: last month")] = None,
) -> dict:
    """Preview a month's two invoices per customer (1st-15th and 16th-end, each half the monthly amount, plus
    21% IVA), with each one's status and the create_invoice arguments that issue it. Issues nothing."""
    with _db() as conn:
        return service.prepare_month(conn, customer, month)


@mcp.tool()
def create_invoice(
    customer: Annotated[str, Field(description="Id or CUIT")],
    period_from: Annotated[str, Field(description="First day of the services billed, YYYY-MM-DD")],
    period_to: Annotated[str, Field(description="Last day of the services billed, YYYY-MM-DD")],
    net_amount: Annotated[
        float | None,
        Field(description="Net before IVA; absent: the monthly amount, or its half for 1st-15th and 16th-end"),
    ] = None,
    issue_date: Annotated[str | None, Field(description="YYYY-MM-DD; absent: today")] = None,
    due_date: Annotated[
        str | None, Field(description="Payment due date, YYYY-MM-DD; absent: issue date + payment_days")
    ] = None,
    description: Annotated[
        str | None, Field(description="The invoice's line; absent: the customer's, with the period")
    ] = None,
) -> dict:
    """Issue a Factura A with ARCA for services in the period, and make its PDF. If one was already issued
    for this customer and period it's returned (already_issued: true) and nothing is issued."""
    with db.issuing(), _db() as conn:
        return _linked(
            service.create_invoice(
                conn,
                customer,
                period_from,
                period_to,
                net_amount=net_amount,
                issue_date=issue_date,
                due_date=due_date,
                description=description,
                pdf_dir=db.invoices_dir(),
                send=SEND,
            )
        )


@mcp.tool(annotations=READ_ONLY)
def list_invoices(
    customer: Annotated[str | None, Field(description="Id or CUIT")] = None,
    month: Annotated[str | None, Field(description="YYYY-MM the period starts in")] = None,
    status: Annotated[str | None, Field(description="authorized, rejected, sent or draft")] = None,
    environment: Annotated[str | None, Field(description="homologacion or produccion; absent: the current one")] = None,
) -> list[dict]:
    """Invoices, ordered by period."""
    with _db() as conn:
        return [_linked(i) for i in service.list_invoices(conn, customer, month, status, environment)]


@mcp.tool(annotations=READ_ONLY)
def get_invoice(id: int) -> dict:
    """One invoice, with the path of its PDF (made again if it's missing)."""
    with _db() as conn:
        return _linked(service.get_invoice(conn, id, db.invoices_dir()))


def main() -> None:
    # Fail now on a bad database path, not on the first tool call.
    db.connect().close()
    mcp.run()


if __name__ == "__main__":
    main()
