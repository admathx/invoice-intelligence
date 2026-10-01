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

# The most invoices one file is divided into: as many as it may have pages
# (settings.max_invoice_pages), a month of single-page invoices scanned
# together. It was 20, and a 24-page stack was then read as its first
# invoice alone, Ready, with the other twenty-three gone.
MAX_INVOICES_PER_FILE = 100


def _pages_by_invoice(page_invoices: list[int], pdf_pages: list[int]) -> dict[int, list[int]] | None:
    """Each numbered invoice's pages of the PDF (from 0); None when the
    answer isn't one about these page images.

    A tall page is cut into several images (app/ingest/render.py), and it
    is still one page: it belongs to the first invoice any of its images
    was given. (The reader, shown a long receipt in four slices at the end
    of a stack, numbered them as four invoices.)"""
    if len(page_invoices) != len(pdf_pages) or any(number < 0 for number in page_invoices):
        return None
    invoice_of: dict[int, int] = {}
    for number, pdf_page in zip(page_invoices, pdf_pages):
        invoice_of[pdf_page] = invoice_of.get(pdf_page, 0) or number
    pages_of: dict[int, list[int]] = {}
    for pdf_page in sorted(invoice_of):
        if invoice_of[pdf_page]:
            pages_of.setdefault(invoice_of[pdf_page], []).append(pdf_page)
    return pages_of


def plan(page_invoices: list[int], pdf_pages: list[int]) -> dict[int, list[int]] | None:
    """Each invoice's pages of the PDF (from 0), by its number, when the
    file holds more than one; None when it's one invoice, or when the answer
    can't be used (cannot_be_separated says which).

    `page_invoices[i]` is the invoice the reader put page image i in (0 for
    none); `pdf_pages[i]` is the page of the PDF that image shows, which
    differs from i when a tall page was cut into several images."""
    pages_of = _pages_by_invoice(page_invoices, pdf_pages)
    if pages_of is None or not 2 <= len(pages_of) <= MAX_INVOICES_PER_FILE:
        return None
    return pages_of


def cannot_be_separated(page_invoices: list[int], pdf_pages: list[int]) -> bool:
    """Whether the reading says there are several invoices here but gives
    nothing they can be divided by: an answer that isn't about these pages,
    or more invoices than a file is divided into. Such a file must not go
    on as if it were its first invoice."""
    pages_of = _pages_by_invoice(page_invoices, pdf_pages)
    return pages_of is None or len(pages_of) > MAX_INVOICES_PER_FILE
