"""Dashboard side of your WhatsApp assistant (wa_bot.py): start it, status, link / unlink, chat log."""
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "wa_bot_data"
URL = "http://127.0.0.1:3079"


def _call(method, path, timeout=5, body=None):
    try:
        token = (DATA / ".token").read_text().strip()
    except OSError:
        token = ""
    data = json.dumps(body or {}).encode() if method == "POST" else None
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
    if s := status():
        return s
    try:
        import neonize  # noqa: F401
    except ImportError:
        raise RuntimeError("The WhatsApp bot only runs on the PC it was set up on.")
    (ROOT / "logs").mkdir(exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    quiet = Path(sys.executable).with_name("pythonw.exe")  # no console window popping up
    subprocess.Popen([str(quiet if quiet.exists() else sys.executable), "-u", str(ROOT / "wa_bot.py")], cwd=ROOT,
                     creationflags=flags,
                     stdout=open(ROOT / "logs" / "wa_bot.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(30):
        time.sleep(1)
        if s := status():
            return s
    raise RuntimeError("The bot didn't start - see logs/wa_bot.log")


def logout():
    try:
        _call("POST", "/logout", timeout=20)
    except Exception:
        pass


def assistant_say(text):
    """Your assistant sends you this text (and maybe one of your stickers) - e.g. a test."""
    return _call("POST", "/assistant_say", timeout=10, body={"text": text})


def assistant_nudge():
    """Your assistant sends you one reminder now (the bot words and sends it in the background)."""
    return _call("POST", "/assistant_nudge", timeout=10)


def set_env(key, value):
    """Save a setting (e.g. the AI key) in .env on this PC only."""
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"{key}={value}"
    text = re.sub(rf"(?m)^{key}=.*$", line, text) if re.search(rf"(?m)^{key}=", text) else text.rstrip() + "\n" + line + "\n"
    env.write_text(text, encoding="utf-8")


def conversations(limit=40):
    """Latest chats with their last messages: [{chat, name, last_ts, messages:[(role, text, ts)]}]."""
    f = DATA / "bot.sqlite"
    if not f.exists():
        return []
    con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    with con:
        chats = con.execute("SELECT chat, MAX(name), MAX(ts) FROM log GROUP BY chat ORDER BY MAX(ts) DESC LIMIT ?",
                            (limit,)).fetchall()
        out = []
        for chat, name, last in chats:
            msgs = con.execute("SELECT role, text, ts FROM log WHERE chat=? ORDER BY ts DESC, rowid DESC LIMIT 12",
                               (chat,)).fetchall()
            out.append({"chat": chat, "name": name, "last_ts": last, "messages": list(reversed(msgs))})
    return out
