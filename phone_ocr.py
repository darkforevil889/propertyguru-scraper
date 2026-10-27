"""Text lines from PDFs and photos for the phone filter - free, offline OCR (RapidOCR = PaddleOCR models).

* PDF: each page's own text when it has some (fast, exact); scanned pages are rendered sharp and read by OCR
* photos / screenshots (.jpg .png .webp): OCR, small ones enlarged first
* OCR slips inside numbers are fixed before the phone filter sees them (O->0, l/I/|->1, S->5, B->8, Z->2)
Used by phone_filter.filter_file and, as a command, by the new KL Tracker:
    python phone_ocr.py <file> [<original name>]   ->  JSON {"lines": [...], "pages": n, "ocr_pages": n}
"""
import io
import json
import logging
import re
import sys
from pathlib import Path

PDF = {".pdf"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
logging.getLogger("RapidOCR").setLevel(logging.WARNING)
MAX_PAGES = 80          # a long phone list PDF is fine; beyond that it's probably not a list
SCALE = 3               # page render scale for OCR (~216 dpi) - small digits read much better than at 2

# a run that looks like a phone number with OCR letters mixed in: digits, separators and look-alike letters
_RUN = re.compile(r"[+(]?[0-9OoIl|SBZ][0-9OoIl|SBZ\s().\-]{7,}[0-9OoIl|SBZ]")
_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})


def fix_digits(line):
    """Turn look-alike letters into digits, but only inside number-like runs that are mostly digits already
    (so names such as 'BOB' or 'LISA' are left alone)."""
    def fix(m):
        run = m.group(0)
        digits = sum(c.isdigit() for c in run)
        letters = sum(c in "OoIl|SBZ" for c in run)
        return run.translate(_FIX) if digits >= 6 and letters <= 3 else run
    return _RUN.sub(fix, line)


def _lines(res, gap=14):
    """RapidOCR boxes -> text lines, top to bottom, pieces on the same line joined left to right."""
    boxes, texts = getattr(res, "boxes", None), getattr(res, "txts", None)
    if boxes is None or not texts:
        return []
    items = sorted(((min(p[1] for p in b), min(p[0] for p in b), t) for b, t in zip(boxes, texts)), key=lambda x: (x[0], x[1]))
    rows = []
    for y, x, t in items:
        if rows and abs(y - rows[-1][0]) < gap:
            rows[-1][1].append((x, t))
        else:
            rows.append([y, [(x, t)]])
    return ["  ".join(t for _, t in sorted(parts)) for _, parts in rows]


def _engine():
    global _ENGINE
    try:
        return _ENGINE
    except NameError:
        import logging

        from rapidocr import RapidOCR  # downloads its small model files once
        _ENGINE = RapidOCR()
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        return _ENGINE


def ocr_image(img):
    """PIL image -> text lines."""
    img = img.convert("RGB")
    if img.width < 1400:  # small photos / screenshots read badly: enlarge
        from PIL import Image
        img = img.resize((1600, int(img.height * 1600 / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return _lines(_engine()(buf.getvalue()))


def text_lines(filename, data):
    """(lines, pages, ocr_pages) for a PDF or a picture."""
    ext = Path(filename).suffix.lower()
    if ext in IMAGES:
        from PIL import Image
        return [fix_digits(l) for l in ocr_image(Image.open(io.BytesIO(data)))], 1, 1
    if ext not in PDF:
        raise ValueError(f"Not a PDF or picture: {filename}")
    import pypdfium2 as pdfium
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    doc = None
    lines, ocr_pages = [], 0
    pages = len(reader.pages)
    for i in range(min(pages, MAX_PAGES)):
        own = (reader.pages[i].extract_text() or "").strip()
        if len(re.sub(r"\s", "", own)) >= 20:  # the page has real text: use it as is
            lines += own.splitlines()
            continue
        doc = doc or pdfium.PdfDocument(data)  # scanned page: render it and read it
        lines += ocr_image(doc[i].render(scale=SCALE).to_pil())
        ocr_pages += 1
    return [fix_digits(l) for l in lines if l.strip()], pages, ocr_pages


if __name__ == "__main__":
    path = Path(sys.argv[1])
    name = sys.argv[2] if len(sys.argv) > 2 else path.name
    try:
        lines, pages, ocr = text_lines(name, path.read_bytes())
        out = {"lines": lines, "pages": pages, "ocr_pages": ocr}
    except Exception as e:
        out = {"error": str(e)[:300]}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))
