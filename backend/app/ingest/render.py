from pathlib import Path

import pypdfium2 as pdfium

from app.ingest.pdfium_lock import PDFIUM_LOCK

RENDER_DPI = 150


def render_pdf_to_pngs(pdf_bytes: bytes, out_dir: Path) -> list[Path]:
    """Render each page of a PDF to a PNG at RENDER_DPI. Returns page image paths in order.

    Takes the bytes rather than a path: the original may live in a bucket
    (app/storage.py), and the worker reads it from wherever it is."""
    out_dir.mkdir(parents=True, exist_ok=True)

    scale = RENDER_DPI / 72
    page_paths = []
    with PDFIUM_LOCK:  # app/ingest/pdfium_lock.py
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            for i, page in enumerate(pdf):
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                page_path = out_dir / f"page_{i + 1:03d}.png"
                image.save(page_path)
                page_paths.append(page_path)
                page.close()
        finally:
            pdf.close()

    return page_paths
