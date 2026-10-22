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


def status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
    files_msgs = [m for m in msgs if m["has_image"] or m["is_pdf"] or m["is_video"]]
    if read_pictures and files_msgs:
        from wa_ocr import is_chat_screenshot, read_pdf, read_text
        progress(f"Downloading {len(files_msgs)} pictures / PDFs / videos…", 0.05)
        files = wa_client.download([(m["chatId"], m["id"]) for m in files_msgs], run_dir / "media")
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
        # This scan replaces what earlier scans saved for the same chats (old, less accurate results)
        try:
            chats_scanned = list({l["chat"] for l in listings})
            if chats_scanned:
                db.table("owner_listings").delete().in_("chat", chats_scanned) \
                    .not_.in_("message_id", [r["message_id"] for r in rows]).execute()
        except Exception:
            pass  # needs the delete permission from schema_owner_listings_v2.sql
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
