"""One file, several invoices.

A week's invoices get scanned as one PDF; someone photographs two invoices
and sends both pictures in one email; a distributor puts a statement in
front of the invoices it lists. The app read every file as one invoice, so
these came out as one invoice whose items were everything on every page and
whose total was the first one found: held for not adding up, with nothing a
person could do but split the file themselves.

The reader now says which invoice each page belongs to
(ExtractedInvoice.page_invoices) and reads only the first. The invoice that
was added keeps that first one; each of the others becomes an invoice of its
own, from its own pages, read on its own like any other.
"""

# More than this isn't a stack of invoices but a misreading of one long
# document; left whole, it's held for a person.
MAX_INVOICES_PER_FILE = 20


def plan(page_invoices: list[int], pdf_pages: list[int]) -> dict[int, list[int]] | None:
    """Each invoice's pages of the PDF (from 0), by its number, when the
    file holds more than one; None when it's one invoice, or when the answer
    can't be used.

    `page_invoices[i]` is the invoice the reader put page image i in (0 for
    none); `pdf_pages[i]` is the page of the PDF that image shows, which
    differs from i when a tall page was cut into several images
    (app/ingest/render.py)."""
    if len(page_invoices) != len(pdf_pages):
        return None
    invoice_of: dict[int, int] = {}
    for number, pdf_page in zip(page_invoices, pdf_pages):
        if number < 0:
            return None
        seen = invoice_of.get(pdf_page, 0)
        if number and seen and number != seen:
            # Two invoices on one page: not something pages can separate.
            return None
        invoice_of[pdf_page] = seen or number
    pages_of: dict[int, list[int]] = {}
    for pdf_page in sorted(invoice_of):
        if invoice_of[pdf_page]:
            pages_of.setdefault(invoice_of[pdf_page], []).append(pdf_page)
    if not 2 <= len(pages_of) <= MAX_INVOICES_PER_FILE:
        return None
    return pages_of
