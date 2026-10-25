"""Your personal WhatsApp assistant (Chat bot page -> My assistant).

Runs inside wa_bot.py on the bot's number and talks ONLY to your own number (settings "number"):
* reminders, one thing per message, at random times inside your hours (default 9 AM - 8 PM, a few a day):
  - to-dos ("the U Mobile thing") - every day until you say it's done
  - monthly things ("8 sim card", due on the 9th) - from N days before the due date, every day until you say you
    renewed; "bought, starts 5 Nov" moves the next due date to 5 Dec
  - check-ins ("how's your tenant?", "how many leads today?") - every few days
  The code decides WHAT is due; the AI only words it - short, Singlish / Chinese-English, never the same twice.
* your messages: the AI answers with tools that read this PC's data - reminders (done / not yet / new ones),
  saved WhatsApp listings, scanning the listing group, blasts (progress, resume, pause), condo prices,
  PropertyGuru posting, today's leads.
State: Supabase storage owner-photos/data/assistant.json (so the online page can change it too).
"""
import calendar
import json
import os
import random
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.resolve()
load_dotenv(ROOT / ".env")
BUCKET, PATH = "owner-photos", "data/assistant.json"
LOCAL = ROOT / "wa_bot_data" / "assistant.json"  # last copy, if Supabase can't be reached
MYT = timezone(timedelta(hours=8))
HISTORY_KEY = "assistant"  # the chat's key in wa_bot's conversation log

DEFAULT = {
    "enabled": True, "number": os.environ.get("ASSISTANT_NUMBER", ""), "start_hour": 9, "end_hour": 20, "per_day": 3,
    "tasks": [
        {"id": "umobile", "title": "the U Mobile thing", "kind": "todo", "status": "open"},
        {"id": "8sim", "title": "8 sim card unlimited call plan (renew / top up)", "kind": "monthly",
         "due": "2026-11-09", "remind_days_before": 10, "status": "open"},
        {"id": "agency", "title": "find a property agency to join", "kind": "todo", "status": "open"},
        {"id": "tenant", "title": "how the tenant is doing", "kind": "checkin", "every_days": 7, "status": "open"},
        {"id": "leads", "title": "how many leads (owner listings) collected today", "kind": "checkin",
         "every_days": 2, "status": "open"},
    ],
    "plan": {}, "waiting": None,
}


# ---------- state ----------
def _db():
    import bot_brain
    return bot_brain._db()


