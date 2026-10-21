"""Keeps the PC-side parts of KL Tracker running (started at Windows logon by a Startup-folder shortcut):
  * the dashboard (http://localhost:8501) - started if nothing is on that port yet
  * the WhatsApp connector (wa_service.py) and the chat bot (wa_bot.py) - only once each has been linked by QR,
    so scheduled blasts and auto-replies keep working after a restart / crash.
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


def main():
    import bot_client
    import wa_client
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
