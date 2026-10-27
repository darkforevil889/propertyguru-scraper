"""Phone filter on PDFs / photos: makes a scanned PDF, a phone-camera style photo and a normal (text) PDF of a
phone list, runs phone_filter.filter_file on each and checks every number. Run: .venv/Scripts/python tests_phone_ocr.py"""
import io
import random
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from phone_filter import filter_file

ROWS = [("TAN AH KOW", "012-345 6789"), ("SITI AMINAH", "+60 17-888 9999"), ("RAJ KUMAR", "019 559 1500"),
        ("LIM WEI MING", "60111234 5678"), ("NG SIEW LAN", "O16-777 2222"),  # letter O typed / printed for zero
        ("OFFICE", "03-7788 9900"),  # landline - dropped
        ("TAN AH KOW (again)", "0123456789"),  # duplicate
        ("WONG MEI LING", "013 9998 887"), ("ALI BIN ABU", "+6014-555 1212"), ("LEE CHONG", "018-2891700")]
WANT = ["0123456789", "0178889999", "0195591500", "01112345678", "0167772222", "0139998887", "0145551212", "0182891700"]
FONT = "C:/Windows/Fonts/arial.ttf"


def table_image(scale=1.0, noise=False):
    f = ImageFont.truetype(FONT, int(30 * scale))
    w, h = int(1650 * scale), int((220 + 70 * len(ROWS)) * scale)
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((int(90 * scale), int(60 * scale)), "OWNER CONTACT LIST - KAJANG", font=ImageFont.truetype(FONT, int(40 * scale)), fill="black")
    for i, (name, phone) in enumerate(ROWS):
        y = int((160 + 70 * i) * scale)
        d.text((int(90 * scale), y), f"{i + 1}.", font=f, fill="black")
        d.text((int(170 * scale), y), name, font=f, fill="black")
        d.text((int(900 * scale), y), phone, font=f, fill="black")
        d.line((int(90 * scale), y + int(52 * scale), w - int(90 * scale), y + int(52 * scale)), fill=(190, 190, 190), width=2)
    if noise:  # phone photo: slight tilt, blur, grey paper
        img = img.rotate(1.2, expand=True, fillcolor=(235, 232, 225)).filter(ImageFilter.GaussianBlur(0.8))
        px = img.load()
        for _ in range(4000):
            x, y = random.randrange(img.width), random.randrange(img.height)
            px[x, y] = (170, 170, 170)
    return img


def check(label, name, data):
    nums, rejected, dupes = filter_file(name, data)
    ok = nums == WANT
    print(("OK  " if ok else "BAD ") + f"{label}: {len(nums)} numbers, {dupes} dupes, rejected {[r[0] for r in rejected][:4]}")
    if not ok:
        print("    got ", nums)
        print("    want", WANT)
    return ok


results = []
buf = io.BytesIO()
table_image(1.0).save(buf, "PDF", resolution=150)  # image-only PDF = a scan
results.append(check("scanned PDF", "scan.pdf", buf.getvalue()))

buf = io.BytesIO()
table_image(0.7, noise=True).save(buf, "JPEG", quality=80)
results.append(check("phone photo", "photo.jpg", buf.getvalue()))

# a normal PDF with real text (printed from a web page by Chrome)
from patchright.sync_api import sync_playwright
html = "<h2>Owner contact list</h2><table>" + "".join(f"<tr><td>{n}</td><td>{p}</td></tr>" for n, p in ROWS) + "</table>"
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page()
    pg.set_content(html)
    pdf = pg.pdf()
    b.close()
results.append(check("text PDF", "list.pdf", pdf.replace(b"", b"")))

print("problems:", "none" if all(results) else f"{results.count(False)} failed")
