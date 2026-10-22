"""Furnishing for saved owner listings (Supabase owner_listings) that don't say it.

1. Their own text first: "fully ID furnished", "partly furnished", "kosong"... (free, instant).
2. Otherwise up to 3 of their photos go to the free AI (Groq Qwen - the only free model there that can see
   pictures): fully furnished / partly furnished / unfurnished, or unknown when the photos don't show the inside.
   The result is marked "(from photos)". Shops, offices, factories and land are skipped.

Runs after each WhatsApp scan and from keeper.py now and then (catch-up when the AI's daily allowance was used up).
    python furnish_check.py
"""
import base64
import io
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent.resolve()
DONE = ROOT / "wa_data" / "furnish_checked.json"  # message_id -> what the photos showed (so each is asked once)
BUCKET = "owner-photos"
SKIP_TYPES = {"Shop / Shop office", "Office / Commercial", "Factory / Warehouse", "Land"}
LABELS = {"fully": "Fully Furnished", "partly": "Partially Furnished", "unfurnished": "Unfurnished"}
load_dotenv(ROOT / ".env")

PROMPT = """These are photos from a Malaysian property owner's listing. Look at the inside of the unit and say how \
it is furnished:
- "fully": a lived-in set - beds, sofa, dining table, wardrobes (most of these)
- "partly": only built-ins / basics - kitchen cabinets, aircon, water heater, maybe curtains or lights, no loose furniture
- "unfurnished": empty rooms
- "unknown": the photos don't show the inside (building, view, pool, floor plan, screenshots, documents)
Reply ONLY with JSON: {"furnishing": "fully" | "partly" | "unfurnished" | "unknown", "why": "<a few words>"}"""


class AllowanceUsedUp(RuntimeError):
    pass


def _small_jpeg(data, size=768):
    """Shrink a photo before sending - the free AI counts picture size against the daily allowance."""
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((size, size))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return out.getvalue()


def ask_photos(images):
    """[jpeg bytes] -> ('fully' | 'partly' | 'unfurnished' | 'unknown', why)."""
    content = [{"type": "text", "text": PROMPT}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(_small_jpeg(i)).decode()}}
        for i in images]
    for attempt in range(5):
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions", timeout=120, json={
            "model": "qwen/qwen3.8-27b", "temperature": 0, "max_tokens": 500,
            "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": content}]},
            headers={"Authorization": f"Bearer {os.environ.get('BOT_LLM_API_KEY', '')}", "User-Agent": "KLTracker/1.0"})
        if r.status_code == 429:
            if "per day" in r.text.lower():
                raise AllowanceUsedUp("today's free AI allowance for photos is used up")
            time.sleep(min(60, float(r.headers.get("retry-after") or 10)))
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
        try:
            j = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        except (AttributeError, ValueError):
            return "unknown", "unreadable answer"
        f = str(j.get("furnishing", "unknown")).lower().strip()
        return (f if f in ("fully", "partly", "unfurnished") else "unknown"), str(j.get("why") or "")[:120]
    return "unknown", "AI busy"


def run(db, only_ids=None, log=print, max_listings=40):
    """Fill in furnishing for saved listings that don't have it. Returns how many were filled in."""
    from wa_parser import furnishing_in, property_category
    try:
        done = json.loads(DONE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    try:
        condos = [c.get("condo") for c in json.loads(db.storage.from_(BUCKET).download("data/condo_summary.json"))
                  .get("rows") or []]
    except Exception:
        condos = []
    q = db.table("owner_listings").select("message_id,place,property_type,message_text,photo_paths,furnishing")
    rows = q.is_("furnishing", "null").execute().data
    if only_ids is not None:
        rows = [r for r in rows if r["message_id"] in set(only_ids)]
    filled = asked = 0
    for r in list(rows):  # first everything whose own text says it - free, and never held up by the AI's limit
        stated = furnishing_in(r.get("message_text"))
        if stated:
            db.table("owner_listings").update({"furnishing": stated}).eq("message_id", r["message_id"]).execute()
            filled += 1
            rows.remove(r)
    for r in rows:
        mid = r["message_id"]
        if mid in done or property_category(r.get("property_type"), r.get("place"), r.get("message_text"),
                                            condos) in SKIP_TYPES:
            continue
        photos = [p for p in r.get("photo_paths") or [] if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        if not photos or asked >= max_listings:
            continue
        # a few photos from across the set - the first ones are often the building / view
        pick = [photos[i] for i in sorted({len(photos) // 4, len(photos) // 2, (3 * len(photos)) // 4})][:3]
        images = []
        for p in pick:
            try:
                images.append(db.storage.from_(BUCKET).download(p))
            except Exception:
                pass
        if not images:
            continue
        verdict, why = ask_photos(images)  # AllowanceUsedUp stops the run - the rest are tried next time
        asked += 1
        done[mid] = {"furnishing": verdict, "why": why, "at": time.time()}
        DONE.parent.mkdir(exist_ok=True)
        DONE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
        if verdict in LABELS:
            db.table("owner_listings").update({"furnishing": f"{LABELS[verdict]} (from photos)"}) \
                .eq("message_id", mid).execute()
            filled += 1
        log(f"furnishing from photos: {r.get('place')!r} -> {verdict} ({why})")
    return filled


if __name__ == "__main__":
    from supabase import create_client
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    try:
        print("filled in:", run(db))
    except AllowanceUsedUp as e:
        print(f"stopped: {e} - run again later")
