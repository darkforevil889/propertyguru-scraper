"""Build WhatsApp listing downloads.

ZIP layout:
  listings.txt / listings.xlsx              - everything in one place (easy to scan through / filter)
  listings/001_The Conlay/description.txt   - one folder per listing: its description...
  listings/001_The Conlay/photo_01.jpg      - ...and its pictures
"""
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd

MYT = timezone(timedelta(hours=8))


def folder_name(i, listing):
    return f"{i:03d}_{re.sub(r'[^A-Za-z0-9 ]+', '', listing.get('place') or 'listing').strip()[:40] or 'listing'}"


def describe(l):
    """Plain-text description of one listing (scan result or Supabase row, see from_row)."""
    money = f"RM {l['price_rm']:,.0f}" if l.get("price_rm") else "-"
    rooms = " ".join(x for x in (f"{l['bedrooms']} bedrooms" if l.get("bedrooms") else "",
                                 f"{l['bathrooms']} bathrooms" if l.get("bathrooms") else "") if x)
    when = datetime.fromtimestamp(l["posted_at"], MYT).strftime("%Y-%m-%d %H:%M") if l.get("posted_at") else "-"
    phones = ", ".join(l.get("phones") or []) or "-"
    size = f"{l['sqft']:,} sqft" if l.get("sqft") else "-"
    if not l.get("price_rm") and l.get("price_psf"):
        money = f"RM {l['price_psf']:,.0f} per sq ft"
    optional = [(label, l.get(key)) for label, key in (("Unit no.", "unit_no"), ("Address", "address"),
                                                         ("Property", "property_type"), ("Tenure", "tenure"))]
    rental = f"RM {l['rental_rm']:,.0f} / month" if l.get("rental_rm") else None
    return "\r\n".join([
        f"Owner:       {l.get('owner_name') or '-'}",
        f"Phone:       {phones}",
        f"Type:        {l.get('type') or '-'}",
        f"Price:       {money}{' / month' if l.get('type') == 'Rent' and l.get('price_rm') else ''}",
        *([f"Rental:      {rental} (current tenant)"] if rental else []),
        f"Location:    {l.get('place') or '-'}",
        *[f"{label + ':':<13}{value}" for label, value in optional if value],
        f"Size:        {size}",
        f"Rooms:       {rooms or '-'}",
        f"Furnishing:  {l.get('furnishing') or '-'}",
        f"Sent by:     {(l.get('sender_name') or '-')} {('(' + l['sender_phone'] + ')') if l.get('sender_phone') else ''}",
        f"Chat:        {l.get('chat') or '-'}",
        f"Posted:      {when}",
        "",
        "Read from a screenshot (please double-check)" if l.get("from_picture") else "",
        "Original message:" if not l.get("from_picture") else "Text read from the picture:",
        (l.get("text") or "").strip(),
        "",
    ])


def from_row(r):
    """Supabase owner_listings row -> the dict shape describe() uses."""
    posted = pd.to_datetime(r.get("posted_at"), utc=True, errors="coerce")
    return {"type": r.get("listing_type"), "price_rm": r.get("price_rm"), "place": r.get("place"),
            "sqft": r.get("sqft"), "bedrooms": r.get("bedrooms"), "bathrooms": r.get("bathrooms"),
            "furnishing": r.get("furnishing"), "phones": [p for p in (r.get("phones") or "").split("; ") if p],
            "sender_name": r.get("sender_name"), "sender_phone": r.get("sender_phone"), "chat": r.get("chat"),
            "posted_at": posted.timestamp() if pd.notna(posted) else None, "text": r.get("message_text"),
            **{k: r.get(k) for k in ("owner_name", "unit_no", "address", "rental_rm", "tenure", "property_type",
                                     "price_psf")}}


def file_name(n, name):
    """photo_01.jpg / document_02.pdf - keeps PDFs as PDFs."""
    return f"document_{n:02d}.pdf" if str(name).lower().endswith(".pdf") else f"photo_{n:02d}.jpg"


def listing_zip(listing, files):
    """One listing: description.txt + its pictures / PDFs (files = list of (original name, bytes))."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("description.txt", describe(listing).encode("utf-8-sig"))
        for n, (name, data) in enumerate(files, 1):
            z.writestr(file_name(n, name), data)
    return buf.getvalue()


def make_zip(listings, media_dir):
    """All scanned listings: listings.txt + listings.xlsx + listings/<nnn>_<place>/(description.txt, photos)."""
    buf, blocks, sheet = io.BytesIO(), [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, l in enumerate(listings, 1):
            folder = f"listings/{folder_name(i, l)}"
            text = describe(l)
            z.writestr(f"{folder}/description.txt", text.encode("utf-8-sig"))
            n = 0
            for p in l["photos"]:
                if (media_dir / p).exists():
                    n += 1
                    z.write(media_dir / p, f"{folder}/{file_name(n, p)}")
            owner = f"owner #{l['owner_no']}, " if l.get("owner_no") else ""
            blocks += [f"#{i}  ({owner}{folder}/, {n} file{'s' if n != 1 else ''})", text, "-" * 60, ""]
            when = datetime.fromtimestamp(l["posted_at"], MYT).strftime("%Y-%m-%d %H:%M")
            sheet.append({"No": i, "Owner #": l.get("owner_no"), "Owner": l.get("owner_name"),
                          "Unit": l.get("unit_no"), "Rental (RM/month)": l.get("rental_rm"),
                          "Price (RM/sqft)": l.get("price_psf"), "Tenure": l.get("tenure"),
                          "Type": l["type"], "Price (RM)": l["price_rm"], "Location": l["place"],
                          "Sqft": l["sqft"], "Bedrooms": l["bedrooms"], "Bathrooms": l["bathrooms"],
                          "Furnishing": l["furnishing"], "Phones": ", ".join(l["phones"]) or l["sender_phone"],
                          "Sender": l["sender_name"], "Chat": l["chat"], "Posted": when, "Photos": n,
                          "Folder": folder, "Message": l["text"]})
        z.writestr("listings.txt", "\r\n".join(blocks).encode("utf-8-sig"))
        xbuf = io.BytesIO()
        pd.DataFrame(sheet).to_excel(xbuf, index=False)
        z.writestr("listings.xlsx", xbuf.getvalue())
    return buf.getvalue()
