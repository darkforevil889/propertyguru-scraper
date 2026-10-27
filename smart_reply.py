"""Smart reply: the AI writes Kelvin's next reply to a customer - in his own style (learned from his real past replies),
using his rules "when the customer ... -> reply ..." as the preferred wording, never asking again for what the customer
already gave, never inventing prices / fees / dates (those are marked needs_kelvin so they're never auto-sent).
Falls back to picking one of the rules when the AI can't be reached. In the WhatsApp chats window the reply shows as a
button above the message box; reply_approval.py WhatsApps it to you for an "ok", or sends it by itself when "auto" is on.

Rules + auto switch: Supabase storage owner-photos/data/smart_reply.json, edited on the AI Chat bot page ->
"Smart reply".
"""
import json
import re
import sqlite3
import time
from pathlib import Path

BUCKET, PATH = "owner-photos", "data/smart_reply.json"
DEFAULT = [
    {"when": "says yes / is interested", "reply": "Where is the place ah? U got details like sf and etc?"},
    {"when": "gives a place or condo name (but no size)", "reply": "U got details like sf and etc?"},
    {"when": "asks for a name card / business card",
     "reply": "Yes, will send u later. Do u have any property want to sell?"},
    {"when": "gives the place and size but no pictures", "reply": "Do u have pictures?"},
    {"when": "mentions an area only (not the exact place)", "reply": "Where is the place ah?"},
    {"when": "says they want to sell", "reply": "Where is the place ah? Do u have details?"},
    {"when": "says they want to rent", "reply": "Where is the place ah? Do u have details?"},
]


def _read(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(PATH))
    except Exception:
        return {}


def load(db):
    rules = _read(db).get("rules")
    return rules if rules is not None else [dict(r) for r in DEFAULT]


def auto_on(db):
    """Auto send on? Then the fitting reply goes to the customer by itself (reply_approval.py), no "ok" needed."""
    return bool(_read(db).get("auto"))


def save(db, rules=None, auto=None):
    """Save the rules and/or the auto send switch (None = keep what's there)."""
    d = _read(db)
    if rules is not None:
        d["rules"] = [{"when": r["when"].strip(), "reply": r["reply"].strip()} for r in rules
                      if (r.get("when") or "").strip() and (r.get("reply") or "").strip()]
    if auto is not None:
        d["auto"] = bool(auto)
    db.storage.from_(BUCKET).upload(PATH, json.dumps(d, ensure_ascii=False).encode(),
                                    {"content-type": "application/json", "upsert": "true"})
    return d.get("rules") if d.get("rules") is not None else [dict(r) for r in DEFAULT]


# What the customer may already have told us - so a reply never asks for it again.
SIZE = re.compile(r"\d[\d,.]*\s*k?\s*(?:sq\.?\s*ft|sqft|sf|kaki|sq\.?\s*m|sqm|ft2|平方|尺)|\b\d{3,5}\s*(?:sf|ft)\b", re.I)
ROOMS = re.compile(r"\b\d\s*(?:r|rm|room|rooms|bilik|房)\s*\d?\s*(?:b|bath|t|bilik air)?\b|\bstudio\b|\bduplex\b|\bpenthouse\b", re.I)
ASK_PLACE = re.compile(r"where|which (?:area|place|condo)|\bplace\b|kat mana|dekat mana|mana satu|lokasi|location|哪里|哪个|地方", re.I)
ASK_SIZE = re.compile(r"\bsf\b|sq ?ft|\bsize\b|saiz|keluasan|多大|几尺", re.I)
ASK_PICS = re.compile(r"pic|photo|gambar|照片|图片", re.I)
ASK_DETAILS = re.compile(r"details|maklumat|资料", re.I)
PLACES_FILE = Path(__file__).parent / "wa_bot_data" / "places.json"
# everyday words the AI sometimes offers as "names" - never places on their own
COMMON = set("""got have has yes no can ok okay sure sell selling sale rent renting rental buy unit units house home room
rooms studio condo condominium apartment property area place here there near nearby sir boss bro sis la lah ah want need
ada boleh nak jual sewa beli rumah bilik dekat sini sana tak tidak ya""".split())


