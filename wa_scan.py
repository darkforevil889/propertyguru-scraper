"""Scan WhatsApp chats into owner listings (Supabase owner_listings + files in owner-photos/files/).

Runs on the PC (it needs the WhatsApp connection, the stored messages and the screenshot reader):
  * from the dashboard on the PC: the WhatsApp page calls scan() directly
  * from the online dashboard: the page adds a pc_tasks row; keeper.py runs `python wa_scan.py --task <json>`
    and reports progress in its heartbeat (status()). The chat list is copied up by sync_chats().
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
STATUS = ROOT / "data" / "wa_scan_status.json"
SAVED_IDS = ROOT / "wa_data" / "saved_listing_ids.json"  # every listing a scan saved - to tell what you deleted
BUCKET = "owner-photos"
NEW_COLS = ("owner_name", "unit_no", "address", "rental_rm", "tenure", "property_type", "price_psf", "owner_no")


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):  # Windows refuses while another program is reading the file - try again shortly
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _store(path, obj):
    _save(path, obj)


def status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


ZIP_KINDS = {".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo", ".pdf": "pdf", ".mp4": "video",
             ".mov": "video", ".txt": "text"}
ZIP_MAX_FILES, ZIP_MAX_BYTES = 300, 300 * 1024 * 1024


import re  # noqa: E402

# WhatsApp "Export chat" line: Android "19/10/2026, 6:01 pm - Name: text", iPhone "[19/10/2026, 18:01:05] Name: text"
CHAT_LINE = re.compile(r"^‎?\[?(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}[:.]\d{2}(?:[:.]\d{2})?\s*(?:[ap]\.?\s?m\.?)?)"
                       r"\]?\s*(?:[-–]\s*)?([^:]{1,60}?): (.*)$", re.I)
ATTACHED = re.compile(r"^‎?(?:(.+?\.\w{2,4}) \(file attached\)|<attached: (.+?)>)", re.I)
EXPORT_NAME = re.compile(r"WhatsApp Chat (?:with|-) (.+?)(?:\.txt|\.zip)?$", re.I)


def _digits(s):
    return re.sub(r"\D", "", s or "")


# You introducing yourself ("Hi I'm <name> property agent here…") - that author is you, not the owner
MY_INTRO = re.compile(r"\b(?:i'?m|i am|this is|saya)\b.{0,40}\b(?:property|real estate|hartanah)?\s*(?:agent|"
                      r"negotiator|ejen)\b", re.I)


# A price you quoted in the chat: "around 4-4.3k" (a range -> its top), "Around 800k~", "RM 4,500"
QUOTE_RANGE = re.compile(r"(?:rm\s*)?(\d+(?:\.\d+)?)\s*(k|mil|m)?\s*(?:-|–|~|to)\s*(\d+(?:\.\d+)?)\s*(k|mil|m)\b",
                         re.I)
QUOTE_ONE = re.compile(r"\brm\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|mil|m)?\b|\b(\d+(?:\.\d+)?)\s*(k|mil|m)\b",
                       re.I)
# The owner's answer pushing back on a quoted price
OBJECTION = re.compile(r"\b(no|not|cannot|can'?t|too\s+(low|cheap)|higher|more\s+than|tak|tidak|x\s*boleh)\b", re.I)


def _quoted_price(text):
    def amt(num, unit):
        v = float(num.replace(",", ""))
        return v * {"k": 1e3, "m": 1e6, "mil": 1e6}.get((unit or "").lower(), 1)
    if m := QUOTE_RANGE.search(text or ""):
        top = amt(m.group(3), m.group(4))
        return top if 300 <= top <= 500_000_000 else None
    for m in QUOTE_ONE.finditer(text or ""):
        v = amt(m.group(1), m.group(2)) if m.group(1) else amt(m.group(3), m.group(4))
        if 300 <= v <= 500_000_000:
            return v
    return None


def _agreed_price(lines, party_author):
    """The owner gave no price, but you quoted one and they went on (sent the details) without pushing back - the
    price they're OK with. -> a line for the listing, or None."""
    if not party_author or any(_quoted_price(l["text"]) for l in lines if l["author"] == party_author):
        return None
    for i in range(len(lines) - 1, -1, -1):
        l = lines[i]
        v = _quoted_price(l["text"]) if l["author"] != party_author else None
        if not v:
            continue
        reply = next((x for x in lines[i + 1:] if x["author"] == party_author), None)
        if reply and not OBJECTION.search(reply["text"]):
            return f"Price RM {v:,.0f} (you quoted \"{l['text'].strip()[:60]}\" and the owner went on without objecting)"
        return None  # the last quote was argued with (or never answered) - no price
    return None


