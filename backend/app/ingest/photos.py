"""Phone photos of paper invoices, turned into the PDF the pipeline expects.

Most invoices a restaurant gets are paper, handed over at the back door, and
the quickest way in is a phone photo. Rather than teach every later step
(storage, rendering, the review screen's page images, extraction) a second
kind of file, the photos become one PDF here, a page per photo, and from then
on the invoice is like any other.

Each photo is:
- turned upright from its EXIF orientation (phones store most photos
  sideways and only flag the rotation, which a PDF would ignore);
- flattened onto white if it has transparency;
- scaled down to MAX_LONG_EDGE, which is more detail than extraction uses
  (render.RENDER_DPI brings the page back to ~1,650 px) and keeps a
  multi-page invoice a few MB instead of tens;
- placed on a page whose long side is PAGE_LONG_EDGE_INCHES, so the renderer
  sees a letter-sized page, not a 56-inch one (a PDF page's size comes from
  the image's DPI, which for phone photos is usually absent, meaning 72).
"""
import io

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

try:  # HEIC/HEIF: what iPhones save by default.
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - the requirement is pinned; this is belt and braces
    pillow_heif = None

from app.ingest.pdfium_lock import PDFIUM_LOCK  # noqa: E402 (after the HEIC opener is registered)

MAX_PHOTOS = 10
MAX_LONG_EDGE = 3000
PAGE_LONG_EDGE_INCHES = 11
JPEG_QUALITY = 85
# What may be decoded, after a JPEG's reduced-size decode (below): ~150 MB of
# memory in the API process per photo. A 50 MP phone photo fits whole; a
# 200 MP one is a JPEG and decodes at a quarter of that. Also the guard
# against a small file that decompresses into gigabytes; Pillow's own only
# warns below twice its default limit.
MAX_PIXELS = 50_000_000

_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1")


def image_kind(data: bytes) -> str | None:
    """What kind of image the bytes are, from their signature (never the
    filename or Content-Type, which the sender controls); None if not one
    this accepts."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "heic"
    return None


class UnreadablePhotoError(ValueError):
    pass


def _page(data: bytes, position: int) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        # JPEGs decode straight to (about) the size kept, not the full sensor:
        # a no-op for every other format.
        image.draft("RGB", (MAX_LONG_EDGE, MAX_LONG_EDGE))
        width, height = image.size
    except Image.DecompressionBombError as exc:  # Pillow's own ceiling, ~179 MP, checked on open
        raise UnreadablePhotoError(f"Photo {position} is too big.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise UnreadablePhotoError(f"We couldn't open photo {position}.") from exc
    if width * height > MAX_PIXELS:
        raise UnreadablePhotoError(f"Photo {position} is too big.")
    try:
        image = ImageOps.exif_transpose(image)
        image.load()
    except (OSError, Image.DecompressionBombError, SyntaxError, ValueError) as exc:
        raise UnreadablePhotoError(f"We couldn't open photo {position}.") from exc

    if image.mode in ("RGBA", "LA", "PA", "P"):
        image = image.convert("RGBA")
        flat = Image.new("RGB", image.size, "white")
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    elif image.mode != "RGB":
        image = image.convert("RGB")

    image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)
    return image


def photos_to_pdf(photos: list[bytes]) -> bytes:
    """One PDF, a page per photo, in the order given."""
    if not photos:
        raise UnreadablePhotoError("Choose a photo to upload.")
    if len(photos) > MAX_PHOTOS:
        raise UnreadablePhotoError(f"One invoice can have up to {MAX_PHOTOS} photos.")
    # Each page saved on its own, at its own resolution, then joined: Pillow's
    # multi-page save applies the first page's resolution to every page, so a
    # second photo from a different camera came out a different paper size.
    pages = []
    for i, data in enumerate(photos, start=1):
        page = _page(data, i)
        single = io.BytesIO()
        page.save(single, format="PDF", resolution=max(page.size) / PAGE_LONG_EDGE_INCHES, quality=JPEG_QUALITY)
        pages.append(single.getvalue())
    with PDFIUM_LOCK:  # app/ingest/pdfium_lock.py; the photo work above runs unlocked
        merged = pdfium.PdfDocument.new()
        try:
            for single_pdf in pages:
                source = pdfium.PdfDocument(single_pdf)
                try:
                    merged.import_pages(source)
                finally:
                    source.close()
            out = io.BytesIO()
            merged.save(out)
            return out.getvalue()
        finally:
            merged.close()
