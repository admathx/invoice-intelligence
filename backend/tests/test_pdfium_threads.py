"""PDFium isn't thread-safe; the parallel test-set read segfaulted rendering
two PDFs at once. The API's thread pool does the same with two uploads at
once, so every pdfium call takes one lock (app/ingest/pdfium_lock.py).
Without it this test crashes the test process outright."""
import io
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
from reportlab.pdfgen import canvas

from app.ingest.photos import photos_to_pdf
from app.ingest.render import render_pdf_to_pngs
from app.ingest.upload import is_password_protected


def _pdf(pages: int = 3) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"page {i}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_pdfs_can_be_opened_from_many_threads_at_once():
    pdf, photo = _pdf(), _photo()

    def work(i: int) -> int:
        with tempfile.TemporaryDirectory() as scratch:
            pages = render_pdf_to_pngs(pdf, Path(scratch))
        assert not is_password_protected(pdf)
        merged = photos_to_pdf([photo, photo])
        return len(pages) + len(merged)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(32)))
    assert len(results) == 32
