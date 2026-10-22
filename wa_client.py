"""Dashboard side of the WhatsApp connector (wa_service.py): start it, check status, read stored messages."""
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "wa_data"
URL = "http://127.0.0.1:3078"


def _call(method, path, body=None, timeout=10):
    try:
        token = (DATA / ".token").read_text().strip()
    except OSError:
        token = ""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Bridge-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def status():
    try:
        return _call("GET", "/status", timeout=3)
    except Exception:
        return None


def ensure_running():
    """Start the connector in the background (no window) if it isn't running. Returns its status."""
    if s := status():
        return s
    try:
        import neonize  # noqa: F401
    except ImportError:
        raise RuntimeError("The WhatsApp connector isn't installed on this computer - "
                           "the WhatsApp listing page only works on the PC it was set up on.")
    (ROOT / "logs").mkdir(exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([sys.executable, str(ROOT / "wa_service.py")], cwd=ROOT, creationflags=flags,
                     stdout=open(ROOT / "logs" / "wa_service.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(30):
        time.sleep(1)
        if s := status():
            return s
    raise RuntimeError("The WhatsApp connector didn't start - see logs/wa_service.log")


def download(ids, out_dir):
    """ids: [(chat_jid, msg_id)] -> {msg_id: filename} for photos that could still be downloaded."""
    return _call("POST", "/download", {"ids": [list(i) for i in ids], "outDir": str(out_dir)}, timeout=3600)


def logout():
    try:
        _call("POST", "/logout", {}, timeout=30)
    except Exception:
        pass


SETTINGS = DATA / "settings.json"


def load_settings():
    """{'my_numbers': [...], 'listing_groups': [...]} kept on this PC."""
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("my_numbers", [])
    s.setdefault("listing_groups", [])
    return s


def save_settings(s):
    DATA.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=1), encoding="utf-8")


def _db():
    return sqlite3.connect(f"file:{DATA / 'messages.sqlite'}?mode=ro", uri=True, timeout=10)


def _contacts():
    """From the WhatsApp session: hidden id ('lid') -> phone, and id -> saved / profile name."""
    lid_phone, names = {}, {}
    try:
        con = sqlite3.connect(f"file:{DATA / 'session.sqlite3'}?mode=ro", uri=True, timeout=10)
        with con:
            for lid, pn in con.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                lid_phone[str(lid).split("@")[0]] = str(pn).split("@")[0].split(":")[0]
            for jid, first, full, push, biz in con.execute(
                    "SELECT their_jid, first_name, full_name, push_name, business_name FROM whatsmeow_contacts"):
                name = full or first or push or biz
                if name:
                    names[jid.split("@")[0]] = name
    except sqlite3.Error:
        pass
    return lid_phone, names


def _resolve(jid, lid_phone, names, stored_phone=None, stored_name=None):
    """(phone, name) for a WhatsApp id, using the session's lid->phone map and contact names."""
    user, _, server = (jid or "").partition("@")
    user = user.split(":")[0]
    phone = stored_phone or (user if server == "s.whatsapp.net" else lid_phone.get(user))
    name = names.get(user) or (names.get(phone) if phone else None) or stored_name
    return phone, name


def chats():
    """Chats with stored text/photo messages, newest first. The same person under a hidden id and a phone
    number is merged into one entry (see 'jids')."""
    if not (DATA / "messages.sqlite").exists():
        return []
    lid_phone, names = _contacts()
    with _db() as con:  # every group you're in; private chats only once they have messages
        rows = con.execute("""
            SELECT c.jid, c.name, c.phone, c.is_group, COUNT(m.id), MAX(m.ts)
            FROM chats c LEFT JOIN messages m ON m.chat_jid = c.jid
            GROUP BY c.jid HAVING c.is_group = 1 OR COUNT(m.id) > 0""").fetchall()
    merged = {}
    for jid, name, phone, is_group, count, last in rows:
        if is_group:
            key, phone = jid, None
            name = name or jid.split("@")[0]
        else:
            phone, name = _resolve(jid, lid_phone, names, phone, name)
            key = phone or jid
        e = merged.setdefault(key, {"jid": key, "jids": [], "name": name, "phone": phone, "is_group": bool(is_group),
                                    "messages": 0, "last_ts": 0})
        e["jids"].append(jid)
        e["messages"] += count
        e["last_ts"] = max(e["last_ts"], last or 0)
        e["name"] = e["name"] or name
    return sorted(merged.values(), key=lambda e: -e["last_ts"])


def messages(chat_jids, since_ts):
    """Stored messages for these chats since a unix time, in the record format wa_parser.group_listings expects."""
    if not chat_jids:
        return []
    lid_phone, names = _contacts()
    marks = ",".join("?" * len(chat_jids))
    with _db() as con:
        chat_info = {j: _resolve(j, lid_phone, names, p, n) for j, n, p in
                     con.execute(f"SELECT jid, name, phone FROM chats WHERE jid IN ({marks})", chat_jids)}
        has_reply = any(c[1] == "reply_to" for c in con.execute("PRAGMA table_info(messages)"))
        rows = con.execute(f"""SELECT chat_jid, id, sender_jid, sender_name, sender_phone, ts, from_me, body, has_image,
                                      {'reply_to' if has_reply else 'NULL'}
                               FROM messages WHERE chat_jid IN ({marks}) AND ts >= ? ORDER BY ts""",
                           [*chat_jids, int(since_ts)]).fetchall()
    out = []
    for chat, mid, sender, sname, sphone, ts, from_me, body, has_image, reply in rows:
        ts = ts // 1000 if ts and ts > 100_000_000_000 else ts  # safety: milliseconds -> seconds
        cphone, cname = chat_info.get(chat, (None, None))
        phone, name = _resolve(sender or chat, lid_phone, names, sphone, sname)
        out.append({"chatId": chat, "id": mid, "author": phone or sender or chat, "authorName": name,
                    "authorNumber": phone, "timestamp": ts, "fromMe": bool(from_me), "body": body or "",
                    "has_image": has_image == 1, "is_pdf": has_image == 2, "is_video": has_image == 3, "photo": None, "reply_to": reply,
                    "chatName": cname or (f"+{cphone}" if cphone else chat.split("@")[0])})
    return out
