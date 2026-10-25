"""Customer reply practice, step by step like a real WhatsApp chat: the owner / customer says something -> 5 drafts
for your next message -> you pick one (or write your own) -> the owner's next answer -> ... One thing at a time:
while the building, rent or sale, or the size is still unknown, the drafts only ask for that one thing. Once all
three are known: PropertyGuru & iProperty right now (the 5 lowest prices, fake ones marked, the lowest real one for
the owner's size, a suggested price) and the drafts give the price advice.
Drafts only - nothing is sent to anyone. The ones you pick become examples, so later drafts sound more like you.

Used by the dashboard (AI Chat bot -> "Customer reply") and your WhatsApp assistant ("customer: One KL 1500sf rent").
Saved in Supabase storage owner-photos/data/customer_reply.json. Needs this PC (live check opens Chrome).
"""
import json
import re
import time

BUCKET, PATH = "owner-photos", "data/customer_reply.json"
STYLES = ["very short and friendly", "explains a bit more", "Chinese-English mix, casual", "Malay-English mix",
          "polite and professional"]
ORDER = (("which building / condo", "place"), ("rent or sale", "type"), ("size or rooms", "size"))
ASK = {"which building / condo": "which condo ah?", "rent or sale": "u want sell or rent ah?",
       "size or rooms": "how big ah, how many sf?"}


def load(db):
    try:
        st = json.loads(db.storage.from_(BUCKET).download(PATH))
    except Exception:
        st = {}
    return {"chats": [], "examples": [], "test_numbers": [], **st}


def save(db, st):
    st["chats"] = st["chats"][-60:]
    for attempt in range(3):  # Supabase sometimes drops one request - try again
        try:
            db.storage.from_(BUCKET).upload(PATH, json.dumps(st, ensure_ascii=False).encode(),
                                            {"content-type": "application/json", "upsert": "true"})
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def _rm(x):
    return f"RM{x:,.0f}" if x else "-"


def suggest(lowest_real, typical, owner_price, rent):
    """Just under the lowest real price, so it rents / sells fast (e.g. lowest real RM4,400 -> RM4,300)."""
    base = lowest_real or typical
    if not base:
        return None
    if rent:
        step = 100 if base <= 5000 else 200 if base <= 10000 else 500
    else:
        step = 10_000 if base < 1_000_000 else 50_000 if base < 5_000_000 else 100_000
    price = int(base) // step * step
    if price >= base:
        price -= step
    price = max(step, price)
    if owner_price and owner_price <= base:  # the owner's own price is already at / under the market
        price = owner_price
    return price


def _wait_free(progress):
    import market_check
    for _ in range(60):  # one Chrome at a time - a saved-listing comparison may be running
        s = market_check.status()
        if not (s.get("state") == "running" and time.time() - s.get("at", 0) < 600):
            return
        progress("Waiting for another price check to finish…")
        time.sleep(5)
    raise RuntimeError("another price check is still running - try again in a few minutes")


# a price said without "RM" ("want 4500", "asking 4.5k") - never a size ("want 650sf")
WANT = re.compile(r"\b(?:want|wants|asking|ask|price|mau|nak)\s*(?:rm|myr)?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
                  r"\s*(k|mil|m|juta)?\b(?!\s*(?:sf|sq|ft|kaki))|要\s*(\d+(?:\.\d+)?)\s*(k)?", re.I)


def owner_price(text, rent):
    """'want 4500' / 'asking 4.5k' / 'mau 450k' (no "RM") -> the owner's price, or None."""
    for m in WANT.finditer(text):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        x = float(num.replace(",", "")) * {"k": 1e3, "m": 1e6, "mil": 1e6, "juta": 1e6}.get((unit or "").lower(), 1)
        if (300 <= x <= 300_000) if rent else (50_000 <= x <= 500_000_000):
            return int(x)
    return None


