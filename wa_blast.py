"""WhatsApp blast: one message (+ photos / videos / files, + an optional link "button") to a list of numbers,
sent slowly like a person would, now or at a set time.

Each blast is two files in wa_blast/:
  <id>.json           what to send - written by the dashboard (also its pause / cancel switch)
  <id>.progress.json  who got it   - written only by the sender running inside wa_service.py / wa_bot.py
so the two sides never overwrite each other. Attachments live in wa_blast/files/<id>/.
"""
import json
import mimetypes
import os
import random
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DIR = ROOT / "wa_blast"
FILES = DIR / "files"
SENDERS = {"main": "My WhatsApp (linked on the WhatsApp page)", "bot": "Chat bot number"}
IMAGE, VIDEO = {".jpg", ".jpeg", ".png", ".webp"}, {".mp4", ".mov", ".3gp", ".m4v"}


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------- dashboard side ----------
def parse_numbers(lines):
    """Lines like '012-345 6789', '0123456789 Ali', 'Ali, +60123456789' ->
    ([{'phone': '0123456789', 'name': 'Ali' or None}], rejected [(raw, reason)], duplicate count)."""
    from phone_filter import extract_from_text
    out, seen, rejected, dupes = [], set(), [], 0
    for line in lines:
        line = str(line).strip()
        if not line or line.lower() == "nan":
            continue
        found = list(extract_from_text(line))
        good = [(n, raw) for n, _, raw in found if n]
        rejected += [(raw, why) for n, why, raw in found if not n and len(re.sub(r"\D", "", raw)) >= 7]
        name = None
        if len(good) == 1:  # whatever else is on the line is the person's name
            name = re.sub(r"[\s,;:|\-–()+]+", " ", line.replace(good[0][1], " ")).strip()
            name = name if re.search(r"[A-Za-z]", name or "") else None
        for n, _ in good:
            if n in seen:
                dupes += 1
                continue
            seen.add(n)
            out.append({"phone": n, "name": name})
    return out, rejected, dupes


def lines_from_file(filename, data):
    """A .txt / .csv / Excel file -> one text line per row (all cells of the row joined)."""
    ext = Path(filename).suffix.lower()
    if ext == ".txt":
        return data.decode("utf-8-sig", errors="replace").splitlines()
    from phone_filter import read_tables
    return [" ".join(str(v) for v in row if str(v).strip() and str(v) != "nan")
            for t in read_tables(filename, data) for row in t.to_numpy()]


def create(sender, numbers, message, attachments=(), button=None, start_at=None, delay=(45, 120),
           daily_limit=40, hours=(9, 21), test=False):
    """Save a new blast; the sender picks it up within a few seconds (or at start_at). Returns its id."""
    DIR.mkdir(exist_ok=True)
    bid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    files = []
    for name, data in attachments:
        folder = FILES / bid
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]+', "_", Path(name).name) or "file"
        (folder / safe).write_bytes(data)
        files.append(safe)
    spec = {"id": bid, "created": time.time(), "sender": sender, "numbers": numbers, "message": message,
            "files": files, "button": button, "start_at": start_at or time.time(),
            "delay": list(delay), "daily_limit": int(daily_limit), "hours": list(hours) if hours else None,
            "test": test, "control": "run"}
    _write(DIR / f"{bid}.json", spec)
    return bid


def set_control(bid, control):
    """'run' | 'pause' | 'cancel'."""
    path = DIR / f"{bid}.json"
    spec = _read(path)
    if spec:
        spec["control"] = control
        _write(path, spec)


def jobs():
    """Every blast, newest first, with its progress merged in."""
    out = []
    for p in DIR.glob("*.json"):
        if p.name.endswith(".progress.json"):
            continue
        spec = _read(p)
        if spec:
            spec["progress"] = _read(DIR / f"{spec['id']}.progress.json", {}) or {}
            out.append(spec)
    return sorted(out, key=lambda j: j["created"], reverse=True)


def recently_sent(days):
    """Numbers that got a (non-test) blast in the last N days."""
    since, done = time.time() - days * 86400, set()
    for j in jobs():
        if not j.get("test"):
            done |= {p for p, r in (j["progress"].get("results") or {}).items()
                     if r.get("status") == "sent" and r.get("at", 0) >= since}
    return done


def state_of(job):
    """Human status for the dashboard."""
    prog, total = job.get("progress") or {}, len(job["numbers"])
    done = len(prog.get("results") or {})
    if job["control"] == "cancel":
        return "Cancelled"
    if done >= total:
        return "Finished"
    if job["control"] == "pause":
        return "Paused"
    if time.time() < job["start_at"]:
        return f"Scheduled for {datetime.fromtimestamp(job['start_at']):%d %b %Y %I:%M %p}"
    return prog.get("state") or "Waiting for WhatsApp to connect"


