"""The WhatsApp bot's brain: understands a customer's message and answers from YOUR data.

AI: any OpenAI-compatible chat API - default Groq (free tier). Change provider in .env:
    BOT_LLM_API_KEY=...                                   (required for AI replies)
    BOT_LLM_BASE_URL=https://api.groq.com/openai/v1       (default; Gemini/OpenRouter/DeepSeek also work)
    BOT_LLM_MODEL=openai/gpt-oss-120b                     (default)
Without a key (or if the AI is down) it falls back to simple keyword replies.

Tools the AI can use: search current listings, check a condo's price range, save an owner's listing.
"""
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
BOT_DATA = ROOT / "wa_bot_data"
FAQ_FILE = BOT_DATA / "faq.md"
load_dotenv(ROOT / ".env")

DEFAULT_FAQ = """\
- We are a property agency in Kuala Lumpur (KLCC, Mont Kiara, Bangsar, Damansara Heights and more).
- Office hours: Mon-Sat 9am-7pm. Messages outside these hours are answered the next working day.
- Viewings: tell me the property and 2-3 times that suit you; I will confirm.
- Owners: send the location, sale or rent, price, size, bedrooms/bathrooms, furnishing and photos -
  we will list it for you.
"""

CATEGORIES = {"homes_rent": "kl-rent-8k", "commercial_rent": "kl-commercial-rent-8k",
              "homes_sale": "kl-sale-3m", "commercial_sale": "kl-commercial-sale-3m"}

TOOLS = [
    {"type": "function", "function": {
        "name": "search_listings",
        "description": "Search current property listings in our database (Kuala Lumpur: homes for rent from RM8k/month, "
                       "commercial for rent from RM8k/month, homes and commercial for sale from RM3 million).",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "place": {"type": "string", "description": "Condo name or area, e.g. 'Mont Kiara', 'The Conlay'. Optional."},
            "min_price": {"type": "integer", "description": "RM. Optional."},
            "max_price": {"type": "integer", "description": "RM. Optional."},
            "bedrooms": {"type": "integer", "description": "Optional."},
        }, "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "condo_price",
        "description": "Price range of a condo in KLCC or Mont Kiara: lowest/highest sale price, rent and size.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "save_owner_listing",
        "description": "Save a property an OWNER wants to rent out or sell, once you have at least the location, "
                       "sale/rent and price. Photos the owner sent in this chat are attached automatically.",
        "parameters": {"type": "object", "properties": {
            "listing_type": {"type": "string", "enum": ["Rent", "Sale"]},
            "place": {"type": "string"}, "unit_no": {"type": "string"},
            "price_rm": {"type": "integer"}, "sqft": {"type": "integer"},
            "bedrooms": {"type": "integer"}, "bathrooms": {"type": "integer"},
            "furnishing": {"type": "string"}, "owner_name": {"type": "string"},
            "notes": {"type": "string", "description": "Anything else the owner said (tenancy, parking, availability)."},
        }, "required": ["listing_type", "place", "price_rm"]}}},
]


def fix_mojibake(text):
    """'Iâ€™m' -> 'I'm': text pasted through the wrong encoding."""
    if "â€" in text or "Ã" in text:
        try:
            return text.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            for bad, good in (("â€™", "'"), ("â€˜", "'"), ("â€œ", '"'), ("â€\x9d", '"'), ("â€“", "-"), ("â€”", "-"),
                              ("â€¦", "...")):
                text = text.replace(bad, good)
    return text


def faq():
    try:
        return fix_mojibake(FAQ_FILE.read_text(encoding="utf-8"))
    except OSError:
        return DEFAULT_FAQ


PROFILE_FIELDS = {"agent_name": "Name", "agency": "Agency", "phone": "Phone", "extra": "Other details for the name card"}


