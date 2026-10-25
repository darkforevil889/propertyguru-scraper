"""WhatsApp-style chat window for your own WhatsApp (the number linked on the WhatsApp listing page).

A small web page on THIS PC only (http://127.0.0.1:3081 - nothing is reachable from outside, nothing goes online):
chats with profile pictures on the left, the conversation with photos / voice notes / stickers on the right, reply
box at the bottom. Reads what the WhatsApp connector (wa_service.py) stores and sends through it.
The dashboard's "WhatsApp chats" page shows it; it starts by itself when that page is opened.

    python wa_chat_ui.py
"""
import json
import mimetypes
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent.resolve()
PAGE = ROOT / "wa_chat_ui.html"
PORT = 3081
sys.path.insert(0, str(ROOT))


def running():
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def ensure_running():
    """Start it in the background (no window) if it isn't running yet."""
    if running():
        return
    python = Path(sys.executable).with_name("pythonw.exe")
    (ROOT / "logs").mkdir(exist_ok=True)
    subprocess.Popen([str(python if python.exists() else sys.executable), str(ROOT / "wa_chat_ui.py")], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=open(ROOT / "logs" / "wa_chat_ui.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(20):
        time.sleep(0.5)
        if running():
            return


def _kind(m):
    return ("photo" if m["has_image"] else "pdf" if m["is_pdf"] else "video" if m["is_video"] else "zip"
            if m["is_zip"] else "voice" if m.get("is_voice") else "sticker" if m.get("is_sticker") else "")


def chats():
    import wa_client
    out = wa_client.chats()[:200]
    prev = wa_client.last_lines([j for c in out for j in c["jids"]])
    for c in out:
        line = max((prev.get(j) or ("", 0) for j in c["jids"]), key=lambda x: x[1] or 0)[0]
        c["preview"] = line
        c["send_to"] = c["jid"] if c["is_group"] else (f"{c['phone']}@s.whatsapp.net" if c["phone"] else c["jids"][0])
    return out


def messages(key, chat=None):
    import wa_client
    c = chat or next((c for c in chats() if c["jid"] == key), None)
    if not c:
        return []
    msgs = wa_client.messages(c["jids"], time.time() - 365 * 86400, everything=True)[-150:]
    return [{"id": m["id"], "chat": m["chatId"], "me": m["fromMe"], "ts": m["timestamp"], "text": m["body"],
             "kind": _kind(m), "who": m["authorName"] or m["authorNumber"] or ""} for m in msgs]


_suggested = {}  # chat -> (its last message id, smart replies) - the AI is asked once per new message
_rules = {"at": 0, "rules": []}


def suggestions(key):
    """Smart reply buttons for this chat (smart_reply.py) - only when the customer wrote last."""
    import bot_brain
    import smart_reply
    msgs = messages(key)
    if not msgs or msgs[-1]["me"]:
        return []
    if _suggested.get(key, (None,))[0] == msgs[-1]["id"]:
        return _suggested[key][1]
    if time.time() - _rules["at"] > 60:  # rules changed on the dashboard show up within a minute
        _rules.update(at=time.time(), rules=smart_reply.load(bot_brain._db()))
    got = smart_reply.suggest(_rules["rules"], msgs)
    _suggested[key] = (msgs[-1]["id"], got)
    return got


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, cache=True):
        if not path or not Path(path).exists():
            self.send_response(404)
            self.end_headers()
            return
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        import wa_client
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._file(PAGE, cache=False)
            if u.path == "/api/status":
                s = wa_client.status() or {}
                return self._json({"status": s.get("status"), "me": s.get("me")})
            if u.path == "/api/chats":
                return self._json(chats())
            if u.path == "/api/messages":
                return self._json(messages(q.get("chat", "")))
            if u.path == "/api/suggest":
                return self._json(suggestions(q.get("chat", "")))
            if u.path == "/api/avatar":
                return self._file(wa_client.avatar(q.get("jid", "")))
            if u.path == "/api/media":
                return self._file(wa_client.media(q.get("chat", ""), q.get("id", "")))
        except Exception as e:
            return self._json({"error": str(e)[:200]}, 500)
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        import wa_client
        if urlparse(self.path).path != "/api/send":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            self._json(wa_client.send(body.get("to", ""), body.get("text", "")))
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:200]}, 500)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