def load(db=None):
    """db: the online dashboard passes its own login; the PC uses its key."""
    try:
        st = json.loads((db or _db()).storage.from_(BUCKET).download(PATH))
    except Exception:
        try:
            st = json.loads(LOCAL.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = json.loads(json.dumps(DEFAULT))
    return {**DEFAULT, **st}


def save(st, db=None):
    text = json.dumps(st, ensure_ascii=False, indent=1)
    try:
        LOCAL.parent.mkdir(exist_ok=True)
        LOCAL.write_text(text, encoding="utf-8")
    except OSError:
        pass  # online dashboard: no local copy
    try:
        (db or _db()).storage.from_(BUCKET).upload(PATH, text.encode(), {"content-type": "application/json",
                                                                 "upsert": "true"})
    except Exception:
        pass


def now():
    return datetime.now(MYT)


def is_owner(phone, st=None):
    """Is this message from your own number?"""
    digits = re.sub(r"\D", "", phone or "")
    mine = re.sub(r"\D", "", (st or load()).get("number") or "")
    return bool(digits and mine and digits[-9:] == mine[-9:])


def add_month(d):
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def _day(ts):
    return datetime.fromtimestamp(ts, MYT).date() if ts else None


# ---------- what's due ----------
def due(st, today=None):
    """Tasks to bring up today, most pressing first - each at most once a day."""
    today = today or now().date()
    out = []
    for t in st["tasks"]:
        if t.get("status") in ("done", "cancelled") or _day(t.get("last_asked")) == today:
            continue
        if t["kind"] == "todo":
            out.append((1, t.get("last_asked") or 0, t))
        elif t["kind"] == "monthly" and t.get("due"):
            d = date.fromisoformat(t["due"])
            if today >= d - timedelta(days=int(t.get("remind_days_before") or 10)):
                out.append((0, (d - today).days, t))
        elif t["kind"] == "checkin":
            last = _day(t.get("last_asked"))
            if not last or (today - last).days >= int(t.get("every_days") or 7):
                out.append((2, t.get("last_asked") or 0, t))
    return [t for *_, t in sorted(out, key=lambda x: (x[0], x[1]))]


def plan_today(st):
    """Random times for today's messages inside your hours, at least 1.5 hours apart."""
    n, today = now(), now().date().isoformat()
    if st["plan"].get("day") == today:
        return st["plan"]
    start = n.replace(hour=int(st["start_hour"]), minute=0, second=0, microsecond=0)
    end = n.replace(hour=int(st["end_hour"]), minute=0, second=0, microsecond=0)
    times = []
    for _ in range(200):
        if len(times) >= int(st["per_day"]):
            break
        t = start + timedelta(seconds=random.randint(0, max(60, int((end - start).total_seconds()) - 60)))
        if all(abs((t - x).total_seconds()) >= 5400 for x in times):
            times.append(t)
    st["plan"] = {"day": today, "times": sorted(x.timestamp() for x in times), "sent": 0}
    return st["plan"]


# ---------- wording (the AI writes it; templates if it can't) ----------
STYLE = """You are Kelvin's own personal assistant on WhatsApp - like a close buddy / PA, not a company. Kelvin is a
Malaysian property agent (KLCC & Mont Kiara). Write like a Malaysian texting a friend: short, casual Singlish /
Manglish, sometimes a bit of Chinese mixed in (e.g. "记得", "已经", "lah", "ah", "leh", "lor", "meh", "hor",
"jor", "liao"), lowercase is fine, max 1-2 short lines, NO emoji at all. Never sound like a bot or a reminder app, never
use bullet points or formal words, never start with "Reminder:". Vary your wording every time."""

TEMPLATES = {
    "todo": ["eh {title} settle already anot?", "oi {title} done liao or still pending ah", "{title} how ah, 已经 ok 了吗?"],
    "monthly": ["eh {title} due {due} leh, {days} days only - settled anot?",
                "记得 {title} ah, due {due} ({days} days)", "{title} - {due} due hor, top up already?"],
    "checkin": ["eh {title}?", "boss, {title}?", "quick one - {title}?"],
}


def compose(task, st, extra=""):
    """One short message about one task, in Kelvin's style."""
    import bot_brain
    days = (date.fromisoformat(task["due"]) - now().date()).days if task.get("due") else None
    d = date.fromisoformat(task["due"]) if task.get("due") else None
    facts = {"about": task["title"], "kind": task["kind"], "last_answer": task.get("note"),
             "due": f"{d.day} {d:%b}" if d else None, "days_left": days, "extra": extra}
    asks = {"todo": "Ask casually whether it's done yet.",
            "monthly": "Nudge to renew / settle it before the due date (mention the date and days left).",
            "checkin": "Ask about it casually, like a friend checking in."}[task["kind"]]
    # a different flavour each time, and never like the last few messages - so it doesn't get boring
    flavour = random.choice(["Singlish", "Chinese-English mix (a few Chinese words / 一点华语)", "very short Manglish",
                             "playful, a bit cheeky", "chill, like texting a close friend"])
    recent = (st.get("recent") or [])[-10:]
    msg = [{"role": "system", "content": STYLE},
           {"role": "user", "content": f"Write ONE WhatsApp message to Kelvin. {asks} Style this time: {flavour}. "
                                       "Don't start with 'hey' / 'hey Kelvin' and don't reuse the openings or "
                                       f"phrasing of these earlier messages: {json.dumps(recent, ensure_ascii=False)}."
                                       f" Facts: {json.dumps(facts, ensure_ascii=False)}. Only the message text."}]
    try:
        text = (bot_brain._llm(msg, tools=[], temperature=1.0, max_tokens=600).get("content") or "").strip()
        text = text.strip('"').strip()
        if text and len(text) < 300:
            return text
    except Exception:
        pass
    d = date.fromisoformat(task["due"]) if task.get("due") else None
    return random.choice(TEMPLATES[task["kind"]]).format(title=task["title"], due=f"{d.day} {d:%b}" if d else "",
                                                         days=days)


def leads_today():
    """Owner listings posted today (a re-scan of older ones doesn't count)."""
    start = now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    return _db().table("owner_listings").select("message_id,place").gte("posted_at", start).execute().data


# ---------- the clock: wa_bot.py calls tick() every minute ----------
def tick(send, force=False):
    """Time for one of today's messages? -> send ONE reminder (the most pressing). send(text) sends to you.
    force: send one now (the dashboard's "Send one now") - outside your hours / plan too."""
    st = load()
    if not st.get("enabled") and not force:
        return None
    n = now()
    if not force:
        if not (int(st["start_hour"]) <= n.hour < int(st["end_hour"])):
            return None
        new_day = st["plan"].get("day") != n.date().isoformat()
        plan = plan_today(st)
        slots = [t for t in plan["times"] if t <= n.timestamp()]
        if len(slots) <= plan.get("sent", 0):
            if new_day:
                save(st)  # today's times, so the dashboard can show them
            return None
        w = st.get("waiting")  # one thing at a time: the last question still unanswered (< 3 h) -> wait
        if w and not w.get("answered") and n.timestamp() - w.get("at", 0) < 3 * 3600:
            return None
        if n.timestamp() < (st.get("quiet_until") or 0):  # you were just chatting with it - later
            return None
        plan["sent"] = len(slots)  # this slot is used, whether or not something was due
    todo = due(st, n.date())
    if not todo and force:  # nothing due today: the reminder asked about least recently
        todo = sorted((t for t in st["tasks"] if t.get("status") not in ("done", "cancelled")),
                      key=lambda t: t.get("last_asked") or 0)
    if not todo:
        save(st)
        return None
    task = todo[0]
    extra = ""
    if task["id"] == "leads":
        extra = f"so far today {len(leads_today())} owner listing(s) saved from WhatsApp"
    text = no_emoji(compose(task, st, extra))
    send(text, sticker=random.random() < 0.25)  # now and then one of your own stickers after it
    task["last_asked"] = n.timestamp()
    st["waiting"] = {"task": task["id"], "at": n.timestamp(), "text": text}
    st["recent"] = ((st.get("recent") or []) + [text])[-10:]
    save(st)
    return text


# ---------- your messages ----------
TOOLS = [
    {"type": "function", "function": {"name": "update_task", "description": (
        "Record Kelvin's answer about a reminder. result: done (finished / bought / renewed), not_yet (still "
        "pending - ask again tomorrow), cancel (drop it). For monthly things give started_on (YYYY-MM-DD) when he "
        "says when the new month starts ('start at 5 nov') - the next due date becomes a month after."),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "result": {"type": "string", "enum": ["done", "not_yet", "cancel"]},
            "started_on": {"type": "string", "description": "YYYY-MM-DD"},
            "note": {"type": "string", "description": "what he said, short"}}, "required": ["task_id", "result"]}}},
    {"type": "function", "function": {"name": "add_task", "description": (
        "Add a reminder. kind: todo (remind daily until done), monthly (due date each month, remind N days before), "
        "checkin (ask every N days)."), "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "kind": {"type": "string", "enum": ["todo", "monthly", "checkin"]},
            "due": {"type": "string", "description": "YYYY-MM-DD (monthly)"},
            "remind_days_before": {"type": "integer"}, "every_days": {"type": "integer"}},
            "required": ["title", "kind"]}}},
    {"type": "function", "function": {"name": "list_tasks", "description": "All reminders and their state.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "whatsapp_listings", "description": (
        "Owner listings saved from WhatsApp in the last N days (place, rent/sale, price, size, owner, number)."),
        "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "scan_listing_group", "description": (
        "Scan the WhatsApp listing group(s) on the PC now for the last N days and save the owner listings; the "
        "result is sent to Kelvin when done (a few minutes)."),
        "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "call_progress", "description": (
        "Today's ipline AI calls (单任务外呼): numbers left, on the line now, called today, finished or not, ipline's "
        "message. Read only - calls are started / stopped by Kelvin himself on ipline, never by you."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "blast_status", "description": (
        "WhatsApp blasts: how many sent / to go, paused / resting / finished, for the recent ones."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "blast_control", "description": "Resume (run) or pause a blast.",
                                      "parameters": {"type": "object", "properties": {
                                          "blast_id": {"type": "string", "description": "id, or 'latest'"},
                                          "action": {"type": "string", "enum": ["run", "pause"]}},
                                          "required": ["action"]}}},
    {"type": "function", "function": {"name": "condo_price", "description": (
        "General MARKET price range (sale / rent, sizes) of a KLCC / Mont Kiara condo from PropertyGuru data - "
        "not Kelvin's own listings (that's find_saved_listing)."),
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "propertyguru_posts", "description": (
        "What was posted / saved as drafts on PropertyGuru (AgentNet) on a day."),
        "parameters": {"type": "object", "properties": {"day": {"type": "string", "description": "YYYY-MM-DD"}},
                       "required": ["day"]}}},
    {"type": "function", "function": {"name": "leads_today", "description": (
        "Owner listings (leads) saved today from WhatsApp scans."),
        "parameters": {"type": "object", "properties": {}}}},
]


