import io

import pikepdf
from PIL import Image

from piscan.pdf import build_pdf


def jpeg(tmp_path, name, size, colour):
    p = tmp_path / name
    Image.new("RGB", size, colour).save(p, "JPEG")
    return p


def test_pages_rotation_and_untouched_images(tmp_path):
    a = jpeg(tmp_path, "a.jpg", (300, 200), (200, 10, 10))
    b = jpeg(tmp_path, "b.jpg", (200, 300), (10, 200, 10))
    c = jpeg(tmp_path, "c.jpg", (250, 250), (10, 10, 200))
    data = build_pdf([(a, 0), (b, 90), (c, 270)])
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == 3
        assert [int(p.get("/Rotate", 0)) for p in pdf.pages] == [0, 90, 270]
        for page, src in zip(pdf.pages, [a, b, c], strict=True):
            (img,) = page.Resources.XObject.values()
            assert img.read_raw_bytes() == src.read_bytes()