FILLER = {"hi", "hello", "hey", "i", "got", "have", "has", "a", "an", "one", "unit", "units", "want", "wanna", "to",
          "sell", "selling", "rent", "renting", "for", "sale", "my", "boss", "bro", "sis", "ya", "ok", "nak", "jual",
          "sewa", "ada", "saya", "the", "here", "is", "at", "in", "can", "u", "you", "help", "me", "please", "pls"}


def clean_place(place):
    """'i got one kl want sell' -> 'one kl'; 'want sell' -> None (no building named yet). A leading "one" stays
    when a name follows it (One KL, One Central)."""
    words = re.findall(r"[\w@&'.-]+", place or "")
    while words and words[0].lower() in FILLER and not (words[0].lower() == "one" and len(words) > 1
                                                         and words[1].lower() not in FILLER):
        words.pop(0)
    while words and words[-1].lower() in FILLER:
        words.pop()
    name = " ".join(words)
    return name if len(name) >= 2 and name.lower() not in FILLER else None


def _missing(d):
    have = {"place": d.get("place"), "type": d.get("type"), "size": d.get("sqft") or d.get("bedrooms")}
    return [word for word, key in ORDER if not have[key]]


def _check(db, d, text, progress):
    """PropertyGuru & iProperty now for this building / size -> the market dict (prices for the owner's size)."""
    import market_check
    rent = d.get("type") == "Rent"
    _wait_free(progress)
    progress(f"Checking {d['place']} on PropertyGuru & iProperty (about a minute)…")
    mid = f"practice-{int(time.time())}"
    row = {"message_id": mid, "place": d["place"], "address": None, "listing_type": "Rent" if rent else "Sale",
           "price_rm": d.get("price_rm"), "sqft": d.get("sqft"), "property_type": None, "message_text": text,
           "bedrooms": d.get("bedrooms")}
    res = market_check.run(db, rows=[row], store=False, progress=lambda m: None).get(mid) or {}
    kind = "rent" if rent else "sale"
    five = [{"price": u["price"], "sqft": u.get("sqft"), "beds": u.get("beds"), "url": u.get("url"),
             "fake": bool((u.get("check") or {}).get("exclude")), "why": (u.get("check") or {}).get("why")}
            for u in res.get(f"{kind}_lowest") or []]
    low_real, typical, note = market_check.lowest_real(res, kind), res.get("typical"), ""
    real = [u for u in five if not u["fake"] and u["price"] and u.get("sqft")]
    if d.get("sqft") and (res.get("basis") == "psf" or (real and not res.get(f"{kind}_same_size"))):
        # other sizes than the owner's: compare per sq ft, then back to a price for the owner's size
        if real:
            low_real = round(min(u["price"] / u["sqft"] for u in real) * d["sqft"])
        if res.get("basis") == "psf" and typical:
            typical = round(typical * d["sqft"])
        note = f"units there are other sizes - compared per sf, then worked out for {d['sqft']:,} sf"
    return {"property": res.get("property"), "url": res.get("property_url"), "kind": kind, "five": five,
            "lowest_real": low_real, "typical": typical, "label": res.get("label"), "note": note,
            "why": res.get("why") or res.get("reason"),
            "for": [d.get("place"), d.get("type"), d.get("sqft"), d.get("price_rm")],
            "suggested": suggest(low_real, typical, d.get("price_rm"), rent)}


