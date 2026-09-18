from pathlib import Path

import pypdfium2 as pdfium

RENDER_DPI = 150


def render_pdf_to_pngs(pdf_path: str, out_dir: Path) -> list[Path]:
    """Render each page of a PDF to a PNG at RENDER_DPI. Returns page image paths in order."""
    local_path = pdf_path.removeprefix("file://")
    out_dir.mkdir(parents=True, exist_ok=True)

    scale = RENDER_DPI / 72
    pdf = pdfium.PdfDocument(local_path)
    page_paths = []
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
