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
- Viewings: tell us the property and 2-3 times that suit you; an agent will confirm.
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


def faq():
    try:
        return FAQ_FILE.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_FAQ


def system_prompt():
    return f"""You are the WhatsApp assistant of a property agency in Kuala Lumpur, Malaysia.
Reply like a friendly, professional agent on WhatsApp: short (1-6 lines), no markdown headings, at most a few emojis.
Reply in the customer's language (English, Malay or Chinese - mixed is fine).

What you can do:
1. Find properties: use search_listings. Never invent listings, prices or links - only share what the tool returns.
   Show at most 5, each as: name - RM price - size - bedrooms - link. If nothing matches, say so and suggest widening.
2. Condo prices (KLCC / Mont Kiara): use condo_price and quote the sale range, rent range and size range.
3. Owners with a property to rent out or sell: collect location (condo + unit if they will share), sale or rent,
   price, size, bedrooms/bathrooms, furnishing and their name; ask them to send photos. Ask for missing details
   a few at a time. Once you have location, sale/rent and price, call save_owner_listing, then thank them and say an
   agent will follow up.
4. General questions: answer from the agency info below. If you don't know, say an agent will reply soon.
Never promise discounts, availability or viewing times yourself - an agent confirms those.
If the customer asks for a human, say an agent will reply shortly.

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
def _llm(messages):
    base = os.environ.get("BOT_LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    body = json.dumps({"model": os.environ.get("BOT_LLM_MODEL", "openai/gpt-oss-120b"), "messages": messages,
                       "tools": TOOLS, "tool_choice": "auto", "temperature": 0.3, "max_tokens": 700}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('BOT_LLM_API_KEY', '')}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]


def reply(history, save_owner_listing):
    """history: [{'role': 'user'|'assistant', 'content': str}, ...] (oldest first). Returns the reply text."""
    load_dotenv(ROOT / ".env", override=True)  # picks up a key saved from the dashboard
    if not os.environ.get("BOT_LLM_API_KEY"):
        return fallback(history[-1]["content"] if history else "")
    impl = {"search_listings": search_listings, "condo_price": condo_price, "save_owner_listing": save_owner_listing}
    messages = [{"role": "system", "content": system_prompt()}] + history[-14:]
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
        return "Thanks! An agent will get back to you shortly. 🙏"
    except Exception:
        return fallback(history[-1]["content"] if history else "")


def fallback(text):
    """No AI available: simple, honest keyword replies."""
    t = (text or "").lower()
    if any(w in t for w in ("price", "harga", "how much", "berapa")):
        for word in re.findall(r"[a-z0-9@ ]{4,}", t):
            r = condo_price(word.strip())
            if isinstance(r, list) and r:
                c = r[0]
                rm = lambda v: f"RM{v:,}" if v else "-"
                return (f"{c['condo']} ({c['area']}): sale {rm(c['sale_min'])} - {rm(c['sale_max'])}, "
                        f"rent {rm(c['rent_min'])} - {rm(c['rent_max'])}, "
                        f"size {c['sqft_min'] or '-'} - {c['sqft_max'] or '-'} sqft. An agent will follow up! 🙏")
    return ("Hi! Thanks for your message 🙏 Reply with:\n1) the condo / area and budget you're looking for, or\n"
            "2) your property details if you're an owner (location, sale/rent, price, photos).\n"
            "An agent will get back to you shortly.")
