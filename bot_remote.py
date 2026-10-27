"""AI Chat bot page on the online dashboard.

Your assistant runs on the PC (wa_bot.py). The PC copies its WhatsApp status and your chat with the assistant to
Supabase (pc_status row 'bot') every 30 s, so the online page can show them. Nothing else goes up - the AI key stays
in .env on the PC.
"""
import time


def sync(db):
    """Copy the bot's WhatsApp status and your chat with the assistant up (PC side)."""
    import assistant
    import bot_client
    s = bot_client.status() or {}
    # "Send one now" pressed on the (new) dashboard: the assistant texts you its next reminder
    for t in db.table("pc_tasks").select("id").eq("kind", "assistant_nudge").eq("status", "pending").execute().data:
        ok = s.get("status") == "ready"
        if ok:
            bot_client.assistant_nudge()
        db.table("pc_tasks").update({"status": "done" if ok else "failed"}).eq("id", t["id"]).execute()
    try:  # Price filter asked for on the (new) dashboard - this module is reloaded every keeper round, that one isn't
        import importlib

        import price_filter_remote
        importlib.reload(price_filter_remote).run_requests(db)
    except Exception as e:
        print(f"price filter requests: {str(e)[:200]}", flush=True)
    data = {"at": time.time(), "status": s.get("status") or "off", "me": s.get("me") or {},
            "conversations": [c for c in bot_client.conversations() if c["chat"] == assistant.HISTORY_KEY]}
    db.table("pc_status").upsert({"name": "bot", "data": data}).execute()


def state(db):
    """What sync() copied up (online side): status, me, conversations - or {}."""
    rows = db.table("pc_status").select("data").eq("name", "bot").execute().data
    return rows[0]["data"] if rows else {}
