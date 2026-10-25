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
    data = {"at": time.time(), "status": s.get("status") or "off", "me": s.get("me") or {},
            "conversations": [c for c in bot_client.conversations() if c["chat"] == assistant.HISTORY_KEY]}
    db.table("pc_status").upsert({"name": "bot", "data": data}).execute()


def state(db):
    """What sync() copied up (online side): status, me, conversations - or {}."""
    rows = db.table("pc_status").select("data").eq("name", "bot").execute().data
    return rows[0]["data"] if rows else {}
