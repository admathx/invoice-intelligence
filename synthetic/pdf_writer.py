"""Renders a synthetic invoice (header + line items) to PDF bytes using reportlab,
with per-distributor column order/labels from templates/*.py.

Reproducibility: reportlab embeds a creation date and a random document ID by
default, which would make two runs with the same seed differ byte-for-byte even
with identical content. `rl_config.invariant = 1` turns both off.
"""
import io

import reportlab.rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from synthetic.templates.base import DistributorLayout

reportlab.rl_config.invariant = 1

_DESC_STYLE = ParagraphStyle(name="desc", fontName="Helvetica", fontSize=7, leading=8.5)
_HEADER_STYLE = ParagraphStyle(name="header", fontName="Helvetica-Bold", fontSize=7, leading=8.5)

_COLUMN_WIDTHS = {
    "line_number": 0.28 * inch,
    "sku": 0.68 * inch,
    "pack_size": 0.68 * inch,
    "qty": 0.42 * inch,
    "uom": 0.36 * inch,
    "unit_price": 0.62 * inch,
    "ext_price": 0.62 * inch,
}


def render_invoice_pdf(
    layout: DistributorLayout,
    header: dict,
    columns: tuple[str, ...],
    rows: list[dict],
    totals: dict,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
    )

    elements = []

    elements.append(Paragraph(f"<b>{layout.display_name}</b>", ParagraphStyle(name="co", fontSize=13, leading=16)))
    for line in layout.address_lines:
        elements.append(Paragraph(line, ParagraphStyle(name="addr", fontSize=8, leading=10)))
    elements.append(Spacer(1, 10))

    meta_lines = [
        f"<b>Ship To:</b> {header['tenant_name']}",
        f"<b>Invoice #:</b> {header['invoice_number']}",
        f"<b>Invoice Date:</b> {header['invoice_date']}",
        f"<b>Delivery Date:</b> {header.get('delivery_date') or '-'}",
    ]
    for line in meta_lines:
        elements.append(Paragraph(line, ParagraphStyle(name="meta", fontSize=8, leading=11)))
    elements.append(Spacer(1, 10))

    description_width = doc.width - sum(_COLUMN_WIDTHS[c] for c in columns if c != "description")
    col_widths = [description_width if c == "description" else _COLUMN_WIDTHS[c] for c in columns]

    header_row = [Paragraph(layout.column_labels[c], _HEADER_STYLE) for c in columns]
    table_data = [header_row]
    for row in rows:
        cells = []
        for c in columns:
            value = row.get(c, "")
            if c == "description":
                cells.append(Paragraph(str(value), _DESC_STYLE))
            else:
                cells.append(Paragraph(str(value), _DESC_STYLE))
        table_data.append(cells)

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    elements.append(table)
    elements.append(Spacer(1, 10))

    totals_style = ParagraphStyle(name="totals", fontSize=9, leading=13, alignment=2)
    elements.append(Paragraph(f"Subtotal: {totals['subtotal']}", totals_style))
    elements.append(Paragraph(f"Tax: {totals['tax']}", totals_style))
    elements.append(Paragraph(f"<b>Total: {totals['total']}</b>", totals_style))

    doc.build(elements)
    return buf.getvalue()
