"""WhatsApp connector for the KL Tracker dashboard (whatsmeow via neonize - no browser).

Runs in the background on this PC:
  * links as a WhatsApp "linked device" by QR code (shows up as "KL Tracker" on your phone)
  * stores message history your phone sends at link time + new messages, in wa_data/messages.sqlite
  * downloads photos on request (only for chats you scan)
  * sends only the blasts you set up on the dashboard's "WhatsApp blast" page (wa_blast.py) - nothing else,
    and never replies to anyone by itself
Control API on 127.0.0.1:3078, protected by wa_data/.token.

Start it from the dashboard's WhatsApp page (or: python wa_service.py).
"""
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "wa_data"
EXPORT_ROOT = ROOT / "wa_exports"
PORT = 3078

DATA.mkdir(exist_ok=True)
os.chdir(DATA)  # neonize keeps its login session (session.sqlite3) in the working directory

from neonize.client import NewClient  # noqa: E402
from neonize.events import (ConnectedEv, HistorySyncEv, LoggedOutEv, MessageEv,  # noqa: E402
                            PairStatusEv)
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps  # noqa: E402
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as WAMessage  # noqa: E402
import segno  # noqa: E402

TOKEN = secrets.token_hex(24)
(DATA / ".token").write_text(TOKEN)

state = {"status": "starting", "qr": None, "me": None, "error": None,
         "history": {"batches": 0, "messages": 0, "progress": None}}

