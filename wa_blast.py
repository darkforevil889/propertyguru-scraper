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
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

MYT = timezone(timedelta(hours=8))  # Malaysia time - the online dashboard's server runs on UTC
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



def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", Path(name).name) or "file"


def new_spec(sender, numbers, message, files=(), triggers=(), start_at=None, delay=(45, 120), daily_limit=40,
             window=(9 * 60, 21 * 60), rest=(0, 0), test=False):
    """A blast as saved (here on the PC, or online in Supabase). files = attachment file names.
    window = (from, until) in minutes of the day; rest = (every N sent messages, rest that many minutes);
    triggers = [{'when', 'reply'}] - auto replies for people who answer the blast."""
    return {"id": datetime.now(MYT).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "created": time.time(),
            "sender": sender, "numbers": numbers, "message": message, "files": list(files),
            "triggers": [t for t in triggers or [] if t.get("when") and t.get("reply")],
            "start_at": start_at or time.time(), "delay": list(delay), "daily_limit": int(daily_limit),
            "window": list(window) if window else None, "rest": {"every": int(rest[0]), "minutes": int(rest[1])},
            "test": test, "control": "run", "control_at": time.time()}


def create(sender, numbers, message, attachments=(), **kw):
    """Save a new blast on this PC; the sender picks it up within a few seconds (or at start_at). Returns its id."""
    DIR.mkdir(exist_ok=True)
    spec = new_spec(sender, numbers, message, [safe_name(n) for n, _ in attachments], **kw)
    for name, data in attachments:
        folder = FILES / spec["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / safe_name(name)).write_bytes(data)
    _write(DIR / f"{spec['id']}.json", spec)
    return spec["id"]


def set_control(bid, control, at=None):
    """'run' | 'pause' | 'cancel'. control_at = when it was changed (the newest change wins when syncing)."""
    path = DIR / f"{bid}.json"
    spec = _read(path)
    if spec:
        spec["control"], spec["control_at"] = control, at or time.time()
        _write(path, spec)


def attachments_of(job):
    """[(file name, bytes)] of a blast's attachments (for "Try the failed again")."""
    folder = FILES / job["id"]
    return [(f, (folder / f).read_bytes()) for f in job.get("files") or [] if (folder / f).exists()]


def jobs():
    """Every blast, newest first, with its progress merged in."""
    out = []
    for p in DIR.glob("*.json"):
        if p.name.endswith(".progress.json"):
            continue
        spec = _read(p)
        if isinstance(spec, dict) and spec.get("id"):  # skips replied_*.json and other bookkeeping files
            spec["progress"] = _read(DIR / f"{spec['id']}.progress.json", {}) or {}
            out.append(spec)
    return sorted(out, key=lambda j: j["created"], reverse=True)


def recently_sent(days, all_jobs=None):
    """Numbers that got a (non-test) blast in the last N days."""
    since, done = time.time() - days * 86400, set()
    for j in all_jobs if all_jobs is not None else jobs():
        if not j.get("test"):
            done |= {p for p, r in (j["progress"].get("results") or {}).items()
                     if r.get("status") == "sent" and r.get("at", 0) >= since}
    return done


def clock(minutes):
    """540 -> '9:00 AM'."""
    return datetime(2000, 1, 1, minutes // 60 % 24, minutes % 60).strftime("%I:%M %p").lstrip("0")


def window_of(spec):
    """Sending hours as (from, until) minutes of the day, or None = any time (older blasts kept whole hours)."""
    if spec.get("window"):
        return spec["window"]
    h = spec.get("hours")
    return [h[0] * 60, h[1] * 60] if h else None


def in_window(w, now=None):
    now = now or datetime.now(MYT)
    m, (a, b) = now.hour * 60 + now.minute, w
    if a == b:
        return True
    return a <= m < b if a < b else (m >= a or m < b)  # e.g. 10 PM - 2 AM goes past midnight


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
        return f"Scheduled for {datetime.fromtimestamp(job['start_at'], MYT):%d %b %Y %I:%M %p}"
    if time.time() < prog.get("rest_until", 0):
        every = (job.get("rest") or {}).get("every")
        return (f"Resting until {datetime.fromtimestamp(prog['rest_until'], MYT):%I:%M %p}".replace(" 0", " ")
                + (f" (break after every {every} messages)" if every else ""))
    return prog.get("state") or "Waiting for WhatsApp to connect"


# ---------- sender side (runs inside wa_service.py / wa_bot.py) ----------
def personalise(text, name):
    """{name} -> the person's name (or nothing); {Hi|Hello|Hey} -> one of them at random."""
    text = re.sub(r"\{([^{}|]*\|[^{}]*)\}", lambda m: random.choice(m.group(1).split("|")).strip(), text)
    if name:
        return text.replace("{name}", name.split()[0].title() if len(name) > 25 else name)
    return re.sub(r"[ ,]*\{name\}", "", text)


# ---------- auto-reply triggers: "when they ... -> reply ..." for people who answer a blast ----------
DEFAULT_TRIGGERS = [
    ("say they want to rent or sell (rent / sell / sewa / jual / 出租 / 卖)",
     "Where is the place ah? Do you have the details?"),
    ("mention a property, condo or place name",
     "Do you have the details and the size (sf)?"),
    ("ask who this is or how I got their number",
     "Hi, I'm Kelvin Low, property agent focusing on KLCC & Mont Kiara 😊 Do you have any property to rent or sell?"),
    ("", ""),
]
REPLY_DAYS = 14  # people who answer a blast get auto replies for this long
MAX_AUTO = 1     # auto replies per person per blast (your own test numbers: no limit)
SENT_IDS = set()  # ids of messages this PC sent itself - so they are never mistaken for you typing on the phone
_reply_lock = threading.Lock()


def _local(phone):
    digits = re.sub(r"\D", "", phone or "")
    return "0" + digits[2:] if digits.startswith("60") else digits


def _recent_job(sender, local):
    """Newest blast from this number that reached this person in the last REPLY_DAYS days and has triggers."""
    since = time.time() - REPLY_DAYS * 86400
    for j in jobs():
        r = ((j.get("progress") or {}).get("results") or {}).get(local) or {}
        if j["sender"] == sender and j.get("triggers") and r.get("status") == "sent" and r.get("at", 0) >= since:
            return j
    return None


def _pick_trigger(blast, text, rules):
    """Ask the free AI which rule fits their reply -> index into rules, or None."""
    import httpx
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    listing = "\n".join(f'{n}. When they {r["when"]} -> auto reply: "{r["reply"]}"'
                        for n, (_, r) in enumerate(rules, 1))
    prompt = (f'A Malaysian property agent sent this WhatsApp message to many people:\n"{blast}"\n\n'
              f'One person replied:\n"{text}"\n\nAuto-reply rules:\n{listing}\n\n'
              "Check every rule on its own:\n"
              '- "fits": does the "When they ..." part clearly describe this reply?\n'
              '- "redundant": would its auto reply ask for something the reply already gives (e.g. it asks where '
              "the place is but they already named the place)?\n"
              "People may write English, Malay, Chinese or a mix.\n"
              'Reply ONLY with JSON: {"rules": [{"n": 1, "fits": true/false, "redundant": true/false}, ...]}')
    for model in ("qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"):
        body = {"model": model, "temperature": 0, "max_tokens": 400, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}]}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        try:
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=40, headers={
                "Authorization": f"Bearer {os.environ.get('BOT_LLM_API_KEY', '')}", "User-Agent": "KLTracker/1.0"})
            if r.status_code != 200:
                continue  # busy / limit / model gone - try the next one
            checks = json.loads(re.search(r"\{.*\}", r.json()["choices"][0]["message"]["content"], re.S)
                                .group(0)).get("rules") or []
            ok = sorted(int(c["n"]) for c in checks
                        if c.get("fits") is True and c.get("redundant") is not True and 1 <= int(c["n"]) <= len(rules))
            return rules[ok[0] - 1][0] if ok else None  # first fitting rule in your order
        except Exception:
            continue
    return None


