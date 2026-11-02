"""An invoice's PDF, in the layout of ARCA's own invoices: ORIGINAL; the issuer, the letter and the number;
the period; the customer; the line; the totals; and the CAE with the QR of RG 4291."""

import base64
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen.canvas import Canvas

QR_URL = "https://www.afip.gob.ar/fe/qr/?p="

W, H = A4
M = 10 * mm
CW = W - 2 * M
GREY = Color(0.85, 0.85, 0.85)

BOLD, REGULAR, ITALIC = "Helvetica-Bold", "Helvetica", "Helvetica-BoldOblique"


def qr_payload(invoice: dict) -> dict:
    """The data RG 4291's QR carries (ARCA's "Especificaciones del código QR", version 1)."""
    return {
        "ver": 1,
        "fecha": invoice["issue_date"],
        "cuit": int(invoice["issuer"]["cuit"]),
        "ptoVta": invoice["point_of_sale"],
        "tipoCmp": 1,
        "nroCmp": invoice["number"],
        "importe": float(invoice["total"]),
        "moneda": "PES",
        "ctz": 1,
        "tipoDocRec": 80,
        "nroDocRec": int(invoice["customer"]["cuit"]),
        "tipoCodAut": "E",
        "codAut": int(invoice["cae"]),
    }


def qr_url(invoice: dict) -> str:
    data = json.dumps(qr_payload(invoice), separators=(",", ":")).encode()
    return QR_URL + base64.b64encode(data).decode()


def pesos(amount: float | Decimal) -> str:
    """4537500 -> "4.537.500,00", as written in Argentina."""
    return f"{Decimal(str(amount)):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def _day(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y") if iso else ""


def _pair(c: Canvas, x: float, y: float, label: str, value: str, size: float = 8.5, right: float = W - M) -> None:
    """A bold label and its value, the value made smaller (and cut, at worst) to end before `right`."""
    c.setFont(BOLD, size)
    c.drawString(x, y, label)
    x += c.stringWidth(label, BOLD, size) + 3
    room = right - x
    fitted = size
    while fitted > 6 and c.stringWidth(value, REGULAR, fitted) > room:
        fitted -= 0.5
    while value and c.stringWidth(value, REGULAR, fitted) > room:
        value = value[:-2] + "…"
    c.setFont(REGULAR, fitted)
    c.drawString(x, y, value)