def _chat_export(z, infos, media, prefix, original_name=""):
    """A WhatsApp 'Export chat' ZIP (one conversation with an owner) -> (party, items): party = {'name', 'phone'} of
    the person the chat is with; items = their messages and the files THEY sent, in chat order. Your own messages
    (questions, prices you quoted, name card) are left out - they say nothing about the owner's property.
    original_name = the ZIP's name as sent ("WhatsApp Chat - +60 19-622 9148.zip"): the export's own text file is
    often just "_chat.txt" (iPhone), and the copy here is renamed."""
    txt = next(i for i in infos if i.filename.lower().endswith(".txt"))
    who = (EXPORT_NAME.search(Path(txt.filename).name) or EXPORT_NAME.search(original_name or "")
           or EXPORT_NAME.search(Path(z.filename or "").name))
    party_label = re.sub(r"[‪-‮‎‏]", "", who.group(1)).strip() if who else ""
    party = {"phone": _digits(party_label) if len(_digits(party_label)) >= 9 else None,
             "name": None if len(_digits(party_label)) >= 9 else (party_label or None)}
    files = {Path(i.filename).name: i for i in infos}
    lines, cur = [], None
    for raw in z.read(txt).decode("utf-8", "replace").splitlines():
        m = CHAT_LINE.match(raw)
        if m:
            cur = {"author": m.group(3).strip(), "text": m.group(4)}
            lines.append(cur)
        elif cur is not None:  # a message over several lines
            cur["text"] += "\n" + raw
    authors = list(dict.fromkeys(l["author"] for l in lines))
    me = {l["author"] for l in lines if MY_INTRO.search(l["text"])}

    def find_party():
        """Which author is the owner: the number / name from the file name, else (iPhone) the author of the
        "end-to-end encrypted" line - it's written under the other person's name - else a "~Name" (not in your
        contacts), else whoever isn't the one introducing themselves as an agent."""
        for a in authors:
            if party["phone"] and _digits(a).endswith(party["phone"][-9:]):
                return a
            if party["name"] and a.lstrip("~").strip().lower() == party["name"].lstrip("~").strip().lower():
                return a
        notice = next((l["author"] for l in lines if "end-to-end encrypted" in l["text"]), None)
        others = [a for a in authors if a not in me]
        for pick in ([notice] if notice and notice not in me else []) + [a for a in others if a.startswith("~")]:
            return pick
        if len(authors) == 2 and len(others) == 1:
            return others[0]
        if len(others) == 2:  # nothing else to go on: the one who wrote most
            return sorted(others, key=lambda a: -sum(1 for l in lines if l["author"] == a))[0]
        return None

    party_author = find_party()
    if party_author and not party["name"] and not _digits(party_author):
        party["name"] = party_author.lstrip("~").strip()

    def is_party(a):
        return a == party_author

    items, total = [], 0
    for l in lines:
        if not is_party(l["author"]):
            continue
        att = ATTACHED.match(l["text"].strip())
        if att:
            fname = (att.group(1) or att.group(2)).strip()
            info = files.get(fname)
            kind = ZIP_KINDS.get(Path(fname).suffix.lower()) if info else None
            if kind and kind != "text":
                total += info.file_size
                if total > ZIP_MAX_BYTES or len(items) >= ZIP_MAX_FILES:
                    break
                clean = re.sub(r"[^\w.-]", "_", fname)[-60:]
                out_name = f"{prefix}_{len(items)}_{clean}"
                (media / out_name).write_bytes(z.read(info))
                items.append((kind, out_name, ""))
            continue
        if "end-to-end encrypted" not in l["text"] and l["text"].strip() not in ("<Media omitted>", "null"):
            # "Hi, <your name>" - the owner greeting YOU: that name isn't the owner's
            text = re.sub(r"^(\s*(hi|hello|hey|hai|dear|morning|good\s+\w+)\b[\s,!.]*)[A-Z][a-z]+\b", r"\1", l["text"],
                          flags=re.I)
            items.append(("text", None, text))
    agreed = _agreed_price(lines, party_author)
    if agreed:  # the owner's words and the price read together as one message, so the listing gets both
        said = [t for k, f, t in items if k == "text"]
        items = [("text", None, "\n".join(said + [agreed]))] + [x for x in items if x[0] != "text"]
    return party, items


