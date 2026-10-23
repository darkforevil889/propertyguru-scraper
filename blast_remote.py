"""WhatsApp blast from the online dashboard.

Streamlit Cloud can't reach your WhatsApp - only the PC can. So the online page saves blasts in Supabase (table
blast_jobs, attachments in the private storage bucket owner-photos/blast/<id>/), and the PC (keeper.py -> sync()
every few seconds) copies them into wa_blast/ where the normal sender picks them up. Progress, and pause / cancel
from either side, are copied back the same way. The PC also writes a heartbeat (table pc_status) so the online
page can show whether it's on and which WhatsApp numbers are connected.

Tables: schema_blast_online.sql (run once in Supabase -> SQL Editor).
"""
import time

import wa_blast

BUCKET = "owner-photos"


def _path(bid, name):
    return f"blast/{bid}/{name}"


class RemoteStore:
    """Same calls as wa_blast (create / jobs / set_control / ...), but kept in Supabase for the online dashboard.
    Anything not defined here (parse_numbers, full_text, state_of, ...) comes straight from wa_blast."""

    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(wa_blast, name)

    def create(self, sender, numbers, message, attachments=(), **kw):
        spec = wa_blast.new_spec(sender, numbers, message, [wa_blast.safe_name(n) for n, _ in attachments], **kw)
        for name, data in attachments:
            self.db.storage.from_(BUCKET).upload(_path(spec["id"], wa_blast.safe_name(name)), data,
                                                 {"upsert": "true"})
        self.db.table("blast_jobs").insert({"id": spec["id"], "spec": spec, "progress": {}}).execute()
        return spec["id"]

    def jobs(self):
        rows = (self.db.table("blast_jobs").select("spec,progress").order("created_at", desc=True).limit(300)
                .execute().data)
        return [{**r["spec"], "progress": r.get("progress") or {}} for r in rows if r.get("spec")]

    def set_control(self, bid, control, at=None):
        row = self.db.table("blast_jobs").select("spec").eq("id", bid).single().execute().data
        spec = {**row["spec"], "control": control, "control_at": at or time.time()}
        self.db.table("blast_jobs").update({"spec": spec}).eq("id", bid).execute()

    def recently_sent(self, days, all_jobs=None):
        return wa_blast.recently_sent(days, self.jobs() if all_jobs is None else all_jobs)

    def attachments_of(self, job):
        out = []
        for name in job.get("files") or []:
            try:
                out.append((name, self.db.storage.from_(BUCKET).download(_path(job["id"], name))))
            except Exception:
                pass
        return out

    def pc_status(self):
        """{'at': last heartbeat, 'main' / 'bot': {'status', 'number'}, 'my_numbers': [...]} or {}."""
        rows = self.db.table("pc_status").select("data").eq("name", "pc").execute().data
        return rows[0]["data"] if rows else {}


# ---------- PC side (keeper.py) ----------
def heartbeat(db):
    """Tell the online page this PC is on, and which WhatsApp numbers are connected."""
    import bot_client
    import wa_client
    data = {"at": time.time(), "my_numbers": wa_client.load_settings().get("my_numbers") or []}
    for key, mod in (("main", wa_client), ("bot", bot_client)):
        s = mod.status() or {}
        data[key] = {"status": s.get("status") or "off", "number": (s.get("me") or {}).get("number")}
    try:  # AI call progress, shown on the online AI call page
        import ai_call
        data["ai_call"] = ai_call.status()
    except Exception:
        pass
    try:  # WhatsApp scan progress, shown on the online WhatsApp page
        import wa_scan
        data["wa_scan"] = wa_scan.status()
    except Exception:
        pass
    try:  # PropertyGuru comparison progress, shown on the online Saved listings tab
        import market_check
        data["market_check"] = market_check.status()
    except Exception:
        pass
    try:  # PropertyGuru posting (AgentNet) progress, shown on the online Saved listings tab
        import agent_post
        data["agent_post"] = agent_post.status()
    except Exception:
        pass
    try:  # Google Map Extractor progress, shown on the online page
        import gmaps
        data["gmaps"] = gmaps.status()
    except Exception:
        pass
    db.table("pc_status").upsert({"name": "pc", "data": data}).execute()


def sync(db):
    """Copy blasts between Supabase and wa_blast/ - new ones both ways, progress up, the newest pause / cancel
    whichever side it came from. Returns a short summary for the log."""
    remote = {r["id"]: r for r in db.table("blast_jobs").select("id,spec,progress").execute().data}
    local = {j["id"]: j for j in wa_blast.jobs()}
    pulled = pushed = 0

    for bid, r in remote.items():  # online -> PC
        spec = r.get("spec") or {}
        if not spec.get("id"):
            continue
        if bid not in local:
            folder = wa_blast.FILES / bid
            for name in spec.get("files") or []:
                if not (folder / name).exists():
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / name).write_bytes(db.storage.from_(BUCKET).download(_path(bid, name)))
            wa_blast.DIR.mkdir(exist_ok=True)
            wa_blast._write(wa_blast.DIR / f"{bid}.json", spec)
            pulled += 1
        elif (spec.get("control_at") or 0) > (local[bid].get("control_at") or 0):
            wa_blast.set_control(bid, spec["control"], at=spec["control_at"])

    for bid, job in local.items():  # PC -> online
        progress = job.pop("progress", {}) or {}
        r = remote.get(bid)
        if r is None:
            for name, data in wa_blast.attachments_of(job):
                db.storage.from_(BUCKET).upload(_path(bid, name), data, {"upsert": "true"})
            db.table("blast_jobs").insert({"id": bid, "spec": job, "progress": progress}).execute()
            pushed += 1
            continue
        change = {}
        if progress != (r.get("progress") or {}):
            change["progress"] = progress
        if (job.get("control_at") or 0) > ((r.get("spec") or {}).get("control_at") or 0):
            change["spec"] = job
        if change:
            db.table("blast_jobs").update(change).eq("id", bid).execute()
    return pulled, pushed
