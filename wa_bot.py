"""Your personal WhatsApp assistant (assistant.py) - runs on this PC.

* links by QR as a linked device ("KL Tracker Bot") - its own session in wa_bot_data/
* reads and answers ONLY your own number (assistant.py "number"); everyone else's messages are ignored, never
  read, kept or answered
* smart reply by approval (reply_approval.py): a customer messages your main number -> you get the smart reply
  here first, and it's sent to them only when you answer "ok"
* also sends blasts set to go "from the bot's number" (wa_blast.py)
Control API on 127.0.0.1:3079, protected by wa_bot_data/.token. Linked from the dashboard's "AI Chat bot" page.
"""
import json
import random
import re
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "wa_bot_data"
PORT = 3079
DATA.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
os.chdir(DATA)  # the bot's own WhatsApp session file lives here

from neonize.client import NewClient  # noqa: E402
from neonize.events import ConnectedEv, LoggedOutEv, MessageEv, PairStatusEv  # noqa: E402
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps  # noqa: E402
from neonize.utils.enum import ChatPresence, ChatPresenceMedia  # noqa: E402
import segno  # noqa: E402

import assistant  # noqa: E402

TOKEN = secrets.token_hex(24)  # written to .token only once the port is ours - a second copy mustn't overwrite it
STARTED = time.time()
state = {"status": "starting", "qr": None, "me": None, "error": None}

