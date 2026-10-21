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
    n, last_error = 0, None
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
        n += 1
        time.sleep(15)


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
