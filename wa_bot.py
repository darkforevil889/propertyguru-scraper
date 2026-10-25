"""WhatsApp auto-reply bot (runs on this PC, on a SEPARATE number from the scanner).

* links by QR as a linked device ("KL Tracker Bot") - its own session in wa_bot_data/
* replies instantly to PRIVATE chats only (never groups / status / channels); never messages anyone first
* waits a few seconds so several messages / photos from one person get one answer
* answers via bot_brain (AI + your listings, condo prices, FAQ; saves owner listings with their photos)
Control API on 127.0.0.1:3079, protected by wa_bot_data/.token. Start it from the dashboard's "Chat bot" page.
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "wa_bot_data"
MEDIA = DATA / "media"
PORT = 3079
DATA.mkdir(exist_ok=True)
MEDIA.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
os.chdir(DATA)  # the bot's own WhatsApp session file lives here

from neonize.client import NewClient  # noqa: E402
from neonize.events import ConnectedEv, LoggedOutEv, MessageEv, PairStatusEv  # noqa: E402
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps  # noqa: E402
from neonize.utils.enum import ChatPresence, ChatPresenceMedia  # noqa: E402
import segno  # noqa: E402

import assistant  # noqa: E402
import bot_brain  # noqa: E402

TOKEN = secrets.token_hex(24)
(DATA / ".token").write_text(TOKEN)
SETTINGS = DATA / "bot_settings.json"
STARTED = time.time()
state = {"status": "starting", "qr": None, "me": None, "error": None, "replies_today": 0, "day": time.strftime("%Y%m%d")}

db_lock = threading.Lock()
db = sqlite3.connect(DATA / "bot.sqlite", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS log (chat TEXT, name TEXT, role TEXT, text TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS log_chat ON log (chat, ts);
""")


def settings():
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return {"enabled": True, "max_replies_per_day": 500, "ignore_numbers": [], **s}


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


# ---------- per-chat waiting: several messages / photos -> one reply ----------
pending = {}  # chat -> {"timer", "jid", "name", "phone", "photos": [paths]}
photos_by_chat = {}  # chat -> photo paths sent since the last saved owner listing


def schedule(chat, info, delay):
    old = pending.get(chat)
    if old and old.get("timer"):
        old["timer"].cancel()
    t = threading.Timer(delay, answer, args=(chat,))
    pending[chat] = {**info, "timer": t}
    t.daemon = True
    t.start()


