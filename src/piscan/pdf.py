import io
from pathlib import Path

import img2pdf
import pikepdf


def build_pdf(pages: list[tuple[Path, int]]) -> bytes:
    """One PDF from (jpeg path, rotation degrees) pairs.

    img2pdf embeds the JPEG bytes as they are, so scans are never re-encoded.
    Rotation goes in /Rotate rather than touching the pixels.
    """
    if not pages:
        raise ValueError("no pages to send")
    raw = img2pdf.convert([str(p) for p, _ in pages], nodate=True)
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        for page, (_, rotation) in zip(pdf.pages, pages, strict=True):
            page.Rotate = rotation % 360
        # Same pages must give the same bytes, or Paperless can't spot a re-send.
        if "/ID" in pdf.trailer:
            del pdf.trailer["/ID"]
        out = io.BytesIO()
        pdf.save(out, deterministic_id=True)
    return out.getvalue()
