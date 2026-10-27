"""Price filter for the (new) online dashboard: it asks for PropertyGuru price ranges of some properties through
pc_tasks 'price_filter' {pids: [...]}; this PC fetches them in Chrome (PropertyGuru blocks servers) and saves
{pid: {"rent": [lo, hi], "sale": [lo, hi], "at": ...}} plus progress in Supabase storage owner-photos/data/price_filter.json.

PC side: run_requests(db) is called every ~30 s (bot_remote.sync, which keeper.py reloads each round) and starts
`python price_filter_remote.py --task <json>` in the background - one at a time.
"""
import json
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
BUCKET, PATH = "owner-photos", "data/price_filter.json"
LOCK = ROOT / "data" / "price_filter.running"


def load(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(PATH))
    except Exception:
        return {"results": {}, "status": {}}


def _save(db, data):
    db.storage.from_(BUCKET).upload(PATH, json.dumps(data, ensure_ascii=False).encode(),
                                    {"content-type": "application/json", "upsert": "true"})


def run_requests(db, log=print):
    """Start the oldest waiting request (if none is running)."""
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 1800:
        return
    tasks = db.table("pc_tasks").select("id,params").eq("kind", "price_filter").eq("status", "pending") \
        .order("id").limit(1).execute().data
    if not tasks:
        return
    t = tasks[0]
    db.table("pc_tasks").update({"status": "started"}).eq("id", t["id"]).execute()
    LOCK.parent.mkdir(exist_ok=True)
    LOCK.write_text(str(time.time()))
    (ROOT / "logs").mkdir(exist_ok=True)
    subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-u", str(ROOT / "price_filter_remote.py"),
                      "--task", json.dumps({**(t.get("params") or {}), "_task": t["id"]})], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=open(ROOT / "logs" / "price_filter.log", "a"), stderr=subprocess.STDOUT)
    log(f"price filter: checking {len((t.get('params') or {}).get('pids') or [])} properties")


def run(params):
    import tempfile

    from patchright.sync_api import sync_playwright

    import bot_brain
    import price_lookup as pl
    import scraper
    db = bot_brain._db()
    pids = list(dict.fromkeys(int(p) for p in params.get("pids") or []))
    data = load(db)
    data.setdefault("results", {})
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(tempfile.mkdtemp(), channel="chrome", headless=False,
                                                       no_viewport=True)
            page = ctx.pages[0]

            def fetch(url):
                d = scraper.load_next_data(page, url)
                time.sleep(random.uniform(1.5, 3))
                return d

            for i, pid in enumerate(pids):
                data["status"] = {"state": "running", "done": i, "total": len(pids), "at": time.time()}
                _save(db, data)
                try:
                    data["results"][str(pid)] = {**{k: list(pl.price_range(fetch, pid, k)) for k in ("rent", "sale")},
                                                 "at": time.time()}
                except Exception as e:
                    data["results"][str(pid)] = {"error": str(e).splitlines()[0][:200], "at": time.time()}
            ctx.close()
        data["status"] = {"state": "done", "done": len(pids), "total": len(pids), "at": time.time()}
    except Exception as e:
        data["status"] = {"state": "error", "error": str(e).splitlines()[0][:200], "at": time.time()}
    _save(db, data)
    if params.get("_task"):
        state = "done" if data["status"]["state"] == "done" else "failed"
        db.table("pc_tasks").update({"status": state}).eq("id", params["_task"]).execute()
    LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--task":
        run(json.loads(sys.argv[2]))