# ---------- storage ----------
db_lock = threading.Lock()
db = sqlite3.connect(DATA / "messages.sqlite", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS chats (jid TEXT PRIMARY KEY, name TEXT, phone TEXT, is_group INTEGER, last_ts INTEGER);
CREATE TABLE IF NOT EXISTS messages (
    chat_jid TEXT, id TEXT, sender_jid TEXT, sender_name TEXT, sender_phone TEXT, ts INTEGER, from_me INTEGER,
    body TEXT, has_image INTEGER, raw BLOB, PRIMARY KEY (chat_jid, id));
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages (chat_jid, ts);
""")
try:  # added later: id of the message this one replies to (puts a late PDF into the right owner's part)
    db.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT")
except sqlite3.OperationalError:
    pass


def jid_str(j):
    return f"{j.User}@{j.Server}" if j is not None and j.User else ""


def phone_of(jid):
    """Phone number from a normal WhatsApp id (hidden 'lid' ids don't carry one)."""
    user, _, server = (jid or "").partition("@")
    return user.split(":")[0] if server == "s.whatsapp.net" and user.isdigit() else None


PHOTO, PDF, VIDEO, ZIP, VOICE, STICKER = 1, 2, 3, 4, 5, 6  # stored in messages.has_image


def unwrap(m):
    """Return (text, media) from a WhatsApp message - media 0 none / 1 photo / 2 PDF / 3 video / 4 ZIP / 5 voice
    note / 6 sticker - looking inside
    disappearing / view-once / document-with-caption wrappers."""
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage"):
        if m.HasField(wrapper):
            m = getattr(m, wrapper).message
    if m.HasField("imageMessage"):
        return m.imageMessage.caption or "", PHOTO
    if m.HasField("videoMessage"):
        return m.videoMessage.caption or "", VIDEO
    if m.HasField("audioMessage"):  # voice notes (for the WhatsApp chats page - scans skip them)
        return "", VOICE
    if m.HasField("stickerMessage"):
        return "", STICKER
    if m.HasField("documentMessage"):
        d = m.documentMessage
        if d.mimetype == "application/pdf" or (d.fileName or "").lower().endswith(".pdf"):
            return d.caption or d.fileName or "", PDF
        if "zip" in (d.mimetype or "") or (d.fileName or "").lower().endswith(".zip"):
            return d.caption or d.fileName or "", ZIP  # owners' photos / PDFs in one file - opened when scanning
        return None, 0
    if m.conversation:
        return m.conversation, 0
    if m.HasField("extendedTextMessage"):
        return m.extendedTextMessage.text or "", 0
    return None, 0


def reply_to(m):
    """Id of the message this one is a reply to (WhatsApp 'reply'), or None."""
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage"):
        if m.HasField(wrapper):
            m = getattr(m, wrapper).message
    for kind in ("documentMessage", "imageMessage", "videoMessage", "extendedTextMessage"):
        if m.HasField(kind):
            ctx = getattr(m, kind).contextInfo
            return ctx.stanzaID or None if ctx else None
    return None


def save_chat(jid, name=None, phone=None, ts=0):
    if not jid or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return
    with db_lock:
        db.execute("""INSERT INTO chats (jid, name, phone, is_group, last_ts) VALUES (?,?,?,?,?)
                      ON CONFLICT(jid) DO UPDATE SET name=COALESCE(excluded.name, chats.name),
                      phone=COALESCE(excluded.phone, chats.phone), last_ts=MAX(chats.last_ts, excluded.last_ts)""",
                   (jid, name or None, phone or phone_of(jid), int(jid.endswith("@g.us")), int(ts or 0)))


def save_message(chat, mid, sender, sender_name, ts, from_me, msg):
    text, media = unwrap(msg)
    if text is None and not media:
        return False  # stickers, calls, reactions, voice notes, other files... not useful here
    with db_lock:
        db.execute("""INSERT OR IGNORE INTO messages
                      (chat_jid, id, sender_jid, sender_name, sender_phone, ts, from_me, body, has_image, raw, reply_to)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (chat, mid, sender, sender_name, phone_of(sender), int(ts), int(from_me), text or "",
                    media, msg.SerializeToString() if media else None, reply_to(msg)))
    return True


# ---------- WhatsApp client ----------
props = DeviceProps(os="KL Tracker", platformType=DeviceProps.DESKTOP, requireFullSync=True,
                    historySyncConfig=DeviceProps.HistorySyncConfig(fullSyncDaysLimit=365))
client = NewClient("session.sqlite3", props=props)


@client.qr
def on_qr(_, data: bytes):
    state.update(status="qr", qr=segno.make_qr(data).png_data_uri(scale=6, border=2), qr_at=time.time())


def qr_watchdog():
    """WhatsApp rotates the QR every ~20s and gives up after a few minutes. If no fresh code arrives, the shown one
    is dead - exit so the dashboard starts a new connector with a new code (instead of showing an expired QR)."""
    while True:
        time.sleep(5)
        if state["status"] == "qr" and time.time() - state.get("qr_at", time.time()) > 55:
            state.update(status="restarting", qr=None)
            print("QR expired - restarting for a fresh code", flush=True)
            time.sleep(1)
            os._exit(0)


@client.event(PairStatusEv)
def on_pair(_, ev):
    state.update(status="syncing", qr=None)


def load_group_names(c):
    """Store the names of all groups you're in, so they can be picked by name (even before new messages)."""
    try:
        joined = list(c.get_joined_groups())
        print(f"joined groups from WhatsApp: {len(joined)}", flush=True)
        for g in joined:
            save_chat(jid_str(g.JID), g.GroupName.Name or None)
        with db_lock:
            db.commit()
    except Exception as e:
        print("couldn't load group names:", e, flush=True)


named_groups = set()


def ensure_group_name(c, jid):
    if jid in named_groups:
        return
    named_groups.add(jid)
    with db_lock:
        row = db.execute("SELECT name FROM chats WHERE jid=?", (jid,)).fetchone()
    if row and row[0]:
        return
    try:
        info = c.get_group_info(_jid(jid))
        save_chat(jid, info.GroupName.Name or None)
    except Exception:
        pass


def _jid(s):
    from neonize.utils.jid import build_jid
    user, _, server = s.partition("@")
    return build_jid(user, server)


@client.event(ConnectedEv)
def on_connected(c, _):
    me = c.me
    state.update(status="ready", qr=None,
                 me={"number": me.JID.User if me and me.JID else None, "name": me.PushName if me else None})
    threading.Thread(target=load_group_names, args=(c,), daemon=True).start()


@client.event(LoggedOutEv)
def on_logged_out(_, __):
    state.update(status="logged_out", error="Logged out from your phone.")


@client.event(HistorySyncEv)
def on_history(_, ev):
    data, n = ev.Data, 0
    for conv in data.conversations:
        jid = conv.ID
        phone = phone_of(conv.pnJID) if conv.pnJID else None
        save_chat(jid, conv.name or conv.displayName or None, phone, seconds(conv.conversationTimestamp))
        for hm in conv.messages:
            w = hm.message
            sender = w.key.participant or w.participant or (None if w.key.fromMe else jid)
            if save_message(jid, w.key.ID, sender or "", w.pushName or None, seconds(w.messageTimestamp),
                            w.key.fromMe, w.message):
                n += 1
    with db_lock:
        db.commit()
    h = state["history"]
    h["batches"] += 1
    h["messages"] += n
    h["progress"] = data.progress or h["progress"]


def seconds(ts):
    """Live messages carry milliseconds, history carries seconds - store everything as seconds."""
    ts = int(ts or 0)
    return ts // 1000 if ts > 100_000_000_000 else ts


@client.event(MessageEv)
def on_message(c, ev):
    ts = seconds(ev.Info.Timestamp)
    src = ev.Info.MessageSource
    chat, sender = jid_str(src.Chat), jid_str(src.Sender)
    alt = jid_str(src.SenderAlt) if src.HasField("SenderAlt") else ""
    name = None
    if not src.IsGroup and not src.IsFromMe:
        name = ev.Info.Pushname or None
    save_chat(chat, name, phone_of(alt) if alt and not src.IsGroup else None, ts)
    if src.IsGroup:
        threading.Thread(target=ensure_group_name, args=(c, chat), daemon=True).start()
    sender_phone_jid = alt if phone_of(alt) else sender
    if save_message(chat, ev.Info.ID, sender_phone_jid, ev.Info.Pushname or None, ts,
                    src.IsFromMe, ev.Message):
        with db_lock:
            db.commit()


def pn_of(c, jid):
    """Phone number for a chat - WhatsApp often gives only a hidden id ('...@lid'); the session maps it to a phone."""
    s = jid_str(jid)
    if phone_of(s):
        return phone_of(s)
    if not s.endswith("@lid"):
        return None
    try:
        p = phone_of(jid_str(c.get_pn_from_lid(jid)))
        if p:
            return p
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{DATA / 'session.sqlite3'}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (s.split("@")[0],)).fetchone()
        con.close()
        return str(row[0]).split("@")[0].split(":")[0] if row else None
    except Exception:
        return None


AVATARS = DATA / "avatars"


def avatar(jid):
    """A chat's profile picture, kept in wa_data/avatars/ for 3 days -> its file path, or None (no / hidden picture)."""
    import urllib.request
    from neonize.utils import build_jid
    user, _, server = (jid or "").partition("@")
    if not user:
        return None
    AVATARS.mkdir(exist_ok=True)
    f, none = AVATARS / f"{user}.jpg", AVATARS / f"{user}.none"
    for x in (f, none):
        if x.exists() and time.time() - x.stat().st_mtime < 3 * 86400:
            return str(f) if x == f else None
    try:
        info = client.get_profile_picture(build_jid(user, server or "s.whatsapp.net"))
        url = getattr(info, "URL", "") or getattr(info, "url", "")
        if url:
            f.write_bytes(urllib.request.urlopen(url, timeout=20).read())
            none.unlink(missing_ok=True)
            return str(f)
    except Exception:
        pass
    none.write_text("")
    return None


def send_text(to, text):
    """Your reply from the dashboard's WhatsApp chats page -> that chat (a person or a group), saved like the rest."""
    from neonize.utils import build_jid
    user, _, server = (to or "").partition("@")
    if not user or not (text or "").strip():
        return {"ok": False, "error": "nothing to send"}
    jid = build_jid(user, server or "s.whatsapp.net")
    resp = client.send_message(jid, text.strip())
    save_chat(to, None, user if server in ("", "s.whatsapp.net") else None, int(time.time()))
    save_message(to, resp.ID, jid_str(client.me.JID) if client.me else "", None, int(time.time()), True,
                 WAMessage(conversation=text.strip()))
    with db_lock:
        db.commit()
    return {"ok": True, "id": resp.ID}


# ---------- control API ----------
def download(ids, out_dir):
    """ids: [[chat_jid, msg_id], ...] -> {msg_id: filename}. Photos WhatsApp no longer has are skipped."""
    out = Path(out_dir).resolve()
    if EXPORT_ROOT not in out.parents:
        raise ValueError("outDir must be inside wa_exports/")
    out.mkdir(parents=True, exist_ok=True)
    saved = {}
    for chat, mid in ids:
        with db_lock:
            row = db.execute("SELECT raw, has_image FROM messages WHERE chat_jid=? AND id=?", (chat, mid)).fetchone()
        if not row or not row[0]:
            continue
        try:
            data = client.download_any(WAMessage.FromString(row[0]))
            ext = {PDF: 'pdf', VIDEO: 'mp4', ZIP: 'zip', VOICE: 'ogg', STICKER: 'webp'}.get(row[1], 'jpg')
            name = f"{''.join(ch for ch in mid if ch.isalnum())[:40]}.{ext}"
            (out / name).write_bytes(data)
            saved[mid] = name
        except Exception:
            pass  # expired on WhatsApp's servers / deleted
        time.sleep(0.2)  # gentle pace
    return saved


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        if self.headers.get("X-Bridge-Token") != TOKEN:
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._authorised():
            return
        if self.path == "/status":
            return self._send(200, state)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorised():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            if self.path == "/avatar":
                return self._send(200, {"path": avatar(body.get("jid", ""))})
            if self.path == "/send":  # you typed a reply on the dashboard's WhatsApp chats page
                return self._send(200, send_text(body.get("to", ""), body.get("text", "")))
            if self.path == "/download":
                return self._send(200, download(body.get("ids", []), body.get("outDir", "")))
            if self.path == "/logout":
                try:
                    client.logout()
                finally:
                    self._send(200, {"ok": True})
                    threading.Timer(0.5, lambda: os._exit(0)).start()
                return
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=qr_watchdog, daemon=True).start()
    import wa_blast
    threading.Thread(target=wa_blast.worker, args=(client, "main", state), daemon=True).start()
    print(f"wa_service listening on 127.0.0.1:{PORT}", flush=True)
    try:
        client.connect()
    except Exception as e:
        state.update(status="error", error=str(e))
        print("connect failed:", e, flush=True)
        time.sleep(30)  # keep /status answering briefly so the dashboard can show the error
    sys.exit(0)