def my_numbers():
    """Your own numbers (WhatsApp page settings) - used for testing, so they get auto replies every time."""
    s = _read(ROOT / "wa_data" / "settings.json", {}) or {}
    return {_local(n) for n in s.get("my_numbers") or [] if _local(n)}


def auto_reply(sender, phone, text):
    """Someone answered a recent blast -> the auto reply whose trigger fits their message, or None.
    Anyone else gets MAX_AUTO auto reply (the first time a rule fits), never after you replied yourself;
    your own test numbers get one every time."""
    local = _local(phone)
    if not local or not (text or "").strip():
        return None
    with _reply_lock:
        j = _recent_job(sender, local)
        if not j:
            return None
        tester = local in my_numbers()
        path, key = DIR / f"replied_{sender}.json", f"{j['id']}|{local}"
        state = _read(path, {}) or {}
        mine = state.get(key) or {"used": [], "human": False}
        if not tester and (mine.get("human") or len(mine["used"]) >= MAX_AUTO):
            return None
        rules = [(i, t) for i, t in enumerate(j["triggers"]) if t.get("reply") and (tester or i not in mine["used"])]
        if not rules:
            return None
        i = _pick_trigger(personalise(j.get("message") or "", None), text.strip(), rules)
        if i is None:
            return None
        if not tester:
            mine["used"].append(i)
            state[key] = mine
            _write(path, state)
        return j["triggers"][i]["reply"]


