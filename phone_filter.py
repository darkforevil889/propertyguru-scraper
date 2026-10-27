"""Extract Malaysian mobile numbers from spreadsheets and normalise them to 0XXXXXXXXX.

Kept:     mobile numbers 010, 012-014, 016-019 (10 digits) and 011 (11 digits)
Fixed:    +60 / 60 / 0060 prefix -> 0;  missing leading 0 (Excel ate it) -> added back
Removed:  landlines (03-..., 04-..., 8-digit 3xxxxxxx), 015 internet/VoIP numbers,
          foreign numbers (+65, +1, ...), anything too short/long, duplicates
"""
import io
import re
from pathlib import Path

import pandas as pd

MOBILE = re.compile(r"^(?:01[02346789]\d{7}|011\d{8})$")
# A run of digits possibly broken up by spaces, dashes, dots or brackets, optionally starting with +
CANDIDATE = re.compile(r"\+?\(?\d[\d \t\-.()]*\d")  # never runs onto the next line
EXCEL_FLOAT = re.compile(r"^\d+\.0+$")
EXCEL_SCI = re.compile(r"^\d(?:\.\d+)?[eE]\+?\d+$")


def normalise(digits, had_plus=False):
    """Return (number, None) if it's a Malaysian mobile, else (None, reason)."""
    if had_plus and not digits.startswith("60"):
        return None, "foreign"
    if digits.startswith("0060"):
        digits = digits[2:]
    if digits.startswith("60") and len(digits) in (11, 12):
        digits = digits[1:]                       # 60123456789 -> 0123456789
    elif digits.startswith("1") and len(digits) in (9, 10):
        digits = "0" + digits                     # 123456789 -> 0123456789
    if MOBILE.match(digits):
        return digits, None
    if digits.startswith("015"):
        return None, "015 internet/VoIP"
    if re.match(r"^0[2-9]", digits) or (len(digits) in (7, 8) and digits[0] in "3456789"):
        return None, "landline"
    if not digits.startswith(("0", "1", "60")) and len(digits) >= 8:
        return None, "foreign"
    return None, "invalid length / not mobile"


def extract_from_text(text):
    """Yield (number_or_None, reason_or_None, raw) for every phone-like thing in one cell."""
    text = str(text).strip()
    if not text or text.lower() == "nan":
        return
    # Numbers Excel stored as numbers: 123456789.0 or 6.0123456789E+10
    if EXCEL_FLOAT.match(text):
        text = text.split(".")[0]
    elif EXCEL_SCI.match(text):
        text = str(int(float(text)))
    for m in CANDIDATE.finditer(text):
        raw = m.group(0)
        had_plus = raw.startswith("+")
        groups = re.findall(r"\d+", raw)
        whole = "".join(groups)
        num, reason = normalise(whole, had_plus)
        if num or len(groups) == 1:
            yield num, reason, raw
            continue
        # Several numbers separated only by spaces/dashes: join groups until a valid number forms
        acc, plus = "", had_plus
        for g in groups:
            acc += g
            num, reason = normalise(acc, plus)
            if num:
                yield num, None, acc
                acc, plus = "", False
        if acc:
            yield None, normalise(acc, plus)[1], acc


def read_tables(filename, data):
    """Read every sheet of an Excel/CSV file as text (so Excel can't mangle numbers further)."""
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return [pd.read_csv(io.BytesIO(data), header=None, dtype=str, keep_default_na=False,
                            encoding_errors="replace", on_bad_lines="skip")]
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    return list(sheets.values())


def cells(filename, data):
    """Every cell of an Excel / CSV file, or every text line of a PDF / picture (read by OCR when scanned)."""
    ext = Path(filename).suffix.lower()
    if ext == ".txt":
        return data.decode("utf-8", "replace").splitlines()
    if ext in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"):
        import phone_ocr  # PDF text / OCR - the OCR part needs the PC's extra packages (requirements-local.txt)
        return phone_ocr.text_lines(filename, data)[0]
    return [v for table in read_tables(filename, data) for v in table.to_numpy().ravel()]


def filter_file(filename, data):
    """Return (numbers in first-seen order, rejected list of (raw, reason), duplicate count)."""
    seen, numbers, rejected, dupes = set(), [], [], 0
    scanned = Path(filename).suffix.lower() not in (".xlsx", ".xls", ".csv", ".txt")
    for value in cells(filename, data):
        for num, reason, raw in extract_from_text(value):
            if num is None:
                if scanned and len(re.sub(r"\D", "", str(raw))) < 6:
                    continue  # row numbers, unit numbers, dates on a printed page - not phone attempts
                rejected.append((raw, reason))
            elif num in seen:
                dupes += 1
            else:
                seen.add(num)
                numbers.append(num)
    return numbers, rejected, dupes
