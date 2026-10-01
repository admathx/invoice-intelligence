from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

from app.ingest.pdfium_lock import PDFIUM_LOCK

RENDER_DPI = 150

# A page this many times as tall as it is wide is a till receipt (or a long
# scroll of one), not a sheet of paper or a photo of one: the tallest phone
# photos are 21:9, about 2.3. Rendered whole like any page, a
# 3 x 12 inch receipt came out 450 pixels wide, and the model sees images
# no bigger than about 1,570 on their long side: forty rows of small print
# in under 400 pixels. So a tall page is rendered at a sheet's width and cut
# into sheet-sized images, each overlapping the last so no row is lost to a
# cut through its middle (the extraction prompt says rows may repeat).
TALL_PAGE_ASPECT = 2.5
TILE_WIDTH = round(8.5 * RENDER_DPI)
TILE_HEIGHT = round(11 * RENDER_DPI)
TILE_OVERLAP = 200
MAX_TILES = 12
# The most page images one read is given. An upload is limited to this many
# pages (settings.max_invoice_pages); tall pages cut into slices can still
# pass it, and the worker then stops before paying for a read that the model
# would refuse.
MAX_PAGE_IMAGES = 100
_TILE_STEP = TILE_HEIGHT - TILE_OVERLAP
_MAX_TALL_HEIGHT = TILE_HEIGHT + (MAX_TILES - 1) * _TILE_STEP


@dataclass(frozen=True)
class RenderedPage:
    path: Path
    # Which page of the PDF this image shows (from 0): all of it, or for a
    # tall page, one slice of it.
    pdf_page: int


def tile_tops(height: int) -> list[int]:
    """Where each slice of a tall page starts, top to bottom: as few as
    cover it with at least TILE_OVERLAP shared between neighbours, evenly
    spaced, the last ending at the page's foot."""
    if height <= TILE_HEIGHT:
        return [0]
    gaps = -(-(height - TILE_HEIGHT) // _TILE_STEP)  # rounded up
    return [round(i * (height - TILE_HEIGHT) / gaps) for i in range(gaps + 1)]


def render_pages(pdf_bytes: bytes, out_dir: Path) -> list[RenderedPage]:
    """Render each page of a PDF to a PNG at RENDER_DPI, in order; a tall
    page becomes several (above).

    Takes the bytes rather than a path: the original may live in a bucket
    (app/storage.py), and the worker reads it from wherever it is."""
    out_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[RenderedPage] = []

    def save(image, pdf_page: int) -> None:
        path = out_dir / f"page_{len(rendered) + 1:03d}.png"
        image.save(path)
        rendered.append(RenderedPage(path, pdf_page))

    with PDFIUM_LOCK:  # app/ingest/pdfium_lock.py
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            for i, page in enumerate(pdf):
                width, height = page.get_size()
                if width > 0 and height / width > TALL_PAGE_ASPECT:
                    scale = min(TILE_WIDTH / width, _MAX_TALL_HEIGHT / height)
                    image = page.render(scale=scale).to_pil()
                    for top in tile_tops(image.height):
                        save(image.crop((0, top, image.width, min(image.height, top + TILE_HEIGHT))), i)
                else:
                    save(page.render(scale=RENDER_DPI / 72).to_pil(), i)
                page.close()
        finally:
            pdf.close()

    return rendered


def render_pdf_to_pngs(pdf_bytes: bytes, out_dir: Path) -> list[Path]:
    """The page images' paths alone, for callers that only read them."""
    return [page.path for page in render_pages(pdf_bytes, out_dir)]


def pdf_of_pages(pdf_bytes: bytes, pages: list[int]) -> bytes:
    """A PDF holding only the given pages (from 0) of another, in that
    order: one invoice out of a file that held several."""
    import io

    with PDFIUM_LOCK:
        source = pdfium.PdfDocument(pdf_bytes)
        try:
            out = pdfium.PdfDocument.new()
            try:
                out.import_pages(source, pages)
                buffer = io.BytesIO()
                out.save(buffer)
                return buffer.getvalue()
            finally:
                out.close()
        finally:
            source.close()