def _unzip(path, media, mid, original_name=""):
    """A ZIP from a chat -> (party, [(kind, file name in media/ or None, text)]) for its photos / PDFs / videos /
    .txt. party is set when it's a WhatsApp chat export (see _chat_export). Files are written flat into media/
    under new names (never outside it); other file types are skipped."""
    import zipfile
    prefix, out, total = re.sub(r"\W", "", mid)[:30], [], 0
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
            txts = [i for i in infos if i.filename.lower().endswith(".txt")]
            if len(txts) == 1:
                head = z.read(txts[0])[:3000].decode("utf-8", "replace")
                if sum(1 for line in head.splitlines() if CHAT_LINE.match(line)) >= 2:
                    return _chat_export(z, infos, media, prefix, original_name)
            for info in sorted(infos, key=lambda i: i.filename):
                name = Path(info.filename).name
                kind = ZIP_KINDS.get(Path(name).suffix.lower())
                if not kind or name.startswith("."):
                    continue
                total += info.file_size
                if total > ZIP_MAX_BYTES or len(out) >= ZIP_MAX_FILES:
                    break
                if kind == "text":
                    out.append(("text", None, z.read(info).decode("utf-8", "replace")[:20000]))
                    continue
                clean = re.sub(r"[^\w.-]", "_", name)[-60:]
                fname = f"{prefix}_{len(out)}_{clean}"
                (media / fname).write_bytes(z.read(info))
                out.append((kind, fname, ""))
    except (zipfile.BadZipFile, OSError):
        return None, []
    return None, out


def expand_zips(msgs, got, media):
    """Replace each ZIP message by one message per file inside it (same sender and time, so a ZIP posted
    between two '.' separators belongs to that owner). A WhatsApp chat export is one owner of its own: it is
    wrapped in '.' separators and starts with the owner's number / name from the file name.
    Photos / videos / PDFs the same person posts right after a chat export (within 3 minutes, nothing else in
    between - e.g. the owner's unit photos) belong to that owner too.
    A ZIP that can't be opened stays as its caption."""
    out, closing = [], None  # closing = (the export's ZIP message, its end separator) while it can still take photos
    for m in msgs:
        if closing:
            z, end = closing
            if (m.get("has_image") or m.get("is_video") or m.get("is_pdf")) and not m.get("is_zip") \
                    and not (m.get("body") or "").strip() and m.get("author") == z.get("author") \
                    and 0 <= (m.get("timestamp") or 0) - (z.get("timestamp") or 0) <= 180:
                out.append(m)  # the owner's photos, posted right after their chat
                end["timestamp"] = m["timestamp"]  # owners' parts are sorted by time: the end stays after them
                continue
            out.append(end)
            closing = None
        if not m.get("is_zip"):
            out.append(m)
            continue
        party, inner = (_unzip(media / got[m["id"]], media, m["id"], m.get("body") or "") if m["id"] in got
                        else (None, []))
        caption = (m.get("body") or "").strip()
        if (caption and not caption.lower().endswith(".zip") and not party) or not inner:
            out.append({**m, "is_zip": False})  # its caption (or just the file name if it couldn't be opened)
            if not inner:
                continue
        plain = {**m, "is_zip": False, "has_image": False, "is_pdf": False, "is_video": False, "photo": None}
        if party:
            out.append({**plain, "id": f"{m['id']}-s0", "body": "."})
            intro = " ".join(x for x in (f"This is {party['name']}" if party["name"] else "",
                                         f"+{party['phone']}" if party["phone"] else "") if x)
            if intro:
                out.append({**plain, "id": f"{m['id']}-who", "body": intro})
        for i, (kind, fname, text) in enumerate(inner):
            out.append({**plain, "id": f"{m['id']}-z{i}", "body": text or "", "photo": fname,
                        "has_image": kind == "photo", "is_pdf": kind == "pdf", "is_video": kind == "video"})
        if party:
            closing = (m, {**plain, "id": f"{m['id']}-s1", "body": "."})
    if closing:
        out.append(closing[1])
    return out


def merge_repeats(listings, media):
    """The same owner's same property sent twice (e.g. a chat exported and sent again) -> one listing, the first
    one sent, with every file from both - a file that's in both (the same video) only once."""
    import hashlib
    from wa_parser import EXTRA_FIELDS, _same_property

    def digest(p):
        f = media / p
        return hashlib.sha1(f.read_bytes()).hexdigest() if f.exists() else p

    out = []
    for l in sorted(listings, key=lambda l: l["posted_at"]):
        phones = set(l.get("phones") or [])
        same = next((o for o in out if phones & set(o.get("phones") or []) and _same_property(o, l)), None)
        if not same:
            out.append(l)
            continue
        have = {digest(p) for p in same["photos"]}
        for p in l["photos"]:
            if digest(p) not in have:
                same["photos"].append(p)
                have.add(digest(p))
        for k in EXTRA_FIELDS + ("owner_name",):
            if not same.get(k) and l.get(k):
                same[k] = l[k]
    return out