def human_replied(sender, phone):
    """You wrote to this person yourself -> no more auto replies to them for their latest blast."""
    local = _local(phone)
    with _reply_lock:
        j = _recent_job(sender, local) if local else None
        if j:
            path, key = DIR / f"replied_{sender}.json", f"{j['id']}|{local}"
            state = _read(path, {}) or {}
            state.setdefault(key, {"used": [], "human": False})["human"] = True
            _write(path, state)


def full_text(spec, name, body_only=False):
    """The message for one person (links of older blasts written out at the end)."""
    text = personalise(spec.get("message") or "", name).strip()
    if body_only:
        return text
    lines = []
    b = spec.get("button") or {}  # blasts saved before multiple buttons existed
    if b.get("url"):
        lines.append(f"{(b.get('label') or 'Click here').strip()} 👉 {b['url'].strip()}")
    for b in spec.get("buttons") or []:
        lines.append({"url": f"{b['label']} 👉 {b['value']}", "call": f"{b['label']} 📞 {b['value']}",
                      "reply": f"👉 Reply \"{b['label']}\""}[b["type"]])
    if spec.get("footer"):
        lines.append(f"_{spec['footer']}_")
    return (text + "\n\n" + "\n".join(lines)).strip() if lines else text


def remember_sent(resp):
    """Note the id of a message this PC sent (see SENT_IDS)."""
    try:
        SENT_IDS.add(resp.ID)
        if len(SENT_IDS) > 5000:
            SENT_IDS.clear()
    except Exception:
        pass
    return resp


def _send_file(client, jid, f, cap):
    data, ext = f.read_bytes(), f.suffix.lower()
    if ext in IMAGE:
        remember_sent(client.send_image(jid, data, caption=cap))
    elif ext in VIDEO:
        try:
            remember_sent(client.send_video(jid, data, caption=cap))
        except Exception:  # video needs ffmpeg on this PC - send it as a file instead
            remember_sent(client.send_document(jid, data, caption=cap, filename=f.name, title=f.name,
                                               mimetype=mimetypes.guess_type(f.name)[0] or "video/mp4"))
    else:
        remember_sent(client.send_document(jid, data, caption=cap, filename=f.name, title=f.name,
                                           mimetype=mimetypes.guess_type(f.name)[0] or "application/octet-stream"))


def why(e):
    """An error from WhatsApp -> a short reason a person can understand."""
    t = str(e).lower()
    if any(k in t for k in ("timeout", "timed out", "websocket", "not connected", "connection", "eof", "context")):
        return "WhatsApp connection problem"
    if "blocked" in t or "forbidden" in t or "403" in t:
        return "WhatsApp refused it (this person may have blocked you)"
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:150]