def analyse(db, text, progress=lambda m: None, chat_id=None, phone=None):
    """The owner's next message (chat_id: same owner as before, else a new one) -> the chat with fresh details,
    the market check (once building, rent / sale and size are known) and 3 drafts. Saved."""
    from wa_parser import parse
    st = load(db)
    chat = next((c for c in st["chats"] if c["id"] == chat_id), None) if chat_id else None
    if not chat:
        chat = {"id": str(int(time.time() * 1000)), "at": time.time(), "lines": [], "market": None, "phone": phone}
        st["chats"].append(chat)
    _lines(chat).append({"who": "customer", "text": text.strip()})
    said = "\n".join(line["text"] for line in chat["lines"] if line["who"] == "customer")
    d = parse(said, need_price=False) or {}
    d["place"] = clean_place(d.get("place"))
    if not d.get("price_rm") and d.get("type"):
        d["price_rm"] = owner_price(said, d["type"] == "Rent")
    missing = _missing(d)
    old = chat.get("market") or {}
    if not missing and old.get("for") != [d.get("place"), d.get("type"), d.get("sqft"), d.get("price_rm")]:
        chat["market"] = _check(db, d, said, progress)  # everything known (or changed): check the market now
    progress("Writing reply options…")
    chat.update(details=d, missing=missing, picked=None, at=time.time(),
                options=drafts(chat["lines"], d, missing, chat.get("market"), st["examples"]))
    save(db, st)
    return chat


def _lines(chat):
    """The chat's messages (practice chats from before had just one owner message)."""
    if "lines" not in chat:
        chat["lines"] = [{"who": "customer", "text": chat["customer"]}] if chat.get("customer") else []
    return chat["lines"]


def drafts(lines, d, missing, market, examples):
    """5 options for Kelvin's next message - one step at a time, real numbers only."""
    import bot_brain
    convo = "\n".join(f"{'Owner' if x['who'] == 'customer' else 'Kelvin'}: {x['text']}" for x in lines[-12:])
    facts = {}
    if missing:
        task = (f"The owner hasn't said: {missing[0]}. ALL 5 options ask ONLY that one thing, in one very short "
                f"casual line like a real person texting (e.g. \"{ASK[missing[0]]}\"). No prices, nothing else yet.")
    elif market:
        facts = {"building": market["property"], "rent_or_sale": market["kind"],
                 "lowest_real_for_this_size": _rm(market["lowest_real"]),
                 "typical_for_this_size": _rm(market["typical"]), "suggested_price": _rm(market["suggested"]),
                 "how_compared": market.get("note") or "same size units",
                 "owner_price": _rm(d.get("price_rm")) if d.get("price_rm") else None}
        task = ("Now the price advice in 1-2 short lines: when the owner's price is above the suggested price (or "
                "the owner gave none), suggest the suggested price and say why briefly with the real lowest price "
                "for this size; when it's at / under, say it's a good price. ALL 5 give the same price - only the "
                "wording differs. Never invent a number. These are ASKING prices listed on PropertyGuru / iProperty "
                "now - never call them sales, deals or transactions.")
    else:
        task = "Reply naturally and briefly to keep the chat going."
    style = ("Replies Kelvin picked before - copy how he writes:\n"
             + "\n".join(f"- {e}" for e in examples[-8:])) if examples else ""
    prompt = ("Kelvin is a Malaysian property agent (KLCC / Mont Kiara) chatting on WhatsApp with a property owner. "
              "Write 5 options for Kelvin's NEXT message, styles: "
              + ", ".join(f"{i + 1}) {s}" for i, s in enumerate(STYLES))
              + ". Human and casual like a person texting - Singlish / Manglish ok, short, no emoji, not salesy. "
              + f"{task}\n{style}\nChat so far:\n{convo}\nFacts: {json.dumps(facts, ensure_ascii=False)}\n"
              + "Answer ONLY a JSON list of 5 strings.")
    try:
        out = bot_brain._llm([{"role": "user", "content": prompt}], tools=[], temperature=0.8, max_tokens=900)
        m = re.search(r"\[.*\]", out.get("content") or "", re.S)
        got = [str(x).strip() for x in json.loads(m.group(0))][:5] if m else []
        if got:
            return got
    except Exception:
        pass
    if missing:
        return [ASK[missing[0]]]
    if market and market.get("suggested"):
        return [f"market now lowest real around {_rm(market['lowest_real'])}, can try {_rm(market['suggested'])} "
                f"so faster {'rent' if market['kind'] == 'rent' else 'sell'} out. ok anot?"]
    return ["ok noted"]