def scan(db, picked, days, read_pictures, scanned_by, progress=None):
    """Scan the picked chats (keys from wa_client.chats()) -> {'listings', 'media', 'messages', 'saved'}.
    progress(text, fraction or None) is called along the way. saved = True, or a short note / error."""
    import wa_client
    from phone_filter import extract_from_text
    from wa_parser import group_by_owner, group_listings, uses_separators
    progress = progress or (lambda text, frac=None: None)

    me = (wa_client.status() or {}).get("me") or {}
    settings = wa_client.load_settings()
    mine = set(settings["my_numbers"]) | {n for n, _, _ in extract_from_text(me.get("number") or "") if n}
    by_key = {c["jid"]: c["jids"] for c in wa_client.chats()}
    run_dir = ROOT / "wa_exports" / datetime.now().strftime("%Y%m%d_%H%M%S")

    progress("Reading messages…", 0.02)
    msgs = wa_client.messages([j for k in picked for j in by_key.get(k, [k])], time.time() - days * 86400)
    zips = [m for m in msgs if m.get("is_zip")]
    if zips:  # owners' photos / PDFs sent as one ZIP: open it, its files count as posted where the ZIP was
        progress(f"Opening {len(zips)} ZIP file(s)…", 0.03)
        got = wa_client.download([(m["chatId"], m["id"]) for m in zips], run_dir / "media")
        msgs = expand_zips(msgs, got, run_dir / "media")
    files_msgs = [m for m in msgs if m["has_image"] or m["is_pdf"] or m["is_video"]]
    if read_pictures and files_msgs:
        from wa_ocr import is_chat_screenshot, read_pdf, read_text
        progress(f"Downloading {len(files_msgs)} pictures / PDFs / videos…", 0.05)
        files = wa_client.download([(m["chatId"], m["id"]) for m in files_msgs if not m.get("photo")],
                                   run_dir / "media")
        for m in files_msgs:
            if m["id"] in files:
                m["photo"] = files[m["id"]]
        # Read WhatsApp screenshots and PDFs; ordinary photos are skipped (they're only attached)
        to_read = [m for m in files_msgs if m.get("photo") and
                   (m["is_pdf"] or is_chat_screenshot(run_dir / "media" / m["photo"]))]
        skipped = sum(1 for m in files_msgs if m.get("photo")) - len(to_read)
        for i, m in enumerate(to_read, 1):
            progress(f"Reading screenshots / PDFs… {i}/{len(to_read)} ({skipped} ordinary photos skipped)",
                     0.1 + 0.6 * i / len(to_read))
            path = run_dir / "media" / m["photo"]
            if m["is_pdf"]:
                m["pdf_text"] = read_pdf(path)
            else:
                m["ocr_text"] = read_text(path)
    # Chats where you separate owners with a "." message are grouped per owner; others per message
    by_chat = {}
    for m in msgs:
        by_chat.setdefault(m["chatId"], []).append(m)
    listings = []
    for chat_msgs in by_chat.values():
        listings += (group_by_owner(chat_msgs, mine, settings.get("owner_overrides")) if uses_separators(chat_msgs)
                     else group_listings(chat_msgs))
    refs = [p for l in listings for p in l["photos"] if isinstance(p, tuple)]
    if refs:
        progress(f"Downloading {len(refs)} photos…", 0.72)
        files = wa_client.download(refs, run_dir / "media")
        for l in listings:
            l["photos"] = [p if isinstance(p, str) else files[p[1]] for p in l["photos"]
                           if isinstance(p, str) or p[1] in files]
    listings = merge_repeats(listings, run_dir / "media")
    for l in listings:  # sender's number in local 01x format when possible
        nums = [n for n, _, _ in extract_from_text(l["sender_phone"] or "") if n]
        l["sender_phone"] = nums[0] if nums else l["sender_phone"]
        # Your own numbers are never an owner's: drop them from the message's numbers / the sender
        l["phones"] = [p for p in l["phones"] if p not in mine]
        if l["sender_phone"] in mine:
            l["sender_phone"], l["sender_name"] = None, "you"
    result = {"listings": listings, "media": str(run_dir / "media"), "messages": len(msgs), "saved": None}

    # Upload each file ONCE to the private bucket (owner-photos/files/<file>); listings just refer to it.
    # Supabase's free plan allows up to 50 MB per file - bigger videos stay in the ZIP only.
    uploaded, upload_error, too_big = {}, None, 0
    needed = list(dict.fromkeys(p for l in listings for p in l["photos"]))
    for i, p in enumerate(needed, 1):
        progress(f"Uploading pictures / PDFs / videos… {i}/{len(needed)}", 0.75 + 0.2 * i / len(needed))
        src = run_dir / "media" / p
        if not src.exists():
            continue
        if src.stat().st_size > 50 * 1024 * 1024:
            too_big += 1
            continue
        ctype = {"pdf": "application/pdf", "mp4": "video/mp4"}.get(p.rsplit(".", 1)[-1].lower(), "image/jpeg")
        try:
            db.storage.from_(BUCKET).upload(f"files/{p}", src.read_bytes(), {"content-type": ctype, "upsert": "true"})
            uploaded[p] = f"files/{p}"
        except Exception as e:
            upload_error = upload_error or str(e).splitlines()[0]
    # Save to Supabase (re-scans update the same rows instead of duplicating)
    progress("Saving listings…", 0.97)
    rows = [{"message_id": l["id"], "chat": l["chat"],
             "photo_paths": [uploaded[p] for p in l["photos"] if p in uploaded],
             "posted_at": datetime.fromtimestamp(l["posted_at"], timezone.utc).isoformat(),
             "sender_name": l["sender_name"], "sender_phone": l["sender_phone"], "listing_type": l["type"],
             "price_rm": l["price_rm"], "sqft": l["sqft"], "bedrooms": l["bedrooms"],
             "bathrooms": l["bathrooms"], "furnishing": l["furnishing"], "place": l["place"],
             "phones": "; ".join(l["phones"]), "photo_count": len(l["photos"]), "message_text": l["text"],
             "scanned_by": scanned_by,
             # added later (schema_owner_listings_v2.sql)
             "owner_name": l.get("owner_name"), "unit_no": l.get("unit_no"), "address": l.get("address"),
             "rental_rm": l.get("rental_rm"), "tenure": l.get("tenure"), "property_type": l.get("property_type"),
             "price_psf": l.get("price_psf"), "owner_no": l.get("owner_no")} for l in listings]
    notes = [f"{too_big} video(s) over 50 MB kept in the ZIP only"] if too_big else []
    chats_scanned = list({l["chat"] for l in listings})
    # Listings you deleted in Saved listings stay deleted: anything a scan saved before that is gone now was
    # deleted by you, so it is not saved again
    known = set(_load(SAVED_IDS, []))
    existing = set()
    for i in range(0, len(chats_scanned), 50):
        existing |= {r["message_id"] for r in db.table("owner_listings").select("message_id")
                     .in_("chat", chats_scanned[i:i + 50]).execute().data}
    you_deleted = known - existing
    skipped = [r for r in rows if r["message_id"] in you_deleted]
    rows = [r for r in rows if r["message_id"] not in you_deleted]
    result["listings"] = [l for l in listings if l["id"] not in you_deleted]
    try:
        try:
            for i in range(0, len(rows), 500):
                db.table("owner_listings").upsert(rows[i:i + 500], on_conflict="message_id").execute()
        except Exception as e:
            if "column" not in str(e).lower():
                raise
            # Online table not updated yet: save without the new fields, and say so
            for i in range(0, len(rows), 500):
                db.table("owner_listings").upsert([{k: v for k, v in r.items() if k not in NEW_COLS}
                                                   for r in rows[i:i + 500]], on_conflict="message_id").execute()
            notes.append("owner name / unit / rental not saved online yet - run schema_owner_listings_v2.sql")
        # This scan replaces what earlier scans saved for the same chats and the same days (old, less accurate
        # results) - listings from before the scanned days are left alone
        new_ids = [r["message_id"] for r in rows]
        replaced = set()
        try:
            if chats_scanned:
                since = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat()
                old = db.table("owner_listings").select("message_id").in_("chat", chats_scanned) \
                    .gte("posted_at", since).not_.in_("message_id", new_ids).execute().data
                replaced = {r["message_id"] for r in old}
                for i in range(0, len(old), 200):
                    db.table("owner_listings").delete().in_("message_id", [r["message_id"] for r in old[i:i + 200]]) \
                        .execute()
        except Exception:
            pass  # needs the delete permission from schema_owner_listings_v2.sql
        _store(SAVED_IDS, sorted((known - replaced) | set(new_ids)))
        if skipped:
            notes.append(f"{len(skipped)} listing(s) you deleted before were left out")
        try:  # furnishing from the text, else from photos (free AI) - keeper.py catches up on any left over
            import furnish_check
            progress("Checking furnishing…", 0.99)
            furnish_check.run(db, only_ids=new_ids, log=lambda m: None)
        except Exception:
            pass
        if upload_error:
            notes.append(f"some files not uploaded ({upload_error})")
        result["saved"] = True if not notes else "; ".join(notes)
    except Exception as e:
        result["saved"] = str(e).splitlines()[0]
    return result


