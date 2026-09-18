"""Deliberate noise for the "noisy subset" of the corpus (SPEC.md §8): scan skew and
JPEG artifacts. Applied by rasterizing the clean vector PDF, degrading the raster,
and re-wrapping it as an image-only PDF — i.e. simulating what a phone photo or a
flatbed scan of the same invoice would look like.
"""
import io
import random

import pypdfium2 as pdfium
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

RENDER_DPI = 150


def apply_scan_noise(clean_pdf_bytes: bytes, rng: random.Random) -> bytes:
    skew_deg = rng.uniform(-2.5, 2.5)
    jpeg_quality = rng.randint(40, 70)

    src = pdfium.PdfDocument(clean_pdf_bytes)
    out_buf = io.BytesIO()
    c = rl_canvas.Canvas(out_buf, pagesize=letter, invariant=1)

    try:
        for page in src:
            bitmap = page.render(scale=RENDER_DPI / 72)
            image = bitmap.to_pil().convert("RGB")
            page.close()

            rotated = image.rotate(skew_deg, expand=True, fillcolor=(255, 255, 255))

            jpeg_buf = io.BytesIO()
            rotated.save(jpeg_buf, format="JPEG", quality=jpeg_quality)
            jpeg_buf.seek(0)
            degraded = Image.open(jpeg_buf)

            page_w, page_h = letter
            img_reader_buf = io.BytesIO()
            degraded.save(img_reader_buf, format="JPEG", quality=jpeg_quality)
            img_reader_buf.seek(0)

            from reportlab.lib.utils import ImageReader

            reader = ImageReader(img_reader_buf)
            iw, ih = degraded.size
            scale = min(page_w / iw, page_h / ih)
            draw_w, draw_h = iw * scale, ih * scale
            x = (page_w - draw_w) / 2
            y = (page_h - draw_h) / 2
            c.drawImage(reader, x, y, width=draw_w, height=draw_h)
            c.showPage()
    finally:
        src.close()

    c.save()
    return out_buf.getvalue()
