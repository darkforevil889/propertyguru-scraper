"""WhatsApp chats on the online dashboard: the PC copies your latest chats up, and sends the replies you type online.

PC side (keeper.py, every ~20 s):
  * sync(db): the 100 newest chats (name, picture, last line) -> Supabase storage owner-photos/data/wa_chats/
    index.json; for every chat with something new: its last 100 messages (+ the photos / voice notes / stickers of
    the latest 30, and the smart reply when the customer wrote last) -> wa_chats/c/<chat>.json, files in
    wa_chats/media/ and wa_chats/avatars/. The bucket is private - only you, logged in, can read it.
  * run_requests(db): replies typed online (pc_tasks 'wa_send') -> sent from this PC through the connector.
  * the same chats (no groups) also go to the HOMEX ERP's WhatsApp page, and its replies are sent - erp_chat_sync.py
Online side: index(db), chat(db, key), request_send(db, to, text, chat_key).
"""
import hashlib
import json
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
BUCKET, BASE = "owner-photos", "data/wa_chats"
STATE = ROOT / "wa_data" / "online_sync.json"  # what was copied up already (per chat: last message id)
CHATS, PER_CHAT, MEDIA_PER_CHAT = 100, 100, 30
MIME = {"jpg": "image/jpeg", "webp": "image/webp", "ogg": "audio/ogg", "mp4": "video/mp4", "pdf": "application/pdf"}


def key_of(jid):
    """A file-safe name for a chat."""
    return hashlib.sha1(jid.encode()).hexdigest()[:16]


def _up(db, path, data, ctype):
    db.storage.from_(BUCKET).upload(path, data, {"content-type": ctype, "upsert": "true"})


def _json_up(db, path, obj):
    _up(db, path, json.dumps(obj, ensure_ascii=False).encode(), "application/json")


def _load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chats": {}, "avatars": [], "media": []}


def _erp():
    """erp_chat_sync (reloaded, like this module) when the ERP link is set up, else None."""
    try:
        import importlib

        import erp_chat_sync
        importlib.reload(erp_chat_sync)
        return erp_chat_sync if erp_chat_sync.enabled() else None
    except Exception:
        return None


def sync(db, log=print):
    """Copy what's new up. -> how many chats were updated."""
    import wa_chat_ui
    import wa_client
    if (wa_client.status() or {}).get("status") != "ready":
        return 0
    st = _load_state()
    erp = _erp()
    est = erp.load_state() if erp else None

    def to_erp(c, msgs, suggestion):  # the ERP's WhatsApp page (one-to-one chats only); never stops the Supabase copy
        if erp and not c.get("is_group") and erp.want(est, c["key"], msgs[-1]["id"] if msgs else None):
            try:
                erp.push_chat(est, c, msgs, suggestion, key_of)
            except Exception as e:
                log(f"ERP whatsapp copy problem: {str(e)[:200]}")

    chats = wa_chat_ui.chats()[:CHATS]
    rules = None
    changed = 0
    for c in chats:
        c["key"] = key_of(c["jid"])
        pic = f"{c['send_to'].split('@')[0]}.jpg"
        if pic not in st["avatars"]:
            f = wa_client.avatar(c["send_to"])
            if f:
                _up(db, f"{BASE}/avatars/{pic}", Path(f).read_bytes(), "image/jpeg")
            st["avatars"].append(pic)  # no picture: not asked again until the PC's copy is refreshed
        c["avatar"] = f"{BASE}/avatars/{pic}" if (ROOT / "wa_data" / "avatars" / pic).exists() else None
        msgs = wa_chat_ui.messages(c["jid"], chat=c)[-PER_CHAT:]
        last = msgs[-1]["id"] if msgs else None
        if st["chats"].get(c["key"]) == last:
            to_erp(c, msgs, [])   # up to date online, maybe not yet on the ERP (e.g. its first copy)
            continue
        for m in msgs[-MEDIA_PER_CHAT:]:  # photos, voice notes, stickers of the latest messages
            if m["kind"] in ("photo", "voice", "sticker"):
                f = wa_client.media(m["chat"], m["id"])
                if f:
                    ext = Path(f).suffix.lstrip(".")
                    name = f"{BASE}/media/{key_of(m['id'])}.{ext}"
                    if name not in st["media"]:
                        _up(db, name, Path(f).read_bytes(), MIME.get(ext, "application/octet-stream"))
                        st["media"].append(name)
                    m["file"] = name
        suggestion = []
        if msgs and not msgs[-1]["me"]:  # the customer wrote last: your smart reply, ready online too
            import smart_reply
            if rules is None:
                rules = smart_reply.load(db)
            suggestion = smart_reply.suggest(rules, msgs)
        _json_up(db, f"{BASE}/c/{c['key']}.json", {"jid": c["jid"], "messages": msgs, "suggest": suggestion,
                                                    "at": time.time()})
        to_erp(c, msgs, suggestion)
        st["chats"][c["key"]] = last
        changed += 1
    _json_up(db, f"{BASE}/index.json", {"at": time.time(), "chats": [
        {k: c.get(k) for k in ("key", "jid", "name", "phone", "is_group", "last_ts", "preview", "send_to", "avatar")}
        for c in chats]})
    st["media"] = st["media"][-3000:]
    STATE.write_text(json.dumps(st), encoding="utf-8")
    if erp:
        try:
            erp.push_index(est, [c for c in chats if not c.get("is_group")])
        except Exception as e:
            log(f"ERP whatsapp list problem: {str(e)[:200]}")
        erp.save_state(est)
    if changed:
        log(f"whatsapp chats online: {changed} chat(s) updated")
    return changed


def run_requests(db, log=print):
    """Replies typed on the online dashboard -> sent from this PC. -> True if any were sent."""
    import wa_client
    tasks = db.table("pc_tasks").select("id,params").eq("kind", "wa_send").eq("status", "pending") \
        .order("id").execute().data
    for t in tasks:
        p = t.get("params") or {}
        try:
            res = wa_client.send(p.get("to", ""), p.get("text", ""))
            status = "done" if res.get("ok") else "failed"
        except Exception as e:
            status = "failed"
            log(f"online whatsapp reply not sent: {str(e)[:150]}")
        db.table("pc_tasks").update({"status": status}).eq("id", t["id"]).execute()
    erp_sent = False
    erp = _erp()
    if erp:
        try:
            erp_sent = erp.run_requests(log)
        except Exception as e:
            log(f"ERP whatsapp replies problem: {str(e)[:200]}")
    return bool(tasks) or erp_sent


# ---------- online side ----------
def index(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(f"{BASE}/index.json"))
    except Exception:
        return {}


def chat(db, key):
    try:
        return json.loads(db.storage.from_(BUCKET).download(f"{BASE}/c/{key}.json"))
    except Exception:
        return {}


def request_send(db, to, text):
    """Online: your reply goes to the PC, which sends it within about 15 seconds."""
    db.table("pc_tasks").insert({"kind": "wa_send", "params": {"to": to, "text": text}}).execute()


def pending_sends(db):
    return db.table("pc_tasks").select("id,params").eq("kind", "wa_send").eq("status", "pending") \
        .order("id").execute().data