# ---------- online dashboard <-> PC (keeper.py) ----------
def sync_chats(db):
    """Copy the WhatsApp connection status, chat list and scan settings up for the online WhatsApp page."""
    import wa_client
    s = wa_client.status() or {}
    chats = wa_client.chats() if s.get("status") in ("ready", "syncing") else []
    keep = ("jid", "jids", "name", "phone", "is_group", "messages")
    data = {"at": time.time(), "status": s.get("status") or "off", "me": s.get("me") or {},
            "history": s.get("history") or {}, "settings": wa_client.load_settings(),
            "chats": [{k: c.get(k) for k in keep} for c in chats]}
    db.table("pc_status").upsert({"name": "wa_chats", "data": data}).execute()


def remote_chats(db):
    """What sync_chats() copied up: {'status', 'me', 'history', 'settings', 'chats', 'at'} or {}."""
    rows = db.table("pc_status").select("data").eq("name", "wa_chats").execute().data
    return rows[0]["data"] if rows else {}


def status_remote(db):
    """Scan progress as the PC last reported it (heartbeat in pc_status)."""
    rows = db.table("pc_status").select("data").eq("name", "pc").execute().data
    return ((rows[0]["data"] if rows else {}) or {}).get("wa_scan") or {}


def request(db, kind, params):
    """kind 'wa_scan' {chats, days, read_pictures, by} or 'wa_settings' {my_numbers, listing_groups}."""
    db.table("pc_tasks").insert({"kind": kind, "params": params}).execute()