def render(invoice: dict, path: Path) -> None:
    """Write the PDF of an authorized invoice: one of service.get_invoice's, with `issuer` and `customer` as
    they were when it was issued."""
    issuer, customer = invoice["issuer"], invoice["customer"]
    number = f"{invoice['number']:08d}"
    point_of_sale = f"{invoice['point_of_sale']:05d}"
    tmp = path.with_suffix(".tmp")
    c = Canvas(str(tmp), pagesize=A4, invariant=1)
    c.setTitle(f"Factura A {point_of_sale}-{number}")
    c.setAuthor(issuer["business_name"])
    c.setLineWidth(0.8)

    # ORIGINAL
    top = H - M
    c.rect(M, top - 9 * mm, CW, 9 * mm)
    c.setFont(BOLD, 12)
    c.drawCentredString(W / 2, top - 6.2 * mm, "ORIGINAL")

    # The issuer, the letter and the number.
    top -= 9 * mm
    height = 46 * mm
    c.rect(M, top - height, CW, height)
    c.line(W / 2, top - height, W / 2, top - 15 * mm)
    c.setFillColor(white)
    c.rect(W / 2 - 8 * mm, top - 15 * mm, 16 * mm, 15 * mm, fill=1)
    c.setFillColor(black)
    c.setFont(BOLD, 26)
    c.drawCentredString(W / 2, top - 9.5 * mm, "A")
    c.setFont(BOLD, 7)
    c.drawCentredString(W / 2, top - 13 * mm, "COD. 01")

    x, y = M + 4 * mm, top - 12 * mm
    for line in simpleSplit(issuer["business_name"], BOLD, 15, CW / 2 - 16 * mm)[:2]:
        c.setFont(BOLD, 15)
        c.drawString(x, y, line)
        y -= 7 * mm
    y = min(y, top - 24 * mm)
    _pair(c, x, y, "Razón Social:", issuer["business_name"], right=W / 2 - 3 * mm)
    y -= 5.5 * mm
    address = simpleSplit(issuer["address"], REGULAR, 8.5, CW / 2 - 40 * mm) or [""]
    _pair(c, x, y, "Domicilio Comercial:", address[0])
    for line in address[1:2]:
        y -= 4 * mm
        c.drawString(x + 30 * mm, y, line)
    y -= 5.5 * mm
    _pair(c, x, y, "Condición frente al IVA:", issuer["iva_condition_label"])

    x, y = W / 2 + 12 * mm, top - 10 * mm
    c.setFont(BOLD, 18)
    c.drawString(x, y, "FACTURA")
    y -= 8 * mm
    _pair(c, x, y, "Punto de Venta:", point_of_sale, 9)
    _pair(c, x + 40 * mm, y, "Comp. Nro:", number, 9)
    for label, value in (
        ("Fecha de Emisión:", _day(invoice["issue_date"])),
        ("CUIT:", issuer["cuit"]),
        ("Ingresos Brutos:", issuer["iibb"]),
        ("Fecha de Inicio de Actividades:", _day(issuer["activity_start"])),
    ):
        y -= 6 * mm
        _pair(c, x, y, label, value)

    # The period.
    top -= height + 1.5 * mm
    c.rect(M, top - 8 * mm, CW, 8 * mm)
    y = top - 5.3 * mm
    _pair(c, M + 4 * mm, y, "Período Facturado Desde:", _day(invoice["period_from"]))
    _pair(c, M + 62 * mm, y, "Hasta:", _day(invoice["period_to"]))
    _pair(c, M + 100 * mm, y, "Fecha de Vto. para el pago:", _day(invoice["due_date"]))

    # The customer.
    top -= 8 * mm + 1.5 * mm
    c.rect(M, top - 22 * mm, CW, 22 * mm)
    x2 = M + 62 * mm
    y = top - 6 * mm
    _pair(c, M + 4 * mm, y, "CUIT:", customer["cuit"])
    _pair(c, x2, y, "Apellido y Nombre / Razón Social:", customer["name"], right=W - M - 3 * mm)
    y -= 6 * mm
    _pair(c, M + 4 * mm, y, "Condición frente al IVA:", customer["iva_condition_label"])
    _pair(c, x2 + 32 * mm, y, "Domicilio:", customer["address"], right=W - M - 3 * mm)
    y -= 6 * mm
    _pair(c, M + 4 * mm, y, "Condición de venta:", issuer["sale_condition"])

    # The line.
    top -= 22 * mm + 1.5 * mm
    columns = [  # (title, width in mm, right-aligned)
        ("Código", 12, False),
        ("Producto / Servicio", 54, False),
        ("Cantidad", 14, True),
        ("U. Medida", 16, False),
        ("Precio Unit.", 22, True),
        ("% Bonif", 12, True),
        ("Subtotal", 22, True),
        ("Alícuota IVA", 18, True),
        ("Subtotal c/IVA", 20, True),
    ]
    c.setFillColor(GREY)
    c.rect(M, top - 7 * mm, CW, 7 * mm, fill=1)
    c.setFillColor(black)
    description = simpleSplit(invoice["description"], REGULAR, 8, 51 * mm)
    values = [
        "",
        description,
        "1,00",
        "unidades",
        pesos(invoice["net"]),
        "0,00",
        pesos(invoice["net"]),
        "21%",
        pesos(invoice["total"]),
    ]
    x = M
    for (title, width, right), value in zip(columns, values, strict=True):
        width *= mm
        c.setFont(BOLD, 7)
        if right:
            c.drawRightString(x + width - 1.5 * mm, top - 4.6 * mm, title)
        else:
            c.drawString(x + 1.5 * mm, top - 4.6 * mm, title)
        c.setFont(REGULAR, 8)
        y = top - 12 * mm
        for line in value if isinstance(value, list) else [value]:
            if right:
                c.drawRightString(x + width - 1.5 * mm, y, line)
            else:
                c.drawString(x + 1.5 * mm, y, line)
            y -= 4 * mm
        x += width

    # The totals.
    footer = M + 40 * mm
    box = 56 * mm
    c.rect(M, footer, CW, box)
    rows = [
        ("Importe Neto Gravado: $", invoice["net"]),
        ("IVA 27%: $", 0),
        ("IVA 21%: $", invoice["iva"]),
        ("IVA 10.5%: $", 0),
        ("IVA 5%: $", 0),
        ("IVA 2.5%: $", 0),
        ("IVA 0%: $", 0),
        ("Importe Otros Tributos: $", 0),
        ("Importe Total: $", invoice["total"]),
    ]
    y = footer + box - 7 * mm
    for label, amount in rows:
        c.setFont(BOLD, 9 if label.startswith("Importe Total") else 8.5)
        c.drawRightString(W - M - 40 * mm, y, label)
        c.drawRightString(W - M - 4 * mm, y, pesos(amount))
        y -= 5.6 * mm

    # The CAE and the QR.
    size = 32 * mm
    widget = QrCodeWidget(qr_url(invoice), barLevel="M")
    x1, y1, x2_, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2_ - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, M, M + 4 * mm)
    x = M + size + 4 * mm
    c.setFont(BOLD, 16)
    c.drawString(x, M + 27 * mm, "ARCA")
    c.setFont(ITALIC, 9)
    c.drawString(x, M + 21 * mm, "Comprobante Autorizado")
    c.setFont(REGULAR, 6.5)
    c.drawString(
        x, M + 15 * mm, "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operación"
    )
    c.setFont(BOLD, 8.5)
    c.drawCentredString(W / 2 - 10 * mm, M + 6 * mm, "Pág. 1/1")
    _cae(c, "CAE N°:", invoice["cae"], M + 27 * mm)
    _cae(c, "Fecha de Vto. de CAE:", _day(invoice["cae_due"]), M + 21 * mm)

    if invoice["environment"] == "homologacion":
        c.saveState()
        c.setFillColor(Color(0.8, 0.1, 0.1, alpha=0.18))
        c.setFont(BOLD, 34)
        c.translate(W / 2, H / 2)
        c.rotate(35)
        c.drawCentredString(0, 0, "HOMOLOGACIÓN · SIN VALIDEZ FISCAL")
        c.restoreState()

    c.showPage()
    c.save()
    os.replace(tmp, path)


def _cae(c: Canvas, label: str, value: str, y: float) -> None:
    c.setFont(REGULAR, 9)
    c.drawRightString(W - M, y, value)
    c.setFont(BOLD, 9)
    c.drawRightString(W - M - c.stringWidth(value, REGULAR, 9) - 3, y, label)