def _update_task(st, task_id, result, started_on=None, note=None):
    t = next((x for x in st["tasks"] if x["id"] == task_id), None)
    if not t:
        return {"error": f"no reminder {task_id}"}
    t["note"], t["answered_at"] = note or result, time.time()
    if result == "cancel":
        t["status"] = "cancelled"
    elif result == "done":
        if t["kind"] == "monthly":
            base = date.fromisoformat(started_on) if started_on else date.fromisoformat(t.get("due") or
                                                                                         now().date().isoformat())
            t["due"], t["last_asked"] = add_month(base).isoformat(), time.time()
            t["status"] = "open"
        elif t["kind"] == "todo":
            t["status"] = "done"
    return {"ok": True, "task": t}


def _add_task(st, title, kind, due=None, remind_days_before=10, every_days=None):
    tid = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30] or f"t{int(time.time())}"
    t = {"id": tid, "title": title, "kind": kind, "status": "open"}
    if kind == "monthly":
        t.update(due=due or add_month(now().date()).isoformat(), remind_days_before=remind_days_before or 10)
    if kind == "checkin":
        t["every_days"] = every_days or 7
    st["tasks"].append(t)
    return {"ok": True, "task": t}


def _listings(days=1):
    since = (now() - timedelta(days=days or 1)).astimezone(timezone.utc).isoformat()
    rows = _db().table("owner_listings").select("place,listing_type,price_rm,sqft,owner_name,phones,unit_no") \
        .gte("posted_at", since).order("posted_at", desc=True).limit(30).execute().data
    return rows or {"result": "no owner listings saved in that time"}


def _blasts():
    import wa_blast
    out = []
    for j in wa_blast.jobs()[:5]:
        res = (j.get("progress") or {}).get("results") or {}
        sent = sum(1 for r in res.values() if r.get("status") == "sent")
        out.append({"id": j["id"], "started": datetime.fromtimestamp(j["created"], MYT).strftime("%d %b %H:%M"),
                    "from": "your number" if j.get("sender") == "main" else "bot number", "total": len(j["numbers"]),
                    "sent": sent, "failed": sum(1 for r in res.values() if r.get("status") != "sent"),
                    "to_go": len(j["numbers"]) - len(res), "state": wa_blast.state_of(j)})
    return out or {"result": "no blasts"}


def _blast_control(blast_id=None, action="run"):
    import wa_blast
    jobs = wa_blast.jobs()
    j = next((x for x in jobs if x["id"] == blast_id), None) or (jobs[0] if jobs else None)
    if not j:
        return {"error": "no blast"}
    wa_blast.set_control(j["id"], action)
    return {"ok": True, "blast": j["id"], "now": "resumed" if action == "run" else "paused"}


def _posts(day):
    import agent_post
    out = []
    for mid, r in agent_post.load_results(_db()).items():
        ts = r.get("posted_at") if r.get("state") == "posted" else r.get("at")
        if ts and datetime.fromtimestamp(ts, MYT).date().isoformat() == day:
            out.append({"place": r.get("place"), "state": r.get("state"), "headline": r.get("headline"),
                        "price": (r.get("plan") or {}).get("price")})
    return out or {"result": f"nothing posted or drafted on {day}"}


def _scan(days, send):
    """Scan the listing group(s) in the background, then tell Kelvin what was saved."""
    import wa_client
    import wa_scan

    def run():
        try:
            groups = wa_client.load_settings().get("listing_groups") or []
            res = wa_scan.scan(_db(), groups, float(days or 1), True, "assistant")
            got = res.get("listings") or []
            lines = [f"- {l.get('place') or '?'} · {l.get('type') or ''} · RM {l.get('price_rm') or '-'}"
                     for l in got[:10]]
            send(f"scan done liao, {len(got)} listing(s) saved" + (":\n" + "\n".join(lines) if lines else ""))
        except Exception as e:
            send(f"aiya scan cannot leh - {str(e).splitlines()[0][:120]}")
    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "note": "results will be sent in a few minutes"}