def profile():
    """Your real details (set on the dashboard's Chat bot page)."""
    try:
        s = json.loads((BOT_DATA / "bot_settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return {k: (s.get("profile") or {}).get(k, "") for k in PROFILE_FIELDS}


def system_prompt():
    p = profile()
    details = "\n".join(f"- {label}: {p[k]}" for k, label in PROFILE_FIELDS.items() if p[k]) or "- (none given)"
    name = p.get("agent_name") or "the agent"
    return f"""You ARE {name}, a property agent in Kuala Lumpur, Malaysia, chatting on WhatsApp. Speak in the first
person as {name} ("I'll check and get back to you") - never say "an agent will reply" or that you are a bot/assistant.
TEXT LIKE A REAL MALAYSIAN ON WHATSAPP, not like a customer-service bot:
- SHORT. Usually 1 line, max 2 short lines. Like a quick text, not an email. No "Let me know if you need anything else".
- Casual Manglish / Singlish mixed in naturally: "can lah", "ok boss", "got", "no problem", "wah", "eh", "ya",
  "sure sure", "nvm", "ok noted", "one sec ah", "hmm". Don't overdo it - 1 or 2 per message.
- Lowercase start is fine, small typos-free but relaxed. Max 1 emoji, often none.
- If they're very formal, be polite but still short.
- Reply in their language (English, Malay, Chinese or a mix).
- Never write lists of features or long explanations unless they ask. Plain text only (WhatsApp bold = *one star*).
- If you need to say 2 different things, put a blank line between them - each part is sent as its own bubble.
Examples of the tone:
  "hi boss, ya can. which area you looking?"
  "wah nice. what budget ah?"
  "ok noted, let me check and get back to you"
  "haha no la, not sure about the score"

Off-topic chat is welcome: small talk, food, weather, football, jokes, how's your day - just chat naturally like a
person would. If they ask for live information you can't know (today's weather, news, scores, exchange rates), say
casually you're not sure / haven't checked, don't make it up. For recommendations (food, shops, places) only name
very well-known places you are sure exist (e.g. Village Park nasi lemak in Damansara Uptown, Nasi Lemak
Antarabangsa in Kampung Baru); if unsure, talk generally instead of naming places. When it fits naturally, you can bring it back to
property (e.g. "btw still looking for a place?") - but don't force it every time.

STRICT RULE - never invent facts: no made-up names, phone numbers, emails, addresses, companies, licence numbers,
prices or listings. Only use: your details below, the FAQ below, and what the tools return. If something isn't
there, say you'll check and get back to them - do not guess. Don't bring up email or licence numbers yourself.
Don't call the customer by a name unless they told you their name in this chat.

Your details - a name card lists ONLY these lines, nothing else (no email, no licence number, no placeholders):
{details}

What you can do:
1. Find properties: use search_listings. Never invent listings, prices or links - only share what the tool returns.
   Show max 3, one short line each: name - RM price - sqft - beds - link. If nothing matches, say so and ask budget/area.
2. Condo prices: when someone asks the price / "how much" / range of a condo (not a specific unit), ALWAYS call
   condo_price first and give the ranges in ONE short line, like "sale ~Xm-Ym, rent Xk-Yk, X-Y sf" (with the real numbers). Only if not found, use search_listings.
3. Owners with a property to rent out or sell: collect location (condo + unit if they will share), sale or rent,
   price, size, bedrooms/bathrooms, furnishing and their name; ask them to send photos. Ask ONE or TWO missing details
   at a time, casually ("which unit ah? and asking how much?"). Once you have location, sale/rent and price, call save_owner_listing, then thank them and say
   you'll follow up.
4. General questions: answer from the FAQ below. If you don't know, say you'll check and get back to them.
Never confirm discounts, availability or exact viewing times yourself - say you'll check and confirm.

Agency info (FAQ):
{faq()}"""


# ---------- tools ----------
@lru_cache(maxsize=1)
def _db():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])


def search_listings(category, place=None, min_price=None, max_price=None, bedrooms=None):
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 7 * 86400))
    q = (_db().table("listings").select("title,price_rm,floor_area_sqft,bedrooms,bathrooms,furnishing,district,area,url")
         .eq("search_key", CATEGORIES.get(category, "kl-rent-8k")).gte("last_seen", since))
    if min_price:
        q = q.gte("price_rm", int(min_price))
    if max_price:
        q = q.lte("price_rm", int(max_price))
    if bedrooms:
        q = q.eq("bedrooms", int(bedrooms))
    if place:
        p = re.sub(r"[^\w\s@&'-]", " ", place).strip()
        q = q.or_(f"title.ilike.%{p}%,district.ilike.%{p}%,area.ilike.%{p}%,address.ilike.%{p}%")
    rows = q.order("price_rm").limit(5).execute().data
    return [{"name": r["title"], "price_rm": r["price_rm"], "sqft": r["floor_area_sqft"], "bedrooms": r["bedrooms"],
             "bathrooms": r["bathrooms"], "furnishing": r["furnishing"],
             "area": r["area"] or r["district"], "link": r["url"]} for r in rows]


_condo_cache = {"at": 0, "rows": []}


def condo_price(name):
    if time.time() - _condo_cache["at"] > 1800:
        try:
            data = json.loads(_db().storage.from_("owner-photos").download("data/condo_summary.json"))
            _condo_cache.update(at=time.time(), rows=data.get("rows") or [])
        except Exception:
            pass
    rows = _condo_cache["rows"]
    key = name.lower().strip()
    hits = [r for r in rows if key in (r.get("condo") or "").lower()]
    if not hits:
        names = {(r.get("condo") or "").lower(): r for r in rows}
        hits = [names[n] for n in difflib.get_close_matches(key, list(names), n=3, cutoff=0.6)]
    keep = ("condo", "area", "sale_min", "sale_max", "sale_listings", "rent_min", "rent_max", "rent_listings",
            "sqft_min", "sqft_max", "bedrooms", "tenure")
    return [{k: r.get(k) for k in keep} for r in hits[:3]] or {"result": "not found in KLCC / Mont Kiara data"}


# ---------- AI call ----------
def _models():
    """Main model first, then free backups - each has its own per-minute allowance on Groq."""
    main = os.environ.get("BOT_LLM_MODEL", "openai/gpt-oss-120b")
    backups = os.environ.get("BOT_LLM_BACKUP_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b")
    return list(dict.fromkeys([main] + [m.strip() for m in backups.split(",") if m.strip()]))


