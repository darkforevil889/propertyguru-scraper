"""Keeps the PC-side parts of KL Tracker running (started at Windows logon by a Startup-folder shortcut):
  * the dashboard (http://localhost:8501) - started if nothing is on that port yet
  * the WhatsApp connector (wa_service.py) and the chat bot (wa_bot.py) - only once each has been linked by QR,
    so scheduled blasts and auto-replies keep working after a restart / crash.
  * blasts made on the online dashboard: copied here every 15 s to be sent, progress copied back (blast_remote.py).
Checks every 2 minutes. Run with pythonw.exe so no window shows. Log: logs/keeper.log
"""
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
LOGS = ROOT / "logs"
sys.path.insert(0, str(ROOT))


def log(msg):
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / "keeper.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def listening(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_dashboard():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "streamlit.exe"), "run", "app.py",
                      "--server.headless", "true"], cwd=ROOT, creationflags=flags,
                     stdout=open(LOGS / "dashboard.log", "a"), stderr=subprocess.STDOUT)


def blast_sync_loop():
    """Every 15 s: copy blasts made on the online dashboard to this PC (and progress back); heartbeat every 30 s.
    See blast_remote.py."""
    import importlib
    import os
    from dotenv import load_dotenv
    from supabase import create_client
    import blast_remote
    import wa_blast
    load_dotenv(ROOT / ".env")
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    import ai_call_remote
    import bot_remote
    import wa_scan
    n, last_error, last_ai_error, last_wa_error, last_bot_error = 0, None, None, None, None
    while True:
        try:
            importlib.reload(wa_blast)  # pick up code changes without restarting the keeper
            importlib.reload(blast_remote)
            if n % 2 == 0:
                blast_remote.heartbeat(db)
            pulled, pushed = blast_remote.sync(db)
            if pulled or pushed:
                log(f"blast sync: {pulled} new from online, {pushed} sent up")
            last_error = None
        except Exception as e:
            if str(e) != last_error:  # e.g. tables not created yet - log once, keep trying
                log(f"blast sync problem: {str(e)[:300]}")
                last_error = str(e)
        try:  # AI call: checked calls up to Supabase every minute, "Check calls" pressed online every 15 s
            importlib.reload(ai_call_remote)
            ai_call_remote.run_requests(db, start_ai_check)
            if n % 20 == 3:  # every 5 minutes
                ai_backlog()
            if n % 4 == 0:
                sent = ai_call_remote.sync(db)
                if sent:
                    log(f"ai call sync: {sent} calls sent up")
            last_ai_error = None
        except Exception as e:
            if str(e) != last_ai_error:
                log(f"ai call sync problem: {str(e)[:300]}")
                last_ai_error = str(e)
        try:  # WhatsApp page: chat list up every minute, scans / settings asked for online every 15 s
            importlib.reload(wa_scan)
            wa_scan.run_requests(db, log)
            if n % 4 == 1:
                wa_scan.sync_chats(db)
            last_wa_error = None
        except Exception as e:
            if str(e) != last_wa_error:
                log(f"whatsapp page sync problem: {str(e)[:300]}")
                last_wa_error = str(e)
        if n % 240 == 7:  # hourly: furnishing for saved listings that still don't have it (see furnish_check.py)
            try:
                import furnish_check
                importlib.reload(furnish_check)
                furnish_check.run(db, log=log)
            except Exception as e:
                if "allowance" not in str(e):
                    log(f"furnishing check problem: {str(e)[:200]}")
        try:  # Chat bot page: changes / test questions from online every 15 s, status + conversations every 30 s
            importlib.reload(bot_remote)
            if not bot_remote.run_requests(db, log) and n % 2 == 0:
                bot_remote.sync(db)
            last_bot_error = None
        except Exception as e:
            if str(e) != last_bot_error:
                log(f"chat bot page sync problem: {str(e)[:300]}")
                last_bot_error = str(e)
        n += 1
        time.sleep(15)


def ai_backlog():
    """A long AI call run (data/ai_call/backlog.json, e.g. the old text-only pages) that the free daily AI allowance
    can't finish in one go: start it again once the allowance has had time to reset, until it reaches the end."""
    import json
    import ai_call
    path = ai_call.OUT / "backlog.json"
    try:
        b = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not b.get("active"):
        return
    s = ai_call.status()
    if s.get("state") == "running" and time.time() - s.get("at", 0) < 600:
        return  # a check is running (this one or yours)
    if s.get("state") == "done" and s.get("start") == b["start"] and s.get("pages") == b["pages"] \
            and s.get("mode") == b.get("mode") and s.get("at", 0) > b.get("started", 0):
        b["active"], b["finished"] = False, time.time()
        ai_call._save(path, b)
        log(f"ai call backlog finished: pages {b['start']}-{b['start'] + b['pages'] - 1}")
        return
    if s.get("state") == "error" and "allowance" in (s.get("error") or "") and time.time() - s.get("at", 0) < 3 * 3600:
        return  # today's free AI allowance is used up - try again a few hours later
    b["started"] = time.time()
    ai_call._save(path, b)
    start_ai_check(b["start"], b["pages"], text_only=b.get("mode") == "text", quick=b.get("mode") == "quick")


def start_ai_check(start, pages, quick=False, text_only=False):
    """Run ai_call.py in the background (asked for on the online dashboard, or the backlog)."""
    import ai_call
    ai_call.OUT.mkdir(parents=True, exist_ok=True)
    ai_call._save(ai_call.STATUS, {"state": "running", "at": time.time(), "page": start, "call": 0,
                                   "calls": "?", "of_pages": "?", "phone": "logging in…"})
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(python), "-u", str(ROOT / "ai_call.py"), "--pages", str(pages), "--start", str(start)]
                     + (["--quick"] if quick else []) + (["--text"] if text_only else []), cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=open(LOGS / "ai_call.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    log(f"ai call {'text-only ' if text_only else 'quick ' if quick else ''}check started: page {start}, "
        f"{pages} page(s)")


def main():
    import threading

    import bot_client
    import wa_client
    threading.Thread(target=blast_sync_loop, daemon=True).start()
    services = [("WhatsApp connector", wa_client, ROOT / "wa_data" / "session.sqlite3"),
                ("chat bot", bot_client, ROOT / "wa_bot_data" / "bot_session.sqlite3")]
    needs_qr = set()  # logged out -> wait for you to link again on the dashboard instead of looping QR codes
    log("keeper started")
    while True:
        try:
            if not listening(8501):
                log("starting dashboard")
                start_dashboard()
            for name, mod, session in services:
                s = mod.status()
                if s:
                    if s.get("status") in ("qr", "logged_out"):
                        needs_qr.add(name)
                    elif s.get("status") == "ready":
                        needs_qr.discard(name)
                    continue
                if session.exists() and name not in needs_qr:
                    log(f"starting {name}")
                    try:
                        mod.ensure_running()
                    except Exception as e:
                        log(f"{name} didn't start: {e}")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(120)


if __name__ == "__main__":
    main()
