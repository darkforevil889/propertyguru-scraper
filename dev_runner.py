"""Change KL Tracker by WhatsApp: your request -> Claude Code on this PC makes the change.

Started by the assistant (assistant.py, tool dev_request) after you confirm; runs as its own process so it carries
on even while the bot restarts. Steps:
  1. backup of every program file (data/dev_backups/<id>/) - "undo" puts it back
  2. Claude Code (the copy inside the VS Code extension, your Claude login) makes the change - it may read and
     edit the project's files and check them, but can't push, commit or touch .env (your passwords)
  3. every changed Python file must still compile - if not, the backup goes back automatically
  4. restarts what changed (dashboard / WhatsApp bot), then WhatsApps you a plain-words summary
Status: data/dev_status.json. One request at a time.

    python dev_runner.py --task "add a furnish filter to Saved listings"
    python dev_runner.py --undo
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
BACKUPS = ROOT / "data" / "dev_backups"
STATUS = ROOT / "data" / "dev_status.json"
CODE = ["*.py", "*.sql", "*.md", "requirements*.txt", ".gitignore"]  # the program files that get backed up
TIMEOUT = 40 * 60

RULES = """You are changing Kelvin's "KL Tracker" project ({root}) because he asked by WhatsApp (he isn't
technical). His request: "{task}"

Rules:
- Do what he asked, keep the change small, match how the surrounding code is written.
- Never run git commit / git push, never read or change .env (passwords) or anything under .chrome-*, wa_data,
  wa_bot_data.
- After editing Python files, check them with: .venv/Scripts/python.exe -m py_compile <file>
- Don't start or stop services - that's done for you afterwards.
- If the request is unclear or would be risky (deleting data, sending messages to people, spending credits),
  don't do it - explain why instead.