def pending(db):
    return db.table("pc_tasks").select("id,kind,params").in_("kind", ["wa_scan", "wa_settings"]) \
        .eq("status", "pending").order("id").execute().data


def run_requests(db, log=print):
    """Apply settings changed online; start scans asked for online (one at a time)."""
    import wa_client
    for task in pending(db):
        p = task.get("params") or {}
        if task["kind"] == "wa_settings":
            s = wa_client.load_settings()
            s.update({k: p[k] for k in ("my_numbers", "listing_groups") if k in p})
            wa_client.save_settings(s)
            log("whatsapp settings changed from the online page")
        else:
            st = status()
            if st.get("state") == "running" and time.time() - st.get("at", 0) < 900:
                return  # one scan at a time - this request waits
            _save(STATUS, {"state": "running", "at": time.time(), "text": "Starting…", "frac": 0})
            (ROOT / "logs").mkdir(exist_ok=True)
            subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-u", str(ROOT / "wa_scan.py"),
                              "--task", json.dumps(p)], cwd=ROOT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=open(ROOT / "logs" / "wa_scan.log", "a", encoding="utf-8"),
                             stderr=subprocess.STDOUT)
            log(f"whatsapp scan started from the online page: {len(p.get('chats') or [])} chat(s)")
        db.table("pc_tasks").update({"status": "started"}).eq("id", task["id"]).execute()


if __name__ == "__main__":  # started by keeper.py for a scan asked for on the online dashboard
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    p = json.loads(ap.parse_args().task)
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(ROOT / ".env")
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    def report(text, frac=None):
        _save(STATUS, {"state": "running", "at": time.time(), "text": text, "frac": frac or 0})

    try:
        res = scan(db, p.get("chats") or [], int(p.get("days") or 60), bool(p.get("read_pictures", True)),
                   p.get("by") or "online dashboard", report)
        _save(STATUS, {"state": "done", "at": time.time(), "messages": res["messages"],
                       "listings": len(res["listings"]),
                       "photos": sum(len(l["photos"]) for l in res["listings"]), "saved": res["saved"]})
    except Exception as e:
        _save(STATUS, {"state": "error", "at": time.time(), "error": str(e)[:300]})
        raise