def _is_place(name):
    """Is this a real property / area name in Malaysia? PropertyGuru's place search first, the internet if that knows
    nothing. -> {'kind': 'property' | 'area' | '', 'name': ...}. Remembered in wa_bot_data/places.json."""
    key = name.lower().strip()
    try:
        cache = json.loads(PLACES_FILE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if key in cache:
        return cache[key]
    found = {"kind": "", "name": name}
    try:
        import market_check
        words = set(re.findall(r"[a-z0-9]+", key))
        for item in market_check._autocomplete(name)[:5]:
            text = f"{item['displayText']['en']} {item.get('displayDescription', {}).get('en', '')}".lower()
            if words and words <= set(re.findall(r"[a-z0-9]+", text)):   # every word of what they wrote is in the match
                found = {"kind": "property" if item.get("objectType") == "property" else "area",
                         "name": item["displayText"]["en"]}
                break
    except Exception as e:
        print("place search:", str(e)[:150], flush=True)
    if not found["kind"]:
        try:
            import assistant
            hits = assistant._web(f'"{name}" condo OR residence OR taman Malaysia property')
            hits = hits if isinstance(hits, list) else []
            if any(re.search(r"\b" + re.escape(key) + r"\b", h["title"].lower()) and re.search(
                    r"condo|residen|apartment|taman|jalan|property|kuala lumpur|selangor|malaysia", (h["title"] + h["snippet"]), re.I)
                   for h in hits[:6]):
                found = {"kind": "property", "name": name}
        except Exception as e:
            print("place web search:", str(e)[:150], flush=True)
    cache[key] = found
    PLACES_FILE.parent.mkdir(exist_ok=True)
    PLACES_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return found


def known(msgs):
    """What the customer has already given anywhere in this chat: {'places': [...], 'areas': [...], 'size', 'rooms',
    'photos'}. Possible place names (even unfamiliar ones like "Astana", "Arcoris") are picked out by the AI and checked
    on PropertyGuru / the internet."""
    import bot_brain
    theirs = [m for m in msgs if not m["me"]]
    text = "\n".join(m["text"] for m in theirs if m["text"])
    k = {"places": [], "areas": [], "size": bool(SIZE.search(text)), "rooms": bool(ROOMS.search(text)),
         "photos": any(m["kind"] in ("photo", "video") for m in theirs)}
    if not text.strip():
        return k
    prompt = ("A customer wrote this to a Malaysian property agent on WhatsApp:\n\"\"\"\n" + text[-1500:] + "\n\"\"\"\n"
              "List every word or phrase that could be the name of a condo, building, project, taman, road or area - "
              "even short or unfamiliar ones (e.g. \"Astana\", \"Arcoris\", \"PV21\", \"Sentul\"). Not room counts, sizes "
              'or prices. Reply ONLY with JSON: {"names": ["..."]}')
    try:
        out = bot_brain._llm([{"role": "user", "content": prompt}], tools=[], temperature=0, max_tokens=900)
        names = json.loads(re.search(r"\{.*\}", out.get("content") or "", re.S).group(0)).get("names") or []
    except Exception:
        names = []
    for n in names[:6]:
        n = re.sub(r"\s+", " ", str(n)).strip(" .,")
        if len(n) < 3 or ROOMS.fullmatch(n) or SIZE.search(n) or all(w in COMMON for w in n.lower().split()):
            continue
        hit = _is_place(n)
        if hit["kind"]:
            k["places" if hit["kind"] == "property" else "areas"].append(hit["name"])
    return k


def trim(reply, k):
    """Your reply, minus any question about something the customer already gave (your wording otherwise kept)."""
    keep = []
    for part in re.split(r"(?<=[?!.。？])\s+", reply.strip()):
        if (k["places"] and ASK_PLACE.search(part)) or ((k["size"]) and ASK_SIZE.search(part)) \
                or (k["photos"] and ASK_PICS.search(part)) \
                or (ASK_DETAILS.search(part) and not ASK_SIZE.search(part) and k["places"] and (k["size"] or k["rooms"])):
            continue
        keep.append(part)
    return " ".join(keep).strip()


MSG_DB = Path(__file__).parent / "wa_data" / "messages.sqlite"
_STYLE = {"at": 0, "pairs": []}


def _own_chats(con):
    """Chat ids with your own numbers (Settings "my numbers", the assistant's owner number, chats the bot knows are yours)."""
    import os
    mine = set()
    try:
        import wa_client
        nums = list(wa_client.load_settings().get("my_numbers") or [])
    except Exception:
        nums = []
    nums.append(os.environ.get("ASSISTANT_NUMBER", ""))
    last9 = {re.sub(r"\D", "", n)[-9:] for n in nums if len(re.sub(r"\D", "", n)) >= 9}
    try:
        mine |= set(json.loads((Path(__file__).parent / "wa_bot_data" / "owner_chats.json").read_text()))
    except (OSError, ValueError):
        pass
    for jid, phone in con.execute("select distinct chat_jid, sender_phone from messages where from_me = 0"):
        if re.sub(r"\D", "", jid.split("@")[0])[-9:] in last9 or re.sub(r"\D", "", phone or "")[-9:] in last9:
            mine.add(jid)
    return mine


def style_examples(n=14):
    """Kelvin's own short replies to customers, each with the customer's message just before it - newest first.
    Shown to the AI so it writes like him (re-read once an hour)."""
    if _STYLE["pairs"] and time.time() - _STYLE["at"] < 3600:
        return _STYLE["pairs"]
    pairs, con = [], None
    try:
        con = sqlite3.connect(f"file:{MSG_DB}?mode=ro", uri=True)
        rows = con.execute("select chat_jid, from_me, body, ts from messages where chat_jid not like '%@g.us' "
                           "and chat_jid not like '%@broadcast' and trim(body) != '' order by chat_jid, ts").fetchall()
        mine = _own_chats(con)
        last = {}
        for jid, me, body, ts in rows:
            if jid in mine:
                continue  # chats with your own numbers / the assistant - not how you talk to customers
            body = re.sub(r"\s+", " ", body).strip()
            if me and jid in last and 2 <= len(body) <= 160 and not re.search(r"https?://|\d{9,}", body):
                pairs.append((ts, last.pop(jid)[:160], body))
            elif not me:
                last[jid] = body
    except Exception as e:
        print("smart reply style:", str(e)[:150], flush=True)
    finally:
        try:
            con.close()
        except Exception:
            pass
    pairs = [(c, r) for _, c, r in sorted(pairs, reverse=True)[:n]]
    _STYLE.update(at=time.time(), pairs=pairs)
    return pairs


def write(rules, msgs, k):
    """The AI writes the next reply: (reply, needs_kelvin) - or (None, True) when it can't."""
    import bot_brain
    convo = "\n".join(f"{'Kelvin' if m['me'] else 'Customer'}: {m['text'] or '[' + m['kind'] + ']'}"
                      for m in msgs[-20:] if m["text"] or m["kind"])
    examples = "\n".join(f"Customer: {c}\nKelvin: {r}" for c, r in style_examples()) or "(none yet)"
    prefer = "\n".join(f"- when the customer {r['when']}: \"{r['reply']}\"" for r in rules) or "(none)"
    gave = [f"the exact place ({', '.join(k['places'])})" if k["places"] else "",
            f"an area ({', '.join(k['areas'])})" if k["areas"] else "",
            "the size" if k["size"] else "", "the rooms / layout" if k["rooms"] else "", "photos" if k["photos"] else ""]
    gave = "; ".join(g for g in gave if g) or "nothing yet"
    prompt = f"""You write Kelvin's next WhatsApp reply. Kelvin is a property agent at HOMEX Property (Kuala Lumpur / Selangor).
He chats with owners and tenants to get listings to sell or rent: the exact place, the size or rooms, the asking price or
rental, and photos - then he arranges a viewing.

How Kelvin writes (his real messages):
{examples}

Kelvin's preferred lines - use their wording when one fits:
{prefer}

Already given by the customer in this chat: {gave}. Never ask for any of these again.

The chat (newest last):
{convo}

Write Kelvin's next reply:
- write in the language of the customer's LATEST message (English -> English, Malay -> Malay, Chinese -> Chinese,
  a mix -> the same mix), short and casual like his real messages, 1-2 short sentences
- answer what the customer asked if the chat already tells you; otherwise ask for the next missing detail (one thing)
- never make up facts: prices, fees or commission, availability, dates or times, addresses, legal / loan / tax advice.
  If the reply needs one of those, write a short holding line in his style (like "Ok, I check and let u know") and set needs_kelvin true
- needs_kelvin true also for complaints, angry or personal messages, someone no longer interested, or when unsure what they mean
Reply ONLY with JSON: {{"reply": "...", "needs_kelvin": true or false}}"""
    try:
        out = bot_brain._llm([{"role": "user", "content": prompt}], tools=[], temperature=0.4, max_tokens=900)
        j = json.loads(re.search(r"\{.*\}", out.get("content") or "", re.S).group(0))
    except Exception as e:
        print("smart reply write:", str(e)[:150], flush=True)
        return None, True
    reply = re.sub(r"^\s*Kelvin:\s*", "", str(j.get("reply") or "")).strip().strip('"')
    return (reply or None), bool(j.get("needs_kelvin"))


# Never left to the AI alone: a customer asking about these, or a reply that names a time / date / amount, waits for Kelvin
ASKS_KELVIN = re.compile(r"\bview|viewing|tengok|melawat|看房|看屋|\bfee|commission|komisen|佣金|\bcharge|how much|berapa|多少钱|"
                         r"\bmeet|jumpa|见面|appointment|\bcall me|telefon|打电话|agreement|perjanjian|合约|deposit|cagaran|订金", re.I)
NAMES_FACT = re.compile(r"\d\s*(?:am|pm)\b|\b\d{1,2}[:.]\d{2}\b|\btomorrow|\btoday|\btonight|\besok|\bhari ini|明天|今天|"
                        r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\bRM\s*\d|\d\s*%|\b\d+\s*k\b", re.I)


# a reply that commits Kelvin to something (asking the customer's price is fine)
COMMITS = re.compile(r"\bview|viewing|tengok|看房|arrange|\bmeet|jumpa|appointment|\bfee|commission|komisen|佣金|deposit|cagaran", re.I)


def _latest_customer(msgs):
    out = []
    for m in reversed(msgs):
        if m["me"]:
            break
        out.insert(0, m["text"] or "")
    return " ".join(out)


def suggest_full(rules, msgs):
    """{'reply', 'needs_kelvin'} for the customer's latest messages, or None (your message is the last one).
    needs_kelvin: never sent by itself - always asks you first."""
    if not msgs or msgs[-1]["me"]:
        return None
    k = known(msgs)
    reply, needs = write(rules, msgs, k)
    reply = trim(reply, k) if reply else None   # belt and braces: still never re-ask a given place / size / photos
    if reply:
        needs = needs or bool(ASKS_KELVIN.search(_latest_customer(msgs)) or NAMES_FACT.search(reply) or COMMITS.search(reply))
        return {"reply": reply, "needs_kelvin": needs}
    old = _pick(rules, msgs, k)   # AI down or nothing usable: pick one of your rules as before
    return {"reply": old[0], "needs_kelvin": False} if old else None


def suggest(rules, msgs):
    """[reply] or [] - see suggest_full."""
    r = suggest_full(rules, msgs)
    return [r["reply"]] if r else []


def _pick(rules, msgs, k=None):
    """Fallback: the rule that fits what the customer wrote since your last message (the AI only picks a number)."""
    import bot_brain
    if not rules or not msgs or msgs[-1]["me"]:
        return []
    theirs = []
    for m in reversed(msgs):
        if m["me"]:
            break
        theirs.insert(0, m["text"] or {"photo": "[sent a photo]", "voice": "[sent a voice note]",
                                       "pdf": "[sent a PDF]", "video": "[sent a video]"}.get(m["kind"], ""))
    said = "\n".join(t for t in theirs if t).strip()
    if not said:
        return []
    before = "\n".join(f"{'Kelvin' if m['me'] else 'Customer'}: {m['text'] or '[' + m['kind'] + ']'}"
                       for m in msgs[-10:-len(theirs)] if m["text"] or m["kind"])
    k = k or known(msgs)
    gave = [f"the exact place ({', '.join(k['places'])})" if k["places"] else "",
            f"only an area ({', '.join(k['areas'])}), not the exact place" if k["areas"] and not k["places"] else "",
            "the size" if k["size"] else "", "the rooms / layout" if k["rooms"] else "", "photos" if k["photos"] else ""]
    gave = "; ".join(g for g in gave if g) or "nothing yet"
    listing = "\n".join(f"{i}. When the customer {r['when']}" for i, r in enumerate(rules, 1))
    prompt = (f"Kelvin, a Malaysian property agent, is chatting on WhatsApp.\nEarlier:\n{before or '(nothing)'}\n\n"
              f"The customer's latest message:\n\"{said}\"\n\n"
              f"Already given by the customer somewhere in this chat: {gave}.\n\nRules:\n{listing}\n\n"
              "Which rules describe where the chat is now (English, Malay, Chinese or a mix)? Never pick a rule whose "
              "reply would ask for something already given above. Best first, up to 3. "
              'Reply ONLY with JSON: {"rules": [numbers]}')
    try:
        out = bot_brain._llm([{"role": "user", "content": prompt}], tools=[], temperature=0, max_tokens=900)
        nums = json.loads(re.search(r"\{.*\}", out.get("content") or "", re.S).group(0)).get("rules") or []
    except Exception:
        return []
    for n in nums:   # the first fitting reply that still asks something new, without the questions already answered
        if isinstance(n, int) and 1 <= n <= len(rules):
            reply = trim(rules[n - 1]["reply"], k)
            if reply:
                return [reply]
    return []