# ---------- sender side (runs inside wa_service.py / wa_bot.py) ----------
def personalise(text, name):
    """{name} -> the person's name (or nothing); {Hi|Hello|Hey} -> one of them at random."""
    text = re.sub(r"\{([^{}|]*\|[^{}]*)\}", lambda m: random.choice(m.group(1).split("|")).strip(), text)
    if name:
        return text.replace("{name}", name.split()[0].title() if len(name) > 25 else name)
    return re.sub(r"[ ,]*\{name\}", "", text)


def full_text(spec, name):
    text = personalise(spec.get("message") or "", name).strip()
    b = spec.get("button") or {}
    if b.get("url"):
        text += f"\n\n{(b.get('label') or 'Click here').strip()} 👉 {b['url'].strip()}"
    return text.strip()


def _send_one(client, spec, item):
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia
    try:
        found = client.is_on_whatsapp("+6" + item["phone"])  # 0123456789 -> +60123456789
    except Exception as e:
        return {"status": "failed", "error": str(e)[:200], "at": time.time()}
    if not found or not found[0].IsIn:
        return {"status": "not on WhatsApp", "at": time.time()}
    jid = found[0].JID
    text = full_text(spec, item.get("name"))
    try:
        try:
            client.send_chat_presence(jid, ChatPresence.CHAT_PRESENCE_COMPOSING,
                                      ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception:
            pass
        time.sleep(min(6, 2 + len(text) / 50))  # "typing..."
        files = [FILES / spec["id"] / f for f in spec.get("files") or []]
        if not files:
            try:
                client.send_message(jid, text, link_preview="http" in text)
            except Exception:
                client.send_message(jid, text)  # link preview fetch failed - plain text is fine
        for i, f in enumerate(files):
            cap, data, ext = (text or None) if i == 0 else None, f.read_bytes(), f.suffix.lower()
            if ext in IMAGE:
                client.send_image(jid, data, caption=cap)
            elif ext in VIDEO:
                try:
                    client.send_video(jid, data, caption=cap)
                except Exception:  # video needs ffmpeg on this PC - send it as a file instead
                    client.send_document(jid, data, caption=cap, filename=f.name, title=f.name,
                                         mimetype=mimetypes.guess_type(f.name)[0] or "video/mp4")
            else:
                client.send_document(jid, data, caption=cap, filename=f.name, title=f.name,
                                     mimetype=mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            if i < len(files) - 1:
                time.sleep(random.uniform(1.5, 3.5))
        return {"status": "sent", "at": time.time()}
    except Exception as e:
        return {"status": "failed", "error": str(e)[:200], "at": time.time()}


def _keep_awake(on):
    """Stop Windows sleeping while a blast is waiting / sending (no effect elsewhere)."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def worker(client, sender, state):
    """Background loop: send the next number of the oldest active blast, then wait a random gap."""
    next_at, awake = 0.0, None
    while True:
        time.sleep(3)
        try:
            active = [j for j in jobs() if j["sender"] == sender and j["control"] == "run"
                      and len(j["progress"].get("results") or {}) < len(j["numbers"])]
            if bool(active) != awake:
                awake = bool(active)
                _keep_awake(awake)
            if state.get("status") != "ready":
                continue
            now = time.time()
            due = sorted((j for j in active if j["start_at"] <= now), key=lambda j: (not j.get("test"), j["created"]))
            if not due:
                continue
            job = due[0]
            prog_path = DIR / f"{job['id']}.progress.json"
            prog = job["progress"] or {"results": {}}
            prog.setdefault("results", {})
            if not job.get("test"):
                if now < next_at:
                    continue
                h = job.get("hours")
                if h and not (h[0] <= datetime.now().hour < h[1]):
                    prog["state"] = f"Waiting - only sends between {h[0]}:00 and {h[1]}:00"
                    _write(prog_path, prog)
                    continue
                midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                today = sum(1 for j in jobs() if j["sender"] == sender and not j.get("test")
                            for r in (j["progress"].get("results") or {}).values()
                            if r.get("status") == "sent" and r.get("at", 0) >= midnight)
                if today >= job["daily_limit"]:
                    prog["state"] = f"Waiting - today's limit of {job['daily_limit']} reached, continues tomorrow"
                    _write(prog_path, prog)
                    continue
            item = next(n for n in job["numbers"] if n["phone"] not in prog["results"])
            prog["state"] = "Sending"
            prog["results"][item["phone"]] = _send_one(client, job, item)
            if len(prog["results"]) >= len(job["numbers"]):
                prog["state"], prog["finished"] = "Finished", time.time()
            _write(prog_path, prog)
            if not job.get("test"):
                next_at = time.time() + random.uniform(*job["delay"])
        except Exception as e:
            print("blast:", e, flush=True)
