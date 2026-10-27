"""WhatsApp chats of the main number on the HOMEX ERP (online.homex.com.my -> WhatsApp page, e.g. for Ben).

Called from wa_chat_sync.py (which keeper.py reloads and runs every ~15-30 s), so no extra process:
  * push_chat / push_index: one-to-one chats only (no groups) -> the ERP's /api/wa-sync, photos / voice notes /
    stickers / profile pictures uploaded once each
  * run_requests: replies typed on the ERP page -> sent from this PC through the connector, then marked sent / failed
Needs ERP_SYNC_URL + ERP_SYNC_TOKEN in .env (the same token is WA_SYNC_TOKEN on the ERP). Off when they're missing.
What was sent up already: wa_data/erp_sync.json.
"""
import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.resolve()
STATE = ROOT / "wa_data" / "erp_sync.json"
MEDIA_PER_CHAT = 30


def _cfg():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    return os.environ.get("ERP_SYNC_URL", "").rstrip("/"), os.environ.get("ERP_SYNC_TOKEN", "")


def enabled():
    url, token = _cfg()
    return bool(url and token)


def _http():
    url, token = _cfg()
    return httpx.Client(base_url=url, headers={"X-Sync-Token": token, "Accept": "application/json"}, timeout=60)


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chats": {}, "files": []}


def save_state(st):
    st["files"] = st["files"][-4000:]
    STATE.write_text(json.dumps(st), encoding="utf-8")


# Kelvin's own chat is private: never copied to the ERP (Ben can see the ERP's WhatsApp page)
PRIVATE = ("601111314256",)


def private(c):
    ids = " ".join(str(c.get(k) or "") for k in ("phone", "send_to", "jid"))
    return any(p in ids for p in PRIVATE)


def want(st, key, last):
    """Does the ERP still need this chat's latest messages?"""
    return st["chats"].get(key) != last


def _file(cli, st, name, path):
    if name in st["files"] or not path or not Path(path).exists():
        return name in st["files"]
    r = cli.post("/file", data={"name": name}, files={"file": (Path(path).name, Path(path).read_bytes())})
    r.raise_for_status()
    st["files"].append(name)
    return True


def push_chat(st, c, msgs, suggest, key_of):
    """One chat's messages (+ its media) up to the ERP."""
    if private(c):
        return
    import wa_client
    out = []
    with _http() as cli:
        for i, m in enumerate(msgs):
            m = {k: m.get(k) for k in ("id", "me", "ts", "text", "kind", "who")}
            if i >= len(msgs) - MEDIA_PER_CHAT and m["kind"] in ("photo", "voice", "sticker"):
                f = wa_client.media(c["jid"], m["id"])
                if f:
                    name = f"media/{key_of(m['id'])}{Path(f).suffix}"
                    if _file(cli, st, name, f):
                        m["file"] = name
            out.append(m)
        cli.post("/chat", json={"key": c["key"], "jid": c["jid"], "send_to": c["send_to"], "messages": out,
                                "suggest": suggest or []}).raise_for_status()
    st["chats"][c["key"]] = msgs[-1]["id"] if msgs else None


def push_index(st, chats):
    """The chat list, newest first (with profile pictures)."""
    import wa_client
    rows = []
    with _http() as cli:
        for c in chats:
            if private(c):
                continue
            pic = f"avatars/{c['send_to'].split('@')[0]}.jpg"
            if pic not in st["files"]:
                f = wa_client.avatar(c["send_to"])
                if f:
                    _file(cli, st, pic, f)
            rows.append({"key": c["key"], "jid": c["jid"], "send_to": c["send_to"], "name": c.get("name"), "phone": c.get("phone"),
                         "last_ts": c.get("last_ts") or 0, "preview": c.get("preview") or "",
                         "avatar": pic if pic in st["files"] else None})
        cli.post("/index", json={"chats": rows}).raise_for_status()


def run_requests(log=print):
    """Replies typed on the ERP's WhatsApp page -> sent from this PC. -> True if any were sent."""
    if not enabled():
        return False
    import wa_client
    with _http() as cli:
        r = cli.get("/outbox")
        r.raise_for_status()
        todo = r.json().get("data") or []
        for t in todo:
            try:
                res = wa_client.send(t["send_to"], t["text"])
                status = "sent" if res.get("ok") else "failed"
            except Exception as e:
                status = "failed"
                log(f"ERP whatsapp reply not sent: {str(e)[:150]}")
            cli.post(f"/outbox/{t['id']}", json={"status": status})
    return bool(todo)
