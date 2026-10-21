"""AI call on the online dashboard.

The checking itself needs the PC (ipline login + listening). So the PC copies every checked call to Supabase
(table ai_calls, recordings in the private bucket owner-photos/ai_call/<id>.mp3) and the online page reads them
from there. Your ✔ Keep ticks live in ai_calls.keep, so they're the same on the PC and online. Pressing
"Check calls" online adds a row to pc_tasks; the PC (keeper.py) starts the check and reports progress in its
heartbeat (pc_status).

Tables: schema_ai_call_online.sql (run once in Supabase -> SQL Editor).
"""
import hashlib
import json
import time

BUCKET = "owner-photos"
SYNCED = None  # set from ai_call.OUT on first use: data/ai_call/synced.json


def _audio_path(call_id):
    return f"ai_call/{call_id}.mp3"


# ---------- used by the dashboard (PC and online) ----------
def load_rows(db):
    """Every checked call from Supabase (newest call first) - same fields as ai_call.results()."""
    rows, start = [], 0
    while True:
        batch = (db.table("ai_calls").select("call_id,data").order("call_id", desc=True)
                 .range(start, start + 999).execute().data)
        rows += [{"call_id": r["call_id"], **(r.get("data") or {})} for r in batch]
        if len(batch) < 1000:
            break
        start += 1000
    return sorted(rows, key=lambda r: r.get("called") or "", reverse=True)


def load_ticks(db):
    """{call_id: True / False} - only calls you ticked or unticked yourself."""
    out, start = {}, 0
    while True:
        batch = (db.table("ai_calls").select("call_id,keep").not_.is_("keep", "null")
                 .range(start, start + 999).execute().data)
        out.update({r["call_id"]: bool(r["keep"]) for r in batch})
        if len(batch) < 1000:
            return out
        start += 1000


def save_ticks(db, ticks):
    """{call_id: True / False} -> Supabase (for calls already copied there - the PC keeps its own copy too)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for value in (True, False):
        ids = [cid for cid, keep in ticks.items() if bool(keep) == value]
        for i in range(0, len(ids), 200):
            db.table("ai_calls").update({"keep": value, "keep_at": now}).in_("call_id", ids[i:i + 200]).execute()


def audio_url(db, call_id, ttl=3600):
    try:
        r = db.storage.from_(BUCKET).create_signed_url(_audio_path(call_id), ttl)
        return r.get("signedURL") or r.get("signedUrl")
    except Exception:
        return None


def request_check(db, start_page, pages):
    db.table("pc_tasks").insert({"kind": "ai_call", "params": {"start": int(start_page), "pages": int(pages)}}).execute()


def pending_requests(db):
    return db.table("pc_tasks").select("id,params,created_at").eq("kind", "ai_call").eq("status", "pending") \
        .execute().data


# ---------- PC side (keeper.py) ----------
def _fingerprint(row):
    keep = {k: row.get(k) for k in ("verdict", "reason", "said", "phone", "called", "listen", "rules")}
    return hashlib.md5(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def sync(db):
    """Copy new / changed checked calls (and their recordings) up to Supabase. Returns how many were sent."""
    import ai_call
    path = ai_call.OUT / "synced.json"
    try:
        done = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    calls, audio = done.setdefault("calls", {}), set(done.setdefault("audio", []))
    sent, batch = 0, []
    for r in ai_call.results():
        cid, fp = r["call_id"], _fingerprint(r)
        if calls.get(cid) != fp:
            data = {k: r.get(k) for k in ("phone", "called", "seconds", "verdict", "said", "reason", "turns", "heard",
                                          "full", "ipline", "page")}
            batch.append(({"call_id": cid, "data": data}, cid, fp))
        if cid not in audio and (f := ai_call.audio_file(cid)):
            db.storage.from_(BUCKET).upload(_audio_path(cid), f.read_bytes(),
                                            {"content-type": "audio/mpeg", "upsert": "true"})
            audio.add(cid)
    local_ticks = ai_call.load_ticks()
    for i in range(0, len(batch), 100):
        part = batch[i:i + 100]
        db.table("ai_calls").upsert([row for row, _, _ in part], on_conflict="call_id").execute()
        new = {cid: local_ticks[cid] for _, cid, _ in part if cid not in calls and cid in local_ticks}
        if new:  # ticked on the PC before the call was copied up
            save_ticks(db, new)
        for _, cid, fp in part:
            calls[cid] = fp
        sent += len(part)
    if not done.get("ticks_moved"):  # ticks made before ticks lived in Supabase
        if local_ticks:
            save_ticks(db, local_ticks)
        done["ticks_moved"] = True
    done["audio"] = sorted(audio)
    ai_call._save(path, done)
    return sent


def run_requests(db, start_check):
    """Start checks asked for on the online page. start_check(start, pages) launches ai_call.py on this PC."""
    import ai_call
    for task in pending_requests(db):
        s = ai_call.status()
        if s.get("state") == "running" and time.time() - s.get("at", 0) < 600:
            return  # one check at a time - the request waits until this one is done
        p = task.get("params") or {}
        start_check(int(p.get("start") or 1), int(p.get("pages") or 1))
        db.table("pc_tasks").update({"status": "started"}).eq("id", task["id"]).execute()
        return
