"""Chat bot page on the online dashboard.

The bot runs on the PC (wa_bot.py). The PC copies its status, settings, FAQ and recent conversations to Supabase
(pc_status row 'bot') every 30 s; changes made online (ON / OFF, your details, FAQ) and test questions go to the
PC as pc_tasks rows, which keeper.py applies here. The AI key is never sent online - it stays in .env on the PC.
"""
import os
import time


def sync(db):
    """Copy the bot's status, settings, FAQ and recent conversations up (PC side)."""
    import bot_brain
    import bot_client
    s = bot_client.status() or {}
    settings = bot_client.load_settings()
    old = state(db)
    data = {"at": time.time(), "status": s.get("status") or "off", "me": s.get("me") or {},
            "replies_today": s.get("replies_today", 0), "enabled": settings.get("enabled", True),
            "profile": bot_brain.profile(), "faq": bot_brain.faq(), "has_key": bool(os.environ.get("BOT_LLM_API_KEY")),
            "conversations": bot_client.conversations(), "test": old.get("test")}
    db.table("pc_status").upsert({"name": "bot", "data": data}).execute()


def state(db):
    """What sync() copied up (online side): status, me, enabled, profile, faq, conversations, test - or {}."""
    rows = db.table("pc_status").select("data").eq("name", "bot").execute().data
    return rows[0]["data"] if rows else {}


def request(db, kind, params):
    """kind 'bot_settings' {enabled / profile / faq} or 'bot_test' {question}."""
    db.table("pc_tasks").insert({"kind": kind, "params": params}).execute()


def pending(db):
    return db.table("pc_tasks").select("id,kind,params").in_("kind", ["bot_settings", "bot_test"]) \
        .eq("status", "pending").order("id").execute().data


def run_requests(db, log=print):
    """Apply changes made online and answer test questions (PC side). Returns True if anything was done."""
    import bot_brain
    import bot_client
    tasks = pending(db)
    test = None
    for task in tasks:
        p = task.get("params") or {}
        if task["kind"] == "bot_settings":
            s = bot_client.load_settings()
            if "enabled" in p:
                s["enabled"] = bool(p["enabled"])
            if "profile" in p:
                s["profile"] = {k: str(v).strip() for k, v in (p["profile"] or {}).items()}
            bot_client.save_settings(s)
            if "faq" in p:
                bot_brain.FAQ_FILE.parent.mkdir(exist_ok=True)
                bot_brain.FAQ_FILE.write_text(p["faq"], encoding="utf-8")
            log("chat bot settings changed from the online page")
        else:
            q = (p.get("question") or "").strip()
            answer = bot_brain.reply([{"role": "user", "content": q}], lambda **k: {"saved": "test - not saved"})
            test = {"question": q, "answer": answer, "at": time.time(), "task": task["id"]}
        db.table("pc_tasks").update({"status": "done"}).eq("id", task["id"]).execute()
    if tasks:
        sync(db)  # show the change / answer online straight away
        if test:
            row = state(db)
            row["test"] = test
            db.table("pc_status").upsert({"name": "bot", "data": row}).execute()
    return bool(tasks)