db_lock = threading.Lock()
db = sqlite3.connect(DATA / "bot.sqlite", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS log (chat TEXT, name TEXT, role TEXT, text TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS log_chat ON log (chat, ts);
""")


def log(chat, name, role, text):
    with db_lock:
        db.execute("INSERT INTO log VALUES (?,?,?,?,?)", (chat, name, role, text, int(time.time())))
        db.commit()


def history(chat, limit=14):
    with db_lock:
        rows = db.execute("SELECT role, text FROM log WHERE chat=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                          (chat, limit)).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(rows)]


def jid_str(j):
    return f"{j.User}@{j.Server}" if j is not None and j.User else ""


# ---------- WhatsApp client ----------
props = DeviceProps(os="KL Tracker Bot", platformType=DeviceProps.DESKTOP)
client = NewClient("bot_session.sqlite3", props=props)


@client.qr
def on_qr(_, data: bytes):
    state.update(status="qr", qr=segno.make_qr(data).png_data_uri(scale=6, border=2), qr_at=time.time())


def qr_watchdog():
    while True:
        time.sleep(5)
        if state["status"] == "qr" and time.time() - state.get("qr_at", time.time()) > 55:
            os._exit(0)  # dashboard restarts the bot with a fresh code


@client.event(PairStatusEv)
def on_pair(_, __):
    state.update(status="connecting", qr=None)


@client.event(ConnectedEv)
def on_connected(c, _):
    me = c.me
    state.update(status="ready", qr=None,
                 me={"number": me.JID.User if me and me.JID else None, "name": me.PushName if me else None})


@client.event(LoggedOutEv)
def on_logged_out(_, __):
    state.update(status="logged_out", error="Logged out from the phone.")


def lid_phone(c, jid, jid_s):
    """WhatsApp often gives only a hidden id ('...@lid') - the session maps it to the phone number."""
    if not jid_s.endswith("@lid"):
        return None
    try:
        p = jid_str(c.get_pn_from_lid(jid))
        if p.endswith("@s.whatsapp.net"):
            return p.split("@")[0].split(":")[0]
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{DATA / 'bot_session.sqlite3'}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (jid_s.split("@")[0],)).fetchone()
        con.close()
        return str(row[0]).split("@")[0].split(":")[0] if row else None
    except Exception:
        return None


def text_of(m):
    text = m.conversation or (m.extendedTextMessage.text if m.HasField("extendedTextMessage") else "")
    if not text and m.HasField("interactiveResponseMessage"):  # they tapped a quick-reply button from a blast
        text = m.interactiveResponseMessage.body.text
    if not text and m.HasField("buttonsResponseMessage"):
        text = m.buttonsResponseMessage.selectedDisplayText
    return text


@client.event(MessageEv)
def on_message(c, ev):
    src = ev.Info.MessageSource
    chat = jid_str(src.Chat)
    if src.IsFromMe or src.IsGroup or chat.endswith(("@broadcast", "@newsletter", "@g.us")) or not chat:
        return
    ts = int(ev.Info.Timestamp or 0)
    ts = ts // 1000 if ts > 100_000_000_000 else ts
    if ts and ts < time.time() - 3600:
        return  # old backlog after being offline long - don't answer stale messages
    phone_jid = jid_str(src.SenderAlt) if src.HasField("SenderAlt") else chat
    phone = phone_jid.split("@")[0] if phone_jid.endswith("@s.whatsapp.net") else lid_phone(c, src.Chat, chat)
    if not is_owner_chat(chat, phone):
        return  # only you - anyone else's message is ignored (not read, not kept, not answered)
    m = ev.Message
    for w in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        if m.HasField(w):
            m = getattr(m, w).message
    name = ev.Info.Pushname or None
    info = {"jid": src.Chat, "name": name, "phone": phone}
    if m.HasField("stickerMessage"):  # your stickers: kept for the assistant to use
        try:
            save_sticker(c.download_any(m))
            log(assistant.HISTORY_KEY, name, "user", "[sent a sticker]")
            if random.random() < 0.5:  # sometimes a sticker back
                threading.Thread(target=send_sticker, args=(src.Chat,), daemon=True).start()
        except Exception as e:
            print("sticker:", str(e)[:200], flush=True)
        return
    if m.HasField("imageMessage"):  # your photo / screenshot: the assistant looks at it
        try:
            folder = ROOT / "data" / "assistant_photos"  # in the project, so a change request can show it to Claude Code
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{ev.Info.ID[:10]}.jpg"
            path.write_bytes(c.download_any(m))
            log(assistant.HISTORY_KEY, name, "user",
                f"[sent a photo: {path.relative_to(ROOT).as_posix()}] {m.imageMessage.caption or ''}".strip())
            threading.Thread(target=assistant_reply, args=(info, 4), daemon=True).start()  # several photos: wait a bit
        except Exception as e:
            print("owner photo:", str(e)[:200], flush=True)
        return
    text = text_of(m)
    if not text.strip():
        return
    threading.Thread(target=owner_text, args=(info, name, text.strip()), daemon=True).start()


def owner_text(info, name, text):
    """Your "ok" / "no" for a waiting smart reply (reply_approval.py) - or anything else, for the assistant."""
    import reply_approval
    try:
        said = reply_approval.answer(text)
    except Exception as e:
        said = f"Couldn't do that: {str(e)[:150]}"
    log(assistant.HISTORY_KEY, name, "user", text)
    if said is None:
        assistant_reply(info)
    elif said:  # "" = sent, nothing to say
        send_to_owner(said, info["jid"])


# ---------- your personal assistant (assistant.py): only your own number ----------
assistant_lock = threading.Lock()  # one answer at a time


OWNER_CHATS = DATA / "owner_chats.json"  # the ids WhatsApp uses for your number (often a hidden '...@lid')


def is_owner_chat(chat, phone):
    """Your own number? By phone - or by a chat id already known to be yours (the phone isn't always given)."""
    try:
        known = set(json.loads(OWNER_CHATS.read_text()))
    except (OSError, ValueError):
        known = set()
    if assistant.is_owner(phone):
        if chat not in known:
            OWNER_CHATS.write_text(json.dumps(sorted(known | {chat})))
        return True
    if phone and chat in known:  # the number IS known and it isn't yours: a wrongly saved id - forget it (once let Ben in)
        OWNER_CHATS.write_text(json.dumps(sorted(known - {chat})))
        return False
    return chat in known and not phone   # only when WhatsApp didn't say whose number it is


def send_photos(jid, photos):
    """Pictures the assistant found (listing photos) -> you, one by one."""
    for data, caption in photos:
        try:
            time.sleep(random.uniform(1, 2.5))
            client.send_image(jid, data, caption=caption or None)
        except Exception as e:
            print("send photo:", str(e)[:200], flush=True)


STICKERS = DATA / "stickers"  # the stickers you sent the bot - it uses them instead of emojis


def save_sticker(data):
    import hashlib
    STICKERS.mkdir(exist_ok=True)
    (STICKERS / f"{hashlib.sha1(data).hexdigest()[:16]}.webp").write_bytes(data)  # each sticker once


def send_sticker(jid):
    files = list(STICKERS.glob("*.webp")) if STICKERS.exists() else []
    if files:
        time.sleep(random.uniform(1.5, 4))
        try:
            client.send_sticker(jid, random.choice(files).read_bytes())
        except Exception as e:
            print("send sticker:", str(e)[:200], flush=True)


def send_to_owner(text, jid=None, sticker=False):
    """A message from the assistant to you - several thoughts = several bubbles, with typing pauses; sticker=True
    adds one of your own stickers after it."""
    from neonize.utils import build_jid
    jid = jid or build_jid(re.sub(r"\D", "", assistant.load().get("number") or ""))
    for i, part in enumerate(p.strip() for p in text.split("\n\n") if p.strip()):
        try:
            client.send_chat_presence(jid, ChatPresence.CHAT_PRESENCE_COMPOSING,
                                      ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception:
            pass
        time.sleep(min(2, 0.5 + len(part) / 100))  # a short typing moment - you want answers fast
        client.send_message(jid, part)
    log(assistant.HISTORY_KEY, "assistant", "assistant", text)
    if sticker:
        send_sticker(jid)


def send_file_to_owner(f, caption=""):
    """A file from this PC -> you, as a document (keeps full quality; WhatsApp allows documents up to 2 GB)."""
    import mimetypes
    from neonize.utils import build_jid
    jid = build_jid(re.sub(r"\D", "", assistant.load().get("number") or ""))
    try:
        client.send_document(jid, f.read_bytes(), caption=caption or None, filename=f.name, title=f.name,
                             mimetype=mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        print(f"sent {f.name} to you", flush=True)
    except Exception as e:
        print("send file:", str(e)[:300], flush=True)
        send_to_owner(f"Couldn't send {f.name}: {str(e)[:150]}")


def assistant_reply(info, wait=1.5):
    time.sleep(wait)  # you often send a few lines / photos - answer them together
    if not assistant_lock.acquire(blocking=False):
        return  # already answering; it reads the newest lines too
    try:
        for _ in range(3):  # you sent more while it was answering -> one more round for those
            hist = history(assistant.HISTORY_KEY, 20)
            if not hist or hist[-1]["role"] != "user":
                return
            text = assistant.reply(hist, lambda t: send_to_owner(t, info["jid"]),
                                   send_photos=lambda photos: send_photos(info["jid"], photos))
            if text:
                send_to_owner(text, info["jid"], sticker=random.random() < 0.12)
            else:
                return
    except Exception as e:
        print("assistant reply failed:", str(e)[:300], flush=True)
    finally:
        assistant_lock.release()


def assistant_clock():
    """Every minute: time for one of today's reminders? (assistant.tick decides and words it)."""
    while True:
        time.sleep(60)
        if state.get("status") != "ready":
            continue
        try:
            assistant.tick(send_to_owner)
        except Exception as e:
            print("assistant clock:", str(e)[:300], flush=True)


# ---------- control API (dashboard) ----------
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

    def do_GET(self):
        if self.headers.get("X-Bridge-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        if self.path == "/status":
            return self._send(200, {k: v for k, v in state.items()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("X-Bridge-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        if self.path == "/assistant_say":  # a given text from the assistant to you (tests)
            try:
                text = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}").get("text")
            except ValueError:
                text = None
            if text:
                threading.Thread(target=send_to_owner, args=(assistant.no_emoji(text),),
                                 kwargs={"sticker": True}, daemon=True).start()
            return self._send(200, {"ok": bool(text)})
        if self.path == "/assistant_file":  # a file on this PC -> you, as a document (no WhatsApp video size limit)
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                req = {}
            f = Path(req.get("path") or "")
            if not f.is_file():
                return self._send(200, {"ok": False, "error": "no such file"})
            threading.Thread(target=send_file_to_owner, args=(f, req.get("caption") or ""), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/assistant_nudge":  # dashboard: "Send one now"
            threading.Thread(target=lambda: assistant.tick(send_to_owner, force=True), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/logout":
            try:
                client.logout()
            finally:
                self._send(200, {"ok": True})
                threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        self._send(404, {"error": "not found"})


class OwnPort(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows reuse lets a second copy "bind" the same port and overwrite .token


if __name__ == "__main__":
    try:
        server = OwnPort(("127.0.0.1", PORT), Api)
    except OSError:
        print(f"wa_bot already running on {PORT} - this copy exits", flush=True)
        sys.exit(0)
    (DATA / ".token").write_text(TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=qr_watchdog, daemon=True).start()
    import wa_blast
    threading.Thread(target=wa_blast.worker, args=(client, "bot", state), daemon=True).start()
    threading.Thread(target=assistant_clock, daemon=True).start()
    import reply_approval
    mine = {"at": 0, "st": None}  # your number, read once a minute (not once per chat)

    def is_mine(phone):
        if time.time() - mine["at"] > 60:
            mine.update(at=time.time(), st=assistant.load())
        return assistant.is_owner(phone, mine["st"])
    threading.Thread(target=reply_approval.watch, args=(lambda t: send_to_owner(t), is_mine),
                     kwargs={"log": lambda t: print(t, flush=True)}, daemon=True).start()
    print(f"wa_bot listening on 127.0.0.1:{PORT}", flush=True)
    try:
        client.connect()
    except Exception as e:
        state.update(status="error", error=str(e))
        time.sleep(30)
    sys.exit(0)