def _send_one(client, spec, item):
    """Returns {'status': 'sent' | 'not on WhatsApp' | 'failed', 'error': reason, 'at': time};
    'retry': True means nothing went out yet and it is worth trying again (e.g. connection dropped)."""
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia
    try:
        found = client.is_on_whatsapp("+6" + item["phone"])  # 0123456789 -> +60123456789
    except Exception as e:
        return {"status": "failed", "error": why(e), "at": time.time(), "retry": True}
    if not found or not found[0].IsIn:
        return {"status": "not on WhatsApp", "error": "This number isn't on WhatsApp", "at": time.time()}
    jid = found[0].JID
    text = full_text(spec, item.get("name"))
    sent_any = False
    try:
        try:
            client.send_chat_presence(jid, ChatPresence.CHAT_PRESENCE_COMPOSING,
                                      ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception:
            pass
        time.sleep(min(6, 2 + len(text) / 50))  # "typing..."
        files = [FILES / spec["id"] / f for f in spec.get("files") or []]
        for f in files:
            if not f.exists():
                raise FileNotFoundError(f"attachment {f.name} is missing on this PC")
        if not files:
            try:
                remember_sent(client.send_message(jid, text, link_preview="http" in text))
            except Exception:
                remember_sent(client.send_message(jid, text))  # link preview fetch failed - plain text is fine
            sent_any = True
        for i, f in enumerate(files):
            _send_file(client, jid, f, (text or None) if i == 0 else None)
            sent_any = True
            if i < len(files) - 1:
                time.sleep(random.uniform(1.5, 3.5))
        return {"status": "sent", "at": time.time()}
    except Exception as e:
        if sent_any:  # don't send the first part twice - just report it
            return {"status": "failed", "error": f"Only part of it went through ({why(e)})", "at": time.time()}
        return {"status": "failed", "error": why(e), "at": time.time(), "retry": "connection" in why(e)}


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
            prog = job["progress"] or {}
            prog.setdefault("results", {})
            prog.setdefault("tries", {})
            if not job.get("test"):
                if now < next_at or now < prog.get("rest_until", 0):
                    continue
                w = window_of(job)
                if w and not in_window(w):
                    prog["state"] = f"Waiting - only sends between {clock(w[0])} and {clock(w[1])}"
                    _write(prog_path, prog)
                    continue
                midnight = datetime.now(MYT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                today = sum(1 for j in jobs() if j["sender"] == sender and not j.get("test")
                            for r in (j["progress"].get("results") or {}).values()
                            if r.get("status") == "sent" and r.get("at", 0) >= midnight)
                if today >= job["daily_limit"]:
                    prog["state"] = f"Waiting - today's limit of {job['daily_limit']} reached, continues tomorrow"
                    _write(prog_path, prog)
                    continue
            item = next(n for n in job["numbers"] if n["phone"] not in prog["results"])
            phone = item["phone"]
            prog["state"] = "Sending"
            res = _send_one(client, job, item)
            if res.pop("retry", False):
                prog["tries"][phone] = prog["tries"].get(phone, 0) + 1
                if prog["tries"][phone] < 3:  # connection hiccup: try this person again in a minute
                    prog["state"] = f"{res['error']} - trying again in a minute"
                    _write(prog_path, prog)
                    next_at = time.time() + 60
                    continue
                res["error"] += " (tried 3 times)"
            prog["results"][phone] = res
            left = len(job["numbers"]) - len(prog["results"])
            rest = job.get("rest") or {}
            if res["status"] == "sent" and not job.get("test") and rest.get("every") and rest.get("minutes"):
                prog["since_rest"] = prog.get("since_rest", 0) + 1
                if prog["since_rest"] >= rest["every"] and left:
                    prog["since_rest"], prog["rest_until"] = 0, time.time() + rest["minutes"] * 60
                    prog["state"] = "Resting"
            if not left:
                prog["state"], prog["finished"] = "Finished", time.time()
            _write(prog_path, prog)
            if not job.get("test"):
                next_at = time.time() + random.uniform(*job["delay"])
        except Exception as e:
            print("blast:", e, flush=True)