MODE_WORDS = ("practice on", "practice", "start practice", "practice mode")
OWNERISH = re.compile(r"\b(?:want|wan|wanna|nak|mau|plan(?:ning)?\s+to)\s+(?:to\s+)?(?:sell|rent|jual|sewa|let)\b"
                      r"|\b(?:got|have|ada)\s+(?:a\s+|one\s+)?unit\b|\bfor\s+(?:sale|rent)\b|想(?:卖|租)", re.I)
HOW = ("practice mode on. now pretend u r the owner - send me what the owner says, i give u 5 options. reply 1-5 to "
       "pick one, or \"me: your own reply\" if yours is better. \"new owner\" = start again, \"practice off\" = stop")


def as_message(c):
    """A practice step for your WhatsApp: the prices (once checked) and the 5 options numbered."""
    out = []
    m = c.get("market")
    if m and m.get("five") and not c.get("missing"):
        out.append(f"{m.get('property')} {m['kind']} - 5 lowest now: "
                   + ", ".join(_rm(u["price"]) + (" (fake)" if u["fake"] else "") for u in m["five"])
                   + f". lowest real for this size {_rm(m['lowest_real'])}, suggest {_rm(m['suggested'])}")
    out.append("\n".join(f"{i}. {o}" for i, o in enumerate(c.get("options") or [], 1)))
    out.append("reply 1-5, or me: your own reply")
    return "\n\n".join(out)


def whatsapp(db, text, send):
    """Your own chat with the assistant: "practice on" -> each message you send is the pretend owner's, you get 5
    options back; 1-5 / "me: ..." picks; "new owner"; "practice off". -> True when it was handled here."""
    t, low = text.strip(), text.strip().lower()
    st = load(db)
    on = st.get("practice_mode") and time.time() - st.get("mode_at", 0) < 12 * 3600
    if low in MODE_WORDS:
        st.update(practice_mode=True, mode_at=time.time(), mode_chat=None)
        save(db, st)
        send(HOW)
        return True
    if not on:
        if not OWNERISH.search(t) or len(t) > 150:
            return False
        # you wrote like an owner ("hi want sell") - that's a practice: switch it on and carry on
        st.update(practice_mode=True, mode_at=time.time(), mode_chat=None)
        save(db, st)
        send("(sounds like an owner talking - practice mode on. \"practice off\" to stop)")
    if low in ("practice off", "stop practice", "stop"):
        st["practice_mode"] = False
        save(db, st)
        send("ok practice off")
        return True
    if low in ("new owner", "new customer"):
        st.update(mode_chat=None, mode_at=time.time())
        save(db, st)
        send("ok new owner - send me the owner's first message")
        return True
    chat = next((c for c in st["chats"] if c["id"] == st.get("mode_chat")), None)
    num = re.match(r"^\s*(?:pick\s*)?([1-5])\s*$", t)
    if chat and chat.get("options") and num and int(num.group(1)) <= len(chat["options"]):
        pick(db, chat["id"], chat["options"][int(num.group(1)) - 1])
        send(f"ok {num.group(1)} it is. owner's next message?")
        return True
    if chat and chat.get("options") and low.startswith("me:"):
        pick(db, chat["id"], t[3:].strip())
        send("ok noted, i learn from yours. owner's next message?")
        return True
    c = analyse(db, t, chat_id=st.get("mode_chat"))
    st = load(db)
    st.update(mode_chat=c["id"], mode_at=time.time())
    save(db, st)
    send(as_message(c))
    return True


def pick(db, chat_id, text):
    """You chose this reply (one of the options or your own words): it's your message in the chat now, and an
    example of your style for later drafts."""
    st = load(db)
    for c in st["chats"]:
        if c["id"] == chat_id:
            _lines(c).append({"who": "you", "text": text})
            c["picked"], c["options"] = text, []
    st["examples"] = (st["examples"] + [text])[-30:]
    save(db, st)