def answer(chat):
    info = pending.pop(chat, None)
    if not info:
        return
    s = settings()
    today = time.strftime("%Y%m%d")
    if state["day"] != today:
        state.update(day=today, replies_today=0)
    if not s["enabled"] or state["replies_today"] >= s["max_replies_per_day"]:
        return
    try:
        client.send_chat_presence(info["jid"], ChatPresence.CHAT_PRESENCE_COMPOSING,
                                  ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
    except Exception:
        pass

    def save_owner_listing(**fields):
        return save_listing(chat, info, fields)

    text = bot_brain.reply(history(chat), save_owner_listing)
    if not text:
        return
    # Like a person: separate thoughts go out as separate bubbles, each after a short "typing" pause
    parts = [p.strip() for p in text.split("\n\n") if p.strip()][:4]
    try:
        for i, part in enumerate(parts):
            if i:
                try:
                    client.send_chat_presence(info["jid"], ChatPresence.CHAT_PRESENCE_COMPOSING,
                                              ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
                except Exception:
                    pass
            time.sleep(min(5, 1.2 + len(part) / 45))  # human-ish typing time
            client.send_message(info["jid"], part)
        log(chat, info.get("name"), "assistant", text)
        state["replies_today"] += 1
    except Exception as e:
        print("send failed:", e, flush=True)


def save_listing(chat, info, f):
    """Owner listing collected by the bot -> Supabase owner_listings (+ the photos they sent)."""
    from phone_filter import extract_from_text
    db_ = bot_brain._db()
    lid = f"bot-{''.join(c for c in chat if c.isalnum())[:30]}-{int(time.time())}"
    paths = []
    for p in photos_by_chat.pop(chat, []):
        try:
            name = f"files/{lid}-{Path(p).name}"
            db_.storage.from_("owner-photos").upload(name, Path(p).read_bytes(),
                                                     {"content-type": "image/jpeg", "upsert": "true"})
            paths.append(name)
        except Exception:
            pass
    phone = next((n for n, _, _ in extract_from_text(info.get("phone") or "") if n), info.get("phone"))
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history(chat, 30))
    row = {"message_id": lid, "chat": "WhatsApp bot", "posted_at": datetime.now(timezone.utc).isoformat(),
           "sender_name": info.get("name"), "sender_phone": phone, "listing_type": f.get("listing_type"),
           "price_rm": f.get("price_rm"), "sqft": f.get("sqft"), "bedrooms": f.get("bedrooms"),
           "bathrooms": f.get("bathrooms"), "furnishing": f.get("furnishing"), "place": f.get("place"),
           "unit_no": f.get("unit_no"), "owner_name": f.get("owner_name") or info.get("name"),
           "phones": phone or "", "photo_count": len(paths), "photo_paths": paths,
           "message_text": (f.get("notes") or "") + "\n\n--- chat ---\n" + transcript, "scanned_by": "WhatsApp bot"}
    db_.table("owner_listings").upsert(row, on_conflict="message_id").execute()
    return {"saved": True, "photos_attached": len(paths)}


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
    if phone and phone in settings()["ignore_numbers"]:
        return
    m = ev.Message
    for w in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        if m.HasField(w):
            m = getattr(m, w).message
    name = ev.Info.Pushname or None
    info = {"jid": src.Chat, "name": name, "phone": phone}
    owner = is_owner_chat(chat, phone)
    if m.HasField("stickerMessage") and owner:  # your stickers: kept for the assistant to use
        try:
            save_sticker(c.download_any(m))
            log(assistant.HISTORY_KEY, name, "user", "[sent a sticker]")
            if random.random() < 0.5:  # sometimes a sticker back
                threading.Thread(target=send_sticker, args=(src.Chat,), daemon=True).start()
        except Exception as e:
            print("sticker:", str(e)[:200], flush=True)
        return
    if m.HasField("imageMessage") and owner:  # your photo / screenshot: the assistant looks at it
        try:
            folder = ROOT / "data" / "assistant_photos"  # in the project, so a change request can show it to Claude Code
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{ev.Info.ID[:10]}.jpg"
            path.write_bytes(c.download_any(m))
            log(assistant.HISTORY_KEY, name, "user",
                f"[sent a photo: {path.relative_to(ROOT).as_posix()}] {m.imageMessage.caption or ''}".strip())
            threading.Thread(target=assistant_reply, args=(info, 10), daemon=True).start()  # several photos: wait a bit
        except Exception as e:
            print("owner photo:", str(e)[:200], flush=True)
        return
    if not owner and not settings()["enabled"]:
        # property bot OFF: other people's chats are neither kept nor answered (this may be your own main number) -
        # only a quick-reply option someone tapped from a blast still gets its set answer
        text = text_of(m)
        if text.strip():
            threading.Thread(target=blast_option_only, args=(chat, info, phone, text), daemon=True).start()
        return
    if m.HasField("imageMessage"):
        try:
            path = MEDIA / f"{''.join(ch for ch in chat if ch.isalnum())[:30]}_{ev.Info.ID}.jpg"
            path.write_bytes(c.download_any(m))
            photos_by_chat.setdefault(chat, []).append(str(path))
        except Exception:
            pass
        cap = m.imageMessage.caption or ""
        log(chat, name, "user", f"[sent a photo] {cap}".strip())
        schedule(chat, info, 25)  # owners often send several photos - answer once they pause
        return
    text = text_of(m)
    if not text.strip():
        return
    if owner:  # your own number: your personal assistant (works even with the bot OFF)
        log(assistant.HISTORY_KEY, name, "user", text.strip())
        threading.Thread(target=assistant_reply, args=(info,), daemon=True).start()
        return
    log(chat, name, "user", text.strip())
    threading.Thread(target=route_reply, args=(chat, info, phone, text), daemon=True).start()


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
    return chat in known


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
        time.sleep(min(5, 1.2 + len(part) / 45))
        client.send_message(jid, part)
    log(assistant.HISTORY_KEY, "assistant", "assistant", text)
    if sticker:
        send_sticker(jid)


def assistant_reply(info, wait=4):
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


def route_reply(chat, info, phone, text):
    """They answered a blast from this number and a trigger fits -> that auto reply; otherwise the AI as usual."""
    import wa_blast
    answer = wa_blast.auto_reply("bot", phone, text)
    if answer:
        send_option_reply(chat, info, answer)
    else:
        schedule(chat, info, 6)  # wait a moment in case they send more lines


def blast_option_only(chat, info, phone, text):
    """Bot OFF: answer only a blast's quick-reply option (logged only then)."""
    import wa_blast
    answer = wa_blast.auto_reply("bot", phone, text)
    if answer:
        log(chat, info.get("name"), "user", text.strip())
        send_option_reply(chat, info, answer)


def send_option_reply(chat, info, answer):
    time.sleep(3)
    try:
        client.send_chat_presence(info["jid"], ChatPresence.CHAT_PRESENCE_COMPOSING,
                                  ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
    except Exception:
        pass
    time.sleep(min(6, 1.5 + len(answer) / 40))
    try:
        client.send_message(info["jid"], answer)
        log(chat, info.get("name"), "assistant", answer)
    except Exception as e:
        print("blast auto reply:", e, flush=True)


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


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=qr_watchdog, daemon=True).start()
    import wa_blast
    threading.Thread(target=wa_blast.worker, args=(client, "bot", state), daemon=True).start()
    threading.Thread(target=assistant_clock, daemon=True).start()
    print(f"wa_bot listening on 127.0.0.1:{PORT}", flush=True)
    try:
        client.connect()
    except Exception as e:
        state.update(status="error", error=str(e))
        time.sleep(30)
    sys.exit(0)