def _llm(messages):
    base = os.environ.get("BOT_LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    wait = 0
    for attempt in range(2):  # every model busy -> wait (as told by the server, max 20 s) and try once more
        if wait:
            time.sleep(wait)
        for model in _models():
            body = json.dumps({"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                               "temperature": 0.3, "max_tokens": 450}).encode()
            req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('BOT_LLM_API_KEY', '')}",
                # Groq's firewall rejects Python's default user agent (error 1010)
                "User-Agent": "KLTrackerBot/1.0", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503):
                    raise
                try:
                    wait = max(wait, min(20, float(e.headers.get("retry-after") or 5)))
                except ValueError:
                    wait = 5
    raise RuntimeError("all AI models busy (rate limit)")


GREETING = re.compile(r"^\s*(hi+|hello+|hey+|hai+|helo+|hallo|yo|morning|good\s*(morning|afternoon|evening|night)|"
                      r"selamat\s*(pagi|petang|malam|tengahari)|assalamualaikum|salam|你好|您好|早上好|早安)\s*"
                      r"(there|bro|sis|boss|sir|madam|dear)?\s*[!.~😊🙏👋]*\s*$", re.I)


def greeting_reply(text):
    """A bare 'hi' gets a bare 'hi' back (no AI)."""
    m = GREETING.match(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word.startswith("good") or word.startswith("selamat") or word == "morning":
        return f"{m.group(1).strip().capitalize()} 😊"
    if word in ("assalamualaikum", "salam"):
        return "Waalaikumsalam 😊"
    if word in ("你好", "您好", "早上好", "早安"):
        return f"{word} 😊"
    return "Hi 😊"


def tidy(text):
    """WhatsApp formatting: **bold** -> *bold*, no markdown headings."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", r"\2", text)  # WhatsApp shows plain links only
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    # safety net: the same sentence / chunk accidentally written twice in a row
    text = re.sub(r"(.{15,}?)[\s.]*\1", r"\1", text)
    return text.strip()


def reply(history, save_owner_listing):
    """history: [{'role': 'user'|'assistant', 'content': str}, ...] (oldest first). Returns the reply text."""
    if history and history[-1]["role"] == "user" and (g := greeting_reply(history[-1]["content"])):
        return g
    return tidy(_reply(history, save_owner_listing))


def _reply(history, save_owner_listing):
    load_dotenv(ROOT / ".env", override=True)  # picks up a key saved from the dashboard
    if not os.environ.get("BOT_LLM_API_KEY"):
        return fallback(history[-1]["content"] if history else "")
    impl = {"search_listings": search_listings, "condo_price": condo_price, "save_owner_listing": save_owner_listing}
    messages = [{"role": "system", "content": system_prompt()}] + history[-10:]
    try:
        for _ in range(4):  # allow a few tool calls
            msg = _llm(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip() or fallback(history[-1]["content"])
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    result = impl[c["function"]["name"]](**args)
                except Exception as e:
                    result = {"error": str(e)[:200]}
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(result, ensure_ascii=False, default=str)[:6000]})
        return "Thanks! I'll check and get back to you shortly. 🙏"
    except Exception as e:
        print("AI call failed:", str(e)[:300], flush=True)  # shows in logs/wa_bot.log
        text = fallback(history[-1]["content"] if history else "")
        # never send the same menu twice in a row
        if any(m["role"] == "assistant" and m["content"] == text for m in history[-4:]):
            return "Noted 🙏 I'll get back to you shortly."
        return text


def fallback(text):
    """No AI available: simple, honest keyword replies."""
    t = (text or "").lower()
    if any(w in t for w in ("price", "harga", "how much", "berapa", "range")):
        # the condo name = the message without price words / filler; only answer on a clear name match
        name = re.sub(r"\b(how much|how|much|price|prices|harga|berapa|range|the|is|of|for|at|ah|ar|lah|leh|eh|"
                      r"boss|bro|pls|please|can|check|what|whats|condo|unit|rent|rental|sale|sell|buy)\b", " ", t)
        name = re.sub(r"[^a-z0-9@& ]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        condo_price("x")  # makes sure the condo list is loaded
        hits = [r for r in _condo_cache["rows"] if len(name) >= 3 and name in (r.get("condo") or "").lower()]
        if hits:
            c = hits[0]
            k = lambda v: (f"{v / 1e6:.2f}".rstrip("0").rstrip(".") + "m") if v and v >= 1e6 else (
                f"{v / 1e3:.1f}".rstrip("0").rstrip(".") + "k") if v else "-"
            return (f"{c['condo']}: sale ~{k(c['sale_min'])}-{k(c['sale_max'])}, rent {k(c['rent_min'])}-"
                    f"{k(c['rent_max'])}, {c['sqft_min'] or '-'}-{c['sqft_max'] or '-'} sf")
        return "hmm let me check on that one and get back to you ya"
    return "ok noted, one sec ah - let me check and get back to you"
