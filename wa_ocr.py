"""Read the text inside pictures (e.g. screenshots of WhatsApp chats with owner listings). Runs locally, free."""
from functools import lru_cache
from pathlib import Path


# WhatsApp colours: dark & light theme backgrounds, headers, chat bubbles
WA_COLOURS = [(11, 20, 26), (17, 27, 33), (31, 44, 52), (32, 44, 51), (0, 92, 75), (17, 27, 33),
              (239, 234, 226), (255, 255, 255), (240, 242, 245), (217, 253, 211), (0, 128, 105), (0, 168, 132)]


def is_chat_screenshot(image_path, threshold=0.45):
    """Quick check (no text reading) whether a picture is a screenshot of a WhatsApp chat rather than an ordinary
    photo: most of it is WhatsApp's own colours (background, header, bubbles). Works for cropped screenshots too.
    On real pictures: listing screenshots scored 0.52-0.82, room / building photos 0.02-0.33."""
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            px = list(im.convert("RGB").resize((80, 80)).getdata())
    except Exception:
        return False
    near = sum(1 for p in px if any(abs(p[0] - c[0]) + abs(p[1] - c[1]) + abs(p[2] - c[2]) < 36 for c in WA_COLOURS))
    return near / len(px) >= threshold


@lru_cache(maxsize=1)
def _engine():
    from rapidocr import RapidOCR  # loaded on first use (downloads its small model files once)
    return RapidOCR()


def read_pdf(pdf_path, max_pages=10):
    """Text of a PDF: its own text layer (all pages - fast), or - for scanned PDFs - the first `max_pages` pages
    read like pictures."""
    try:
        from pypdf import PdfReader
        text = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf_path)).pages).strip()
        if len(text) >= 30:
            return text
    except Exception:
        text = ""
    try:  # scanned PDF: render pages to images and read them
        import tempfile

        import pypdfium2 as pdfium
        doc, parts = pdfium.PdfDocument(str(pdf_path)), []
        for i in range(min(len(doc), max_pages)):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                doc[i].render(scale=2).to_pil().save(f.name)
            parts.append(read_text(f.name))
            Path(f.name).unlink(missing_ok=True)
        return "\n".join(p for p in parts if p) or text
    except Exception:
        return text


def read_text(image_path):
    """Text in the picture, top-to-bottom, one line per detected text line ('' if none). Small / long screenshots
    are enlarged first - WhatsApp shrinks them and small text reads badly."""
    try:
        src = str(image_path)
        from PIL import Image
        with Image.open(src) as im:
            w, h = im.size
            if w < 1100:
                import tempfile
                scale = 1600 / w
                big = im.convert("RGB").resize((1600, int(h * scale)), Image.LANCZOS)
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                big.save(tmp.name)
                tmp.close()
                src = tmp.name
        res = _engine()(src)
        if src != str(image_path):
            Path(src).unlink(missing_ok=True)
    except Exception:
        return ""
    boxes, texts = getattr(res, "boxes", None), getattr(res, "txts", None)
    if boxes is None or not texts:
        return ""
    # Sort by top edge, then left edge, and merge pieces that sit on the same line
    items = sorted(((min(p[1] for p in b), min(p[0] for p in b), t) for b, t in zip(boxes, texts)),
                   key=lambda x: (round(x[0] / 12), x[1]))
    lines, last_y = [], None
    for y, _, t in items:
        if last_y is not None and abs(y - last_y) < 12:
            lines[-1] += " " + t
        else:
            lines.append(t)
        last_y = y
    return "\n".join(lines)