# ---------- more of your PC: listings, compare, PropertyGuru, AI call, blasts, Google Maps ----------
MORE_TOOLS = [
    {"type": "function", "function": {"name": "search_pg_listings", "description": (
        "Listings on PropertyGuru now that the PC collected (Listings page) - only KL rentals from RM 8,000/month "
        "and sales from RM 3 million (cheaper ones aren't collected: say so). Not condo price ranges - that's "
        "condo_price."),
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "enum": ["homes_rent", "commercial_rent", "homes_sale", "commercial_sale"]},
            "place": {"type": "string"}, "min_price": {"type": "integer"}, "max_price": {"type": "integer"},
            "bedrooms": {"type": "integer"}}, "required": ["category"]}}},
    {"type": "function", "function": {"name": "find_saved_listing", "description": (
        "USE FIRST for any question about a specific property / unit by name, owner or phone ('neu suites "
        "details', 'the axon one how much', 'jeremy unit') - Kelvin's own saved owner listings (from WhatsApp), "
        "spelling doesn't need to match. Gives price, size, rooms, furnishing, owner, "
        "number, unit, the PropertyGuru & iProperty comparison verdict, and whether it's posted on PropertyGuru."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "compare_listing", "description": (
        "Compare a saved listing with PropertyGuru & iProperty again now (the verdict comes in a few minutes)."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "propertyguru_listing", "description": (
        "Put a saved listing on PropertyGuru (AgentNet). action draft = save a draft (free); post = post it "
        "(costs credits). For post, call first WITHOUT confirm to get the preview, show it to Kelvin, and only "
        "after he clearly says yes in his next message call again with confirm=true."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "action": {"type": "string", "enum": ["draft", "post"]},
            "confirm": {"type": "boolean"}}, "required": ["query", "action"]}}},
    {"type": "function", "function": {"name": "ai_call_results", "description": (
        "AI call (ipline) results: calls checked and the people who want to sell / rent (number + what they said) "
        "for the last N days."), "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "ai_call_check", "description": (
        "Start checking the newest AI calls on ipline now (pages of 30 calls; quick = faster, a few leads missed)."),
        "parameters": {"type": "object", "properties": {"pages": {"type": "integer"},
                                                        "quick": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "start_blast", "description": (
        "Start a WhatsApp blast from Kelvin's number with his usual message and settings (his last blast's). "
        "source ai_call_wants = the AI call 'want' numbers of the last N days; numbers = the numbers he gives. "
        "People blasted in the last 30 days are skipped. ALWAYS call first without confirm to get the preview, "
        "show it, and only after he clearly says yes in his next message call again with confirm=true."),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "enum": ["ai_call_wants", "numbers"]}, "days": {"type": "integer"},
            "numbers": {"type": "string"}, "message": {"type": "string", "description": "only if he gives one"},
            "confirm": {"type": "boolean"}}, "required": ["source"]}}},
    {"type": "function", "function": {"name": "google_maps", "description": (
        "Google Map Extractor: start a search for businesses (company, address, phone, email); split_kl = search "
        "each KL area (more than 120 places, slow). Results come in its list later - use google_maps_results."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_places": {"type": "integer"},
                                                        "split_kl": {"type": "boolean"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "google_maps_results", "description": (
        "The latest Google Map Extractor search (or the one matching query): how many places, emails, mobiles, "
        "a few names."), "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "send_photos", "description": (
        "Send Kelvin pictures of a property on WhatsApp: his saved owner listing's photos (by place / owner), "
        "or - if he has no saved listing there - photos of that condo from PropertyGuru listings."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                        "how_many": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "dev_request", "description": (
        "Only for a NEW change he asks for (not to check progress - that's dev_status). Change or add something "
        "in Kelvin's own programs on the PC - dashboard pages (localhost), the assistant "
        "itself, AI call rules, scanning, posting robot, new features, fixes. Claude Code does it (backup first; "
        "'undo' puts it back). Not for reminders / data - use the other tools for those. ALWAYS call first "
        "without confirm: it restates the request; ask Kelvin to confirm; only after his clear yes in his next "
        "message call again with confirm=true."),
        "parameters": {"type": "object", "properties": {"task": {"type": "string", "description": (
            "what to change, in full, in plain words")}, "confirm": {"type": "boolean"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "dev_undo", "description": (
        "Undo the last change made with dev_request (puts the old programs back)."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "dev_status", "description": (
        "Progress of the change he asked for ('the change how already?', 'done yet?', 'finished?') - whether it's "
        "still working, finished, undone or failed, and what it changed."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "propertyguru_overview", "description": (
        "Everything on PropertyGuru through AgentNet: drafts waiting for his OK, posted, problems, credits left."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "web_search", "description": (
        "Search the internet (when the PC's data doesn't have it): top results with a short snippet and link."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "pc_status", "description": (
        "What the PC is doing now: AI call check, PropertyGuru compare, PropertyGuru posting, Google Maps search, "
        "WhatsApp connection."), "parameters": {"type": "object", "properties": {}}}},
]


def _find(query):
    """Saved listings matching a place / owner / phone, best match first - typos and spellings too ("neu suites"
    finds "Neu suits")."""
    import difflib
    q = (query or "").lower().strip()
    digits = re.sub(r"\D", "", q)
    rows = _db().table("owner_listings").select("*").order("posted_at", desc=True).execute().data
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 2]

    def score(r):
        if len(digits) >= 7 and digits[-8:] in re.sub(r"\D", "", r.get("phones") or ""):
            return 3.0
        if not words:
            return 0

        def fit(text):  # how well ALL the words match this text (each word to its closest word there)
            tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
            return sum(max((difflib.SequenceMatcher(None, w, t).ratio() for t in tokens), default=0)
                       for w in words) / len(words)
        place = f"{r.get('place') or ''} {r.get('unit_no') or ''}"
        # the building name counts most - also as a whole ("neo suite" ~ "neu suits"); the owner's name too
        return max(fit(place), difflib.SequenceMatcher(None, q, (r.get("place") or "").lower()).ratio(),
                   fit(r.get("owner_name")) - 0.05)
    ranked = sorted(((score(r), r) for r in rows), key=lambda x: -x[0])
    return [r for sc, r in ranked if sc >= 0.72][:5]


def _listing_detail(query):
    import agent_post
    import market_check
    rows = _find(query)
    if not rows:
        return {"result": f"no saved listing matching '{query}'"}
    mk, pg = market_check.load_results(_db()), agent_post.load_results(_db())
    out = []
    for r in rows:
        m, p = mk.get(r["message_id"]) or {}, pg.get(r["message_id"]) or {}
        out.append({"place": r.get("place"), "unit": r.get("unit_no"), "type": r.get("listing_type"),
                    "price_rm": r.get("price_rm"), "tenant_rent_rm": r.get("rental_rm"), "sqft": r.get("sqft"),
                    "rooms": f"{r.get('bedrooms') or '-'} bed {r.get('bathrooms') or '-'} bath",
                    "furnishing": r.get("furnishing"), "owner": r.get("owner_name"), "phone": r.get("phones"),
                    "posted_in_chat": (r.get("posted_at") or "")[:10], "propertyguru_verdict": m.get("label"),
                    "why": m.get("why"), "on_propertyguru": p.get("state") or "not yet"})
    return out


def _compare(query):
    import market_check
    rows = _find(query)
    if not rows:
        return {"result": f"no saved listing matching '{query}'"}
    market_check.start([rows[0]["message_id"]])
    return {"started": rows[0].get("place"), "note": "verdict in a few minutes - ask again then"}


def _pg_listing(st, turn, query, action, confirm=False):
    import agent_post
    rows = _find(query)
    if not rows:
        return {"result": f"no saved listing matching '{query}'"}
    r = rows[0]
    if action == "draft":
        agent_post.start([r["message_id"]], "draft")
        return {"started": f"draft for {r.get('place')}", "note": "about 2 minutes, check PropertyGuru posting page"}
    credits = 2 if r.get("listing_type") == "Rent" else 6
    preview = {"place": r.get("place"), "type": r.get("listing_type"), "price_rm": r.get("price_rm"),
               "credits": credits}
    if not _confirmed(st, turn, "post", r["message_id"], confirm):
        st["pending_action"] = {"kind": "post", "key": r["message_id"], "at": time.time()}
        return {"needs_confirm": True, "preview": preview,
                "say": "show this and ask Kelvin to confirm (yes) before posting"}
    agent_post.start([r["message_id"]], "post")
    st["pending_action"] = None
    return {"started": f"posting {r.get('place')} ({credits} credits)", "note": "about 2 minutes"}


def _confirmed(st, turn, kind, key, confirm):
    """confirm=true only counts when the same action was shown to Kelvin BEFORE his latest message."""
    p = st.get("pending_action") or {}
    return bool(confirm and p.get("kind") == kind and p.get("key") == key and p.get("at", 0) < turn)


def _ai_wants(days):
    import ai_call
    import ai_call_remote
    ticks = {**ai_call.load_ticks(), **ai_call_remote.load_ticks(_db())}
    since = (now() - timedelta(days=days or 1)).strftime("%Y-%m-%d %H:%M")
    rows = [r for r in ai_call.results() if (r.get("called") or "") >= since]
    wants = [r for r in rows if ticks.get(r["call_id"], r.get("verdict") == "interested")]
    return rows, wants


def _ai_results(days=1):
    rows, wants = _ai_wants(days)
    import ai_call
    latest = max((r.get("called") or "" for r in ai_call.results()), default="")
    return {"calls_checked": len(rows), "want": len(wants), "latest_checked_call": latest,
            "people": [{"phone": r["phone"], "called": r.get("called"), "said": (r.get("said") or "")[:80]}
                       for r in wants[:15]]}


def _ai_check(pages=1, quick=False):
    import ai_call_remote
    ai_call_remote.request_check(_db(), 1, max(1, min(int(pages or 1), 20)), bool(quick))
    return {"started": f"checking the newest {pages or 1} page(s) of AI calls", "note": "about 4 min a page"}


def _blast(st, turn, source, days=1, numbers="", message=None, confirm=False):
    import wa_blast
    if source == "ai_call_wants":
        nums = [{"phone": r["phone"], "name": None} for r in _ai_wants(days)[1]]
    else:
        nums = wa_blast.parse_numbers(re.split(r"[\n,;]+", numbers or ""))[0]
    done = wa_blast.recently_sent(30)
    nums = [n for n in nums if n["phone"] not in done]
    last = next((j for j in wa_blast.jobs() if not j.get("test") and j.get("sender") == "main"), None)
    msg = message or (last or {}).get("message")
    if not nums or not msg:
        return {"result": "nothing to blast" if not nums else "no message - tell me the message"}
    key = f"{source}:{','.join(n['phone'] for n in nums)}:{msg[:40]}"
    preview = {"numbers": len(nums), "first": [n["phone"] for n in nums[:5]], "message": msg,
               "skipped_recently_blasted": True}
    if not _confirmed(st, turn, "blast", key, confirm):
        st["pending_action"] = {"kind": "blast", "key": key, "at": time.time()}
        return {"needs_confirm": True, "preview": preview,
                "say": "show the count + message and ask Kelvin to confirm (yes) before sending"}
    kw = {k: last[k] for k in ("triggers", "delay", "daily_limit", "window") if last and last.get(k)}
    if last and last.get("rest"):
        kw["rest"] = (last["rest"].get("every", 0), last["rest"].get("minutes", 0))
    bid = wa_blast.create("main", nums, msg, **kw)
    st["pending_action"] = None
    return {"started": bid, "numbers": len(nums), "note": "same settings as the last blast"}


def _gmaps(query, max_places=50, split_kl=False):
    import gmaps
    areas = gmaps.AREAS["Kuala Lumpur"] if split_kl else None
    gmaps.start(query, int(max_places or (1000 if split_kl else 50)), True, areas)
    return {"started": query, "note": "a few minutes (much longer split by area) - ask for the results later"}


def _gmaps_results(query=None):
    import gmaps
    idx = sorted(gmaps.load_index(_db()), key=lambda j: -(j.get("at") or 0))
    j = next((x for x in idx if query and query.lower() in (x.get("query") or "").lower()), idx[0] if idx else None)
    if not j:
        return {"result": "no Google Maps searches yet"}
    job = gmaps.load_job(_db(), j["id"]) or {}
    rows = job.get("rows") or []
    return {"query": job.get("query"), "state": job.get("state"), "places": len(rows),
            "with_email": sum(1 for r in rows if r.get("email")), "mobiles": sum(1 for r in rows if r.get("whatsapp")),
            "some": [{"name": r.get("name"), "phone": r.get("phone"), "email": r.get("email")} for r in rows[:8]]}


def _pc_status():
    import agent_post
    import ai_call
    import gmaps
    import market_check
    import wa_client
    out = {}
    for name, s in (("ai_call", ai_call.status()), ("compare", market_check.status()),
                    ("propertyguru_posting", agent_post.status()), ("google_maps", gmaps.status())):
        out[name] = {k: s.get(k) for k in ("state", "text", "done", "total", "page", "call", "calls", "query")
                     if s.get(k) is not None}
    out["whatsapp"] = (wa_client.status() or {}).get("status")
    return out


def _photos(send_photos, query, how_many=5):
    """His saved listing's photos, else PropertyGuru photos of that condo -> sent to him on WhatsApp."""
    import urllib.request
    if not send_photos:
        return {"error": "can't send pictures from here"}
    n = max(1, min(int(how_many or 5), 10))
    rows = _find(query)
    photos = []
    if rows:
        r = rows[0]
        for p in [x for x in r.get("photo_paths") or [] if x.lower().endswith(".jpg")][:n]:
            try:
                photos.append((_db().storage.from_(BUCKET).download(p), ""))
            except Exception:
                pass
        if photos:
            photos[0] = (photos[0][0], f"{r.get('place')}" + (f" - {r.get('owner_name')}" if r.get("owner_name") else ""))
            threading.Thread(target=send_photos, args=(photos,), daemon=True).start()
            return {"sending": len(photos), "from": f"your saved listing: {r.get('place')}",
                    "more_photos_there": len(r.get("photo_paths") or []) - len(photos)}
    q = re.sub(r"[%,()]", " ", query).strip()
    pg = (_db().table("listings").select("title,thumbnail,price_rm,url").ilike("title", f"%{q}%")
          .not_.is_("thumbnail", "null").order("last_seen", desc=True).limit(n).execute().data)
    for x in pg:
        try:
            req = urllib.request.Request(x["thumbnail"], headers={"User-Agent": "Mozilla/5.0"})
            photos.append((urllib.request.urlopen(req, timeout=20).read(), f"{x['title']} - RM {x['price_rm']:,}"))
        except Exception:
            pass
    if not photos:
        return {"result": f"no pictures of '{query}' - not in his saved listings or the PropertyGuru listings on the PC"}
    threading.Thread(target=send_photos, args=(photos,), daemon=True).start()
    return {"sending": len(photos), "from": "PropertyGuru listings on the PC"}


def _dev(st, turn, task, confirm=False):
    """A change to his programs (dev_runner.py) - only what he confirmed: the request as it was shown to him."""
    import dev_runner
    if dev_runner.busy():
        s = dev_runner.status()
        return {"busy": True, "working_on": s.get("task"), "note": "one change at a time - wait for it to finish"}
    p = st.get("pending_action") or {}
    if confirm and p.get("kind") == "dev" and p.get("at", 0) < turn:
        dev_runner.start(p["task"])
        st["pending_action"] = None
        return {"started": p["task"], "done_yet": False,
                "say": "tell him you STARTED it (it is NOT done yet) - a few minutes, he gets a WhatsApp when it's "
                       "finished"}
    st["pending_action"] = {"kind": "dev", "key": "dev", "task": task, "at": time.time()}
    return {"needs_confirm": True, "will_do": task,
            "say": "say back what you'll change in one or two lines and ask him to confirm"}


def _dev_undo():
    import dev_runner
    if dev_runner.busy():
        return {"busy": True, "note": "a change is still running - undo after it finishes"}
    if not dev_runner.can_undo():
        return {"result": "nothing to undo"}
    dev_runner.start_undo()
    return {"undoing": dev_runner.status().get("task"), "note": "he gets a WhatsApp when it's back"}


def _dev_status():
    import dev_runner
    s = dev_runner.status()
    if not s:
        return {"result": "no changes asked yet"}
    words = {"running": "still working on it", "done": "finished - the change is in",
             "undone": "was undone - the old version is back", "error": "failed - nothing changed"}
    when = datetime.fromtimestamp(s.get("at") or 0, MYT).strftime("%d %b %I:%M %p")
    return {"request": s.get("task"), "state": words.get(s.get("state"), s.get("state")), "since": when,
            "files_changed": s.get("changed"), "summary": s.get("summary"), "error": s.get("error")}


def _pg_overview():
    import agent_post
    res = agent_post.load_results(_db())
    pick = lambda state: [{"place": r.get("place"), "headline": r.get("headline"),  # noqa: E731
                           "when": datetime.fromtimestamp(r.get("posted_at") or r.get("at") or 0, MYT)
                           .strftime("%d %b"), "problem": r.get("error")} for r in res.values()
                          if r.get("state") == state]
    s = agent_post.status()
    return {"drafts_waiting": pick("draft"), "posted": pick("posted"), "to_check": pick("check"),
            "problems": pick("error"), "credits_left": s.get("credits"),
            "running_now": s.get("text") if s.get("state") == "running" else None}


def _web(query):
    """Free internet search (DuckDuckGo) - top results."""
    import html
    import urllib.parse
    import urllib.request
    req = urllib.request.Request("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query), headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 "
                      "Safari/537.36"})
    page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    clean = lambda x: html.unescape(re.sub(r"<[^>]+>", "", x)).strip()  # noqa: E731
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    out = []
    for (url, title), snip in list(zip(links, snips))[:6]:
        real = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(url)).query).get("uddg", [url])[0]
        out.append({"title": clean(title), "snippet": clean(snip)[:300], "link": real})
    return out or {"result": "nothing found"}


EMOJI = re.compile("[🀀-🫿☀-➿⬀-⯿️‍⃣]+")


def no_emoji(text):
    """Kelvin doesn't want emojis (stickers instead) - strip any the AI still put in."""
    return re.sub(r"[ 	]{2,}", " ", EMOJI.sub("", text or "")).strip()


LINK = re.compile(r"https?://\S+|\b(?:www\.)?[\w-]+(?:\.[\w-]+)*\.(?:com|my|net|org|io|sg|co|gov|edu|ai|app)\b(?:/\S*)?",
                  re.I)


def _tidy(text):
    """The AI sometimes repeats itself - each sentence once. No emojis."""
    text = no_emoji(text)
    # web links stay whole; a sentence ends at . ! ? followed by a space or a letter (never a digit: "6.93"), or at
    # Chinese 。！？
    links = []
    text = LINK.sub(lambda m: links.append(m.group(0)) or f"\x00{len(links) - 1}\x00", text.strip())
    parts, seen = re.split(r"(?<=[.!?])\s+|(?<=[.!?])(?=[A-Za-z])|(?<=[。！？])", text), set()
    parts = [re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], p) for p in parts]
    out = []
    for p in parts:
        key = re.sub(r"\W+", "", p.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return " ".join(out).strip()


HIS_DATA = {"whatsapp_listings", "find_saved_listing", "search_pg_listings", "condo_price", "propertyguru_posts",
            "propertyguru_overview", "ai_call_results", "blast_status", "leads_today", "google_maps_results",
            "call_progress"}


def _made_up(text, real, prices=True):
    """Phone numbers (always) / prices (when his own data was looked up) in a reply that appear nowhere in the tool
    results or the chat -> invented."""
    digits_real = re.sub(r"\D", "", real)
    out = []
    for m in re.finditer(r"\+?\d[\d\s-]{6,}\d", text):  # phone numbers
        d = re.sub(r"\D", "", m.group(0))
        if d[-8:] not in digits_real:
            out.append(m.group(0))
    for m in re.finditer(r"(?:rm\s?)?(\d+(?:[.,]\d+)?)\s?(k|m|mil)\b", text, re.I) if prices else []:  # 1.8M, 450k
        v = float(m.group(1).replace(",", "")) * {"k": 1e3, "m": 1e6, "mil": 1e6}[m.group(2).lower()]
        if str(int(v)) not in digits_real and m.group(1).replace(",", "") not in real:
            out.append(m.group(0))
    return out


# ---------- photos you send: read once by the free AI that can see pictures, then answered like text ----------
PHOTO = re.compile(r"\[sent a photo: ([^\]]+)\]")
SEEN = ROOT / "data" / "assistant_photos" / "_seen.json"  # photo -> what was in it (each photo read once)
LOOK = ("Say what this picture is - anything at all (food, receipt, document, product, place, person, pet, "
        "screenshot of any app or chat, error message, property photo...) and describe what matters in it. Copy "
        "out ALL the text and numbers in it exactly (prices, phone numbers, names, dates, amounts, addresses, unit "
        "numbers). If it's a property: room, furnishing, condition, view. Short and factual, no guessing. His "
        "caption / question: ")


def look(path, caption=""):
    """One photo -> what's in it (Qwen on Groq - the free model that can see pictures)."""
    import base64
    import os
    import httpx
    from furnish_check import _small_jpeg
    img = _small_jpeg((ROOT / path).read_bytes(), 1280)  # big enough to read screenshot text
    content = [{"type": "text", "text": LOOK + (caption or "(none)")},
               {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}]
    for _ in range(3):
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions", timeout=120, json={
            "model": "qwen/qwen3.8-27b", "temperature": 0, "max_tokens": 1500,
            "messages": [{"role": "user", "content": content}]},
            headers={"Authorization": f"Bearer {os.environ.get('BOT_LLM_API_KEY', '')}", "User-Agent": "KLTracker/1.0"})
        if r.status_code == 429:
            if "per day" in r.text.lower():
                return "(couldn't look at it - today's free picture allowance is used up)"
            time.sleep(min(30, float(r.headers.get("retry-after") or 5)))
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()[:2500]
    return "(couldn't look at it - the picture AI is busy)"


def with_photos(history):
    """Your photos in the chat -> 'what's in it' text the main AI can work with (read once, remembered)."""
    try:
        seen = json.loads(SEEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    out, changed = [], False
    for m in history:
        hit = PHOTO.search(m["content"]) if m["role"] == "user" else None
        if hit:
            path, caption = hit.group(1), PHOTO.sub("", m["content"]).strip()
            if path not in seen and (ROOT / path).exists():
                try:
                    seen[path] = look(path, caption)
                except Exception as e:
                    seen[path] = f"(couldn't look at it: {str(e)[:100]})"
                changed = True
            m = {**m, "content": f"[he sent a photo, saved at {path}. In it: {seen.get(path, '(gone)')}]"
                                  + (f" His caption: {caption}" if caption else "")}
        out.append(m)
    if changed:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    return out


def reply(history, send, send_photos=None):
    """Your message -> the assistant's answer (tools read / change this PC's data). history: wa_bot's log."""
    import bot_brain
    st = load()
    n = now()
    turn = time.time()  # a confirm only counts for an action shown before this message
    w = st.get("waiting")
    if w:
        w["answered"] = True
    # after chatting, the next reminder waits a while (30 min - 3 h, random) - never straight after your message
    st["quiet_until"] = turn + random.randint(30, 180) * 60
    tasks = [{k: t.get(k) for k in ("id", "title", "kind", "status", "due", "note")} for t in st["tasks"]]
    system = (STYLE + f"\n\nNow: {n:%A %d %B %Y, %I:%M %p} (Malaysia). Your reminders: "
              f"{json.dumps(tasks, ensure_ascii=False)}. "
              + (f"The last thing you asked him about: {w['task']} ('{w.get('text')}'). " if w else "")
              + "When he answers about a reminder, record it with update_task (done / not_yet / cancel; for "
                "monthly ones pass started_on if he says when it starts). He can ask you ANYTHING, not only "
                "property - everyday questions, translate, calculate, explain, advice, his photos of anything: "
                "answer like a smart friend (web_search for current facts, news, prices of things). For HIS data - "
                "listings, blasts, condo prices, PropertyGuru posts, AI calls, leads - use the tools and ONLY say "
                "what a tool returned in this chat; if it returned nothing / 'no ...', say there's none. NEVER make "
                "up his listings, owners or phone numbers. Posting on PropertyGuru and starting a blast need his clear yes AFTER "
                "you showed him the preview. Answer ONLY what he asked - never bring up other reminders or ask "
                "about them (they're sent separately, later). If the PC's data doesn't have it, use web_search. "
                "When he sends a photo you get what's in it as text - answer about it; if he wants something in "
                "his programs fixed or changed from a screenshot, put the photo's saved path in the dev_request "
                "task. Reply short, in the same casual style.")
    impl = {
        "update_task": lambda **a: _update_task(st, **a), "add_task": lambda **a: _add_task(st, **a),
        "list_tasks": lambda: st["tasks"], "whatsapp_listings": lambda days=1: _listings(days),
        "scan_listing_group": lambda days=1: _scan(days, send), "blast_status": _blasts,
        "call_progress": lambda: __import__("ai_call_watch").progress(),
        "blast_control": lambda action, blast_id=None: _blast_control(blast_id, action),
        "condo_price": lambda name: bot_brain.condo_price(name), "propertyguru_posts": lambda day: _posts(day),
        "leads_today": lambda: {"count": len(leads_today()), "places": [r["place"] for r in leads_today()][:15]},
        "search_pg_listings": lambda **a: bot_brain.search_listings(**a), "find_saved_listing": _listing_detail,
        "compare_listing": _compare, "propertyguru_listing": lambda **a: _pg_listing(st, turn, **a),
        "ai_call_results": _ai_results, "ai_call_check": _ai_check,
        "start_blast": lambda **a: _blast(st, turn, **a), "google_maps": _gmaps,
        "google_maps_results": _gmaps_results, "pc_status": _pc_status,
        "propertyguru_overview": _pg_overview, "web_search": _web,
        "send_photos": lambda **a: _photos(send_photos, **a),
        "dev_request": lambda **a: _dev(st, turn, **a), "dev_undo": _dev_undo, "dev_status": _dev_status}
    history = with_photos(history[-12:])
    messages = [{"role": "system", "content": system}] + history
    try:
        # seen: everything the tools returned - numbers in the reply must come from it; mine: his own data was
        # looked up this time (then its prices must be real too - a general answer may quote prices it knows)
        seen, retried, mine = "", False, False
        for _ in range(6):
            msg = bot_brain._llm(messages, tools=TOOLS + MORE_TOOLS, temperature=0.3, max_tokens=700)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip() or "ok noted 👍"
                made_up = _made_up(text, seen + json.dumps(history[-12:], ensure_ascii=False),
                                   prices=bool(mine))
                if made_up and not retried:  # once: tell it which details weren't real and let it rewrite
                    retried = True
                    messages += [{"role": "assistant", "content": text},
                                 {"role": "user", "content": (
                                     f"(system check, not Kelvin: {', '.join(made_up)} in your reply did not come "
                                     "from any tool result. Rewrite the reply using ONLY what the tools returned.)")}]
                    continue
                if made_up:  # still made up after the rewrite - don't send invented details
                    text = "hmm i check got nothing solid on that leh, nothing to report yet"
                save(st)
                return _tidy(text)
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    result = impl[c["function"]["name"]](**args)
                except Exception as e:
                    result = {"error": str(e)[:200]}
                out = json.dumps(result, ensure_ascii=False, default=str)[:6000]
                seen += out
                mine = mine or c["function"]["name"] in HIS_DATA
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        save(st)
        return "ok noted, i check again later ya"
    except Exception as e:
        save(st)
        print("assistant AI failed:", str(e)[:300], flush=True)
        return "aiya my brain jam a bit, try again later ah 😅"