- End with a short summary for Kelvin in plain words (max 8 lines): what changed and how to use it. No code."""


def _save(obj):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def busy():
    s = status()
    return s.get("state") == "running" and time.time() - s.get("at", 0) < TIMEOUT + 300


def claude_exe():
    """The newest Claude Code that came with the VS Code extension."""
    found = sorted((Path.home() / ".vscode" / "extensions").glob("anthropic.claude-code-*/resources/native-binary/"
                                                                 "claude.exe"),
                   key=lambda p: [int(x) for x in re.findall(r"\d+", p.parts[-4])[:3]])
    if not found:
        raise RuntimeError("Claude Code isn't on this PC (the VS Code extension's copy wasn't found)")
    return found[-1]


def _files():
    out = {}
    for pattern in CODE:
        for f in ROOT.glob(pattern):
            if f.is_file():
                out[f.name] = hashlib.sha1(f.read_bytes()).hexdigest()
    return out


def backup(job_id):
    folder = BACKUPS / job_id
    folder.mkdir(parents=True, exist_ok=True)
    before = _files()
    for name in before:
        shutil.copy2(ROOT / name, folder / name)
    (folder / "_files.json").write_text(json.dumps(before))
    return before


def restore(job_id):
    """Put the programs back as they were before this request (files it added are removed)."""
    folder = BACKUPS / job_id
    before = json.loads((folder / "_files.json").read_text())
    for name in before:
        shutil.copy2(folder / name, ROOT / name)
    for name in _files():
        if name not in before and name.endswith(".py"):
            (ROOT / name).unlink()


def tell(text):
    """WhatsApp you through the bot (retries while it restarts)."""
    sys.path.insert(0, str(ROOT))
    import bot_client
    for _ in range(18):
        try:
            if bot_client.assistant_say(text).get("ok"):
                return
        except Exception:
            pass
        time.sleep(10)


def restart(changed):
    """Restart what uses the changed files: the dashboard and / or the WhatsApp bot (keeper.py starts them again)."""
    import socket
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = []
    if any(n.endswith(".py") for n in changed):  # the dashboard uses (almost) every module
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='streamlit.exe'\" | ForEach-Object "
                        "{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"], creationflags=flags)
        with socket.socket() as s:  # start it again now rather than waiting for keeper
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", 8501)) != 0:
                subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "streamlit.exe"), "run", "app.py",
                                  "--server.headless", "true"], cwd=ROOT, creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        done.append("dashboard")
    if any(n in changed for n in BOT_FILES):
        done.append("WhatsApp bot")  # restarted last, after the summary is sent (restart_bot)
    return done


BOT_FILES = ("wa_bot.py", "assistant.py", "bot_brain.py", "wa_blast.py", "bot_client.py", "reply_approval.py",
             "smart_reply.py")


def restart_bot():
    """Stop the WhatsApp bot - keeper.py starts it again within 2 minutes with the new code."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'wa_bot\\.py' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run(task):
    job_id = time.strftime("%Y%m%d-%H%M%S")
    _save({"state": "running", "at": time.time(), "id": job_id, "task": task, "text": "backing up"})
    before = backup(job_id)
    try:
        exe = claude_exe()
        cmd = [str(exe), "-p", RULES.format(root=ROOT, task=task), "--output-format", "json",
               "--permission-mode", "acceptEdits",
               "--allowedTools", "Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,Bash(.venv/Scripts/python.exe:*),"
                                 "Bash(git status:*),Bash(git diff:*),Bash(git log:*)",
               "--disallowedTools", "Bash(git push:*),Bash(git commit:*),Read(./.env),Edit(./.env),Write(./.env)"]
        _save({"state": "running", "at": time.time(), "id": job_id, "task": task, "text": "Claude Code working"})
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            out = json.loads(p.stdout)
            summary = (out.get("result") or "").strip()
        except ValueError:
            summary = (p.stdout or p.stderr or "").strip()[-800:]
        after = _files()
        changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
        broken = []
        for name in changed:
            if name.endswith(".py") and (ROOT / name).exists():
                c = subprocess.run([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "py_compile", name],
                                   cwd=ROOT, capture_output=True, text=True)
                if c.returncode:
                    broken.append(name)
        if broken:
            restore(job_id)
            _save({"state": "undone", "at": time.time(), "id": job_id, "task": task, "changed": changed})
            tell(f"aiya the change broke {', '.join(broken)}, so i put everything back like before. nothing changed "
                 f"- try say it another way?")
            return
        if not changed:
            _save({"state": "done", "at": time.time(), "id": job_id, "task": task, "changed": []})
            tell(f"didn't change anything leh.\n\n{summary[:1200]}")
            return
        restarted = restart(changed)
        _save({"state": "done", "at": time.time(), "id": job_id, "task": task, "changed": changed,
               "summary": summary[:2000]})
        tell(f"done liao ({', '.join(changed)} changed"
             + (f", {' + '.join(restarted)} restarted" if restarted else "") + ").\n\n"
             + summary[:1500] + "\n\nreply \"undo\" if u don't like it, i put back the old one.")
        if "WhatsApp bot" in restarted:  # last, so the summary got out first
            restart_bot()
    except subprocess.TimeoutExpired:
        restore(job_id)
        _save({"state": "undone", "at": time.time(), "id": job_id, "task": task})
        tell("took too long (40 min) so i stopped and put everything back. maybe split it into smaller asks?")
    except Exception as e:
        restore(job_id)
        _save({"state": "error", "at": time.time(), "id": job_id, "task": task, "error": str(e)[:300]})
        tell(f"cannot do leh - {str(e).splitlines()[0][:200]}. everything is like before.")


def can_undo():
    s = status()
    return bool(s.get("id") and s.get("state") == "done" and s.get("changed"))


def undo():
    """Put back the programs from before the last request, restart what they belong to, tell you."""
    s = status()
    if not can_undo():
        return "nothing to undo"
    restore(s["id"])
    _save({**s, "state": "undone", "at": time.time()})
    restarted = restart(s["changed"])
    tell(f"ok put back how it was before \"{s.get('task')}\"")
    if "WhatsApp bot" in restarted:
        restart_bot()
    return "undone"


def start_undo():
    """Undo in the background (the bot may need restarting, which it can't do to itself mid-reply)."""
    (ROOT / "logs").mkdir(exist_ok=True)
    subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-u", str(ROOT / "dev_runner.py"), "--undo"],
                     cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0),
                     stdout=open(ROOT / "logs" / "dev_runner.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)


def start(task):
    """Run a request in the background on this PC (the assistant calls this after you confirm)."""
    _save({"state": "running", "at": time.time(), "task": task, "text": "starting"})
    (ROOT / "logs").mkdir(exist_ok=True)
    subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-u", str(ROOT / "dev_runner.py"),
                      "--task", task], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess,
                                                                                         "DETACHED_PROCESS", 0),
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                     stdout=open(ROOT / "logs" / "dev_runner.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--undo", action="store_true")
    a = ap.parse_args()
    if a.undo:
        print(undo())
    elif a.task:
        run(a.task)
