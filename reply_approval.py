"""Smart reply by WhatsApp approval: a customer messages your main number -> your assistant sends YOU the smart reply
it would use (smart_reply.py), numbered. You answer "ok" (or "ok 3") and only then it goes to the customer;
"no" skips it. Nothing reaches a customer without your ok.

Runs inside wa_bot.py: watch() looks at your chats (through the WhatsApp connector, wa_client) every 20 s;
answer() handles your ok / no. Waiting replies: wa_bot_data/approvals.json.
  * one waiting reply per chat - the customer writing more replaces it with a fresh one
  * not sent if you already answered that chat yourself, or the customer opted out (wa_blast.py)
  * waiting replies expire after 12 hours; groups and your own number are left out
Auto send (switch on the "Smart reply" tab, smart_reply.auto_on): the reply goes to the customer by itself and you
only get a note of what was sent. Falls back to asking you when that chat already had AUTO_MAX auto replies in the
last 24 h, or the same reply was already sent in that chat (no loops of the same question).
Market report: once a customer has given the place, the size (or rooms) and photos, the PC checks PropertyGuru /
iProperty (market_check.py, opens Chrome) and sends YOU the 5 lowest and the market price first, plus a reply
telling them the market price - that one also waits for your ok. Once per chat a week.
"""
import json
import queue
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
FILE = ROOT / "wa_bot_data" / "approvals.json"
QUIET = 15  # seconds the customer has stopped typing before the reply is worked out (they often send a few lines)
EXPIRE = 12 * 3600
AUTO_MAX = 4  # auto replies per chat in 24 h - after that it asks you again
DONE = queue.Queue()  # market reports finished in the background -> watch() adds their reply to the waiting ones
MARKET_LOCK = threading.Lock()  # one PropertyGuru check at a time
ANSWER = re.compile(r"^\s*(ok|okay|send|yes|no|skip)\s*#?\s*(\d+)?\s*[.!]*\s*$", re.I)


def _load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"next": 1, "items": [], "seen": {}}


def _save(s):
    FILE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def waiting(s=None):
    now = time.time()
    return [i for i in (s or _load())["items"] if i["status"] == "waiting" and now - i["at"] < EXPIRE]


def watch(tell, is_owner, log=print):
    """Background loop: new customer messages -> a smart reply for you to approve. tell(text) messages you."""
    import bot_brain
    import smart_reply
    import wa_blast
    import wa_chat_ui
    import wa_client
    s = _load()
    first, rules = True, {"at": 0, "rules": [], "auto": False}
    while True:
        time.sleep(20)
        try:
            if (wa_client.status() or {}).get("status") != "ready":
                continue
            chats = [c for c in wa_chat_ui.chats() if not c["is_group"] and not (c["phone"] and is_owner(c["phone"]))]
            if first:  # what was already there when the bot started isn't new
                for c in chats:
                    s["seen"].setdefault(c["jid"], c["last_ts"] or 0)
                first = False
                _save(s)
                continue
            now = time.time()
            while not DONE.empty():  # market reports done: their reply for the customer waits for your ok
                c, report, reply = DONE.get()
                n = _add(s, c, "(market report)", reply, now) if reply else None
                tell(report + (f"\n\nSend them: {reply}\nok{'' if len(waiting(s)) == 1 else ' ' + str(n)} = send · "
                               "no = skip" if reply else ""))
            for c in chats:
                ts = c["last_ts"] or 0
                if ts <= s["seen"].get(c["jid"], 0) or now - ts < QUIET:
                    continue
                s["seen"][c["jid"]] = ts
                if c["phone"] and wa_blast._local(c["phone"]) in wa_blast.opted_out():
                    continue
                msgs = wa_chat_ui.messages(c["jid"], chat=c)
                if not msgs or msgs[-1]["me"]:
                    continue
                if any(m["kind"] == "photo" and not m["me"] for m in msgs) \
                        and now - s.setdefault("reported", {}).get(c["jid"], 0) > 7 * 86400:
                    info = listing_of(msgs)
                    if info:  # place + size / rooms + photos: check PropertyGuru, tell you first
                        s["reported"][c["jid"]] = now
                        threading.Thread(target=market_report, args=(c, info, msgs, log), daemon=True).start()
                        continue  # the report is what you get for this chat - not a smart reply as well
                if now - rules["at"] > 60:
                    db = bot_brain._db()
                    rules.update(at=now, rules=smart_reply.load(db), auto=smart_reply.auto_on(db))
                reply = (smart_reply.suggest(rules["rules"], msgs) or [None])[0]
                if not reply:
                    continue
                said = " / ".join(m["text"] or f"[{m['kind']}]" for m in msgs[-3:] if not m["me"])[-200:]
                name = c["name"] or f"+{c['phone']}"
                if rules["auto"] and _auto_ok(s, c, reply, msgs, now):
                    res = wa_client.send(c["send_to"], reply)
                    s["items"].append({"n": 0, "jid": c["jid"], "send_to": c["send_to"], "name": name, "said": said,
                                       "reply": reply, "at": now, "done": now,
                                       "status": "auto sent" if res.get("ok") else "auto failed"})
                    tell(f"⚡ Auto-sent to {name}: \"{said}\"\n→ {reply}" if res.get("ok") else
                         f"⚡ Couldn't auto-send to {name}: {res.get('error')}")
                    continue
                n = _add(s, c, said, reply, now)
                tell(f"⚡ #{n} {c['name'] or '+' + str(c['phone'])}: \"{said}\"\nReply: {reply}\n"
                     f"ok{'' if len(waiting(s)) == 1 else ' ' + str(n)} = send · no = skip")
            _save(s)
        except Exception as e:
            log(f"reply approval: {str(e)[:300]}")


def _auto_ok(s, c, reply, msgs, now):
    """May this reply go out without your ok? Not past AUTO_MAX a day in this chat, never the same reply twice."""
    sent = [i for i in s["items"] if i["jid"] == c["jid"] and i["status"] == "auto sent"]
    if sum(now - i["at"] < 86400 for i in sent) >= AUTO_MAX:
        return False
    norm = lambda t: re.sub(r"\W+", " ", (t or "").lower()).strip()
    return norm(reply) not in {norm(i["reply"]) for i in sent} | {norm(m["text"]) for m in msgs if m["me"]}


def _add(s, c, said, reply, now):
    """A reply waiting for your ok -> its number. One per chat: a newer one replaces the older."""
    for old in waiting(s):
        if old["jid"] == c["jid"]:
            old["status"] = "replaced"
    item = {"n": s["next"], "jid": c["jid"], "send_to": c["send_to"], "name": c["name"] or f"+{c['phone']}",
            "said": said, "reply": reply, "at": now, "status": "waiting"}
    s["next"] += 1
    s["items"] = [i for i in s["items"] if now - i["at"] < 3 * 86400] + [item]
    _save(s)
    return item["n"]


def listing_of(msgs):
    """What the customer told about their property (AI reads their messages) -> fields, or None unless the place
    and the size or rooms are there."""
    import bot_brain
    said = "\n".join(m["text"] for m in msgs if not m["me"] and m["text"])[-3000:]
    if not said:
        return None
    # both sides: a bare "3285" only makes sense after the agent's "U got details like sf?"
    chat = "\n".join(f"{'Agent' if m['me'] else 'Owner'}: {m['text'] or '[' + m['kind'] + ']'}"
                     for m in msgs[-40:] if m["text"] or m["kind"])[-4000:]
    prompt = ("A property owner is telling a Malaysian property agent about their property on WhatsApp. The chat:\n"
              f"{chat}\n\nWhat did the OWNER say about their property? Condo names are often short and lower case "
              "(\"one kl\" = the condo One KL, \"troika\" = The Troika) - a name like that is the place, not an area. "
              "Reply ONLY with JSON: "
              "{\"place\": condo / building / taman name or null if "
              "only a general area, \"area\": area or town or null, \"sale_or_rent\": \"Sale\" or \"Rent\" or "
              "null, \"price_rm\": their asking price as a number in RM or null, \"sqft\": size in sq ft as a "
              "number or null, \"bedrooms\": number or null, \"property_type\": e.g. Condo, Terrace, Shop or null}")
    try:
        out = bot_brain._llm([{"role": "user", "content": prompt}], tools=[], temperature=0, max_tokens=300)
        d = json.loads(re.search(r"\{.*\}", out.get("content") or "", re.S).group(0))
    except Exception:
        return None
    if not d.get("place") or not (d.get("sqft") or d.get("bedrooms")):
        return None
    d["text"] = said
    return d


def market_report(c, info, msgs, log=print):
    """Background: PropertyGuru / iProperty for this customer's property -> DONE (report for you, reply for them)."""
    import bot_brain
    import market_check

    def num(x):
        try:
            return int(float(str(x).replace(",", "")))
        except (TypeError, ValueError):
            return None
    asked, sqft = num(info.get("price_rm")), num(info.get("sqft"))
    kind = "Rent" if info.get("sale_or_rent") == "Rent" else "Sale"
    rent = kind == "Rent"
    row = {"message_id": f"wa-{c['jid']}", "place": info["place"], "address": info.get("area"),
           "listing_type": kind, "sqft": sqft, "bedrooms": num(info.get("bedrooms")),
           "property_type": info.get("property_type"), "message_text": info["text"],
           "price_rm": asked or 1}  # no price yet: a stand-in so the typical price is still worked out
    name = c["name"] or f"+{c['phone']}"
    try:
        with MARKET_LOCK:
            for _ in range(60):  # a comparison from the dashboard is running: wait for it (up to 10 min)
                st = market_check.status()
                if not (st.get("state") == "running" and time.time() - st.get("at", 0) < 600):
                    break
                time.sleep(10)
            res = market_check.run(bot_brain._db(), rows=[row], progress=lambda t: None, store=False)[row["message_id"]]
    except Exception as e:
        log(f"market report: {str(e)[:200]}")
        DONE.put((c, f"🏠 {name}: {info['place']} - couldn't check PropertyGuru ({str(e)[:100]}).", None))
        return
    what = " · ".join(x for x in (info["place"], f"{sqft:,} sf" if sqft else "",
                                  f"{row['bedrooms']}R" if row["bedrooms"] else "", kind.lower(),
                                  f"asks {market_check._rm(asked, rent)}" if asked else "") if x)
    lines = [f"🏠 {name}: {what}"]
    # the 5 cheapest: ✅ real / ⚠️ check / ❌ fake, from each listing's own description (lelong, bait prices, ...)
    units = res.get(f"{kind.lower()}_lowest") or []
    mark = {"real": "✅", "check": "⚠️", "fake": "❌"}
    if units:
        lines.append(f"PropertyGuru lowest {len(units)}"
                     + ("" if res.get(f"{kind.lower()}_same_size") else " (any size)") + ":")
        lines += [f"• {mark.get((u.get('check') or {}).get('status'), '')} {market_check._rm(u['price'], rent)}"
                  + (f" · {u['sqft']:,} sf" if u.get("sqft") else "")
                  + (f" · {u['beds']}R" if (u.get("beds") or 0) > 0 else "") for u in units]
    # your price to put: the lowest one that isn't fake (lelong / bait / Bumi-only are left out)
    real = [u["price"] for u in units if not (u.get("check") or {}).get("exclude")]
    market = min(real) if real else None
    if market:
        lines.append(f"Lowest real: {market_check._rm(market, rent)}"
                     + (f" · typical {market_check._rm(res['typical'], rent)}"
                        if res.get("typical") and res.get("basis") != "psf" else "")
                     + (f" · {res['count']} listed" if res.get("count") else ""))
        if asked:
            lines.append(f"Their price: {res.get('label')}")
    else:
        lines.append(res.get("reason") or res.get("label") or "Nothing to compare with.")
    if res.get("property_url"):
        lines.append(res["property_url"] + "&sort=price&order=asc")  # lowest price first
    reply = f"Now market price around {market_check._rm(market, rent)}" if market else None
    DONE.put((c, "\n".join(lines), reply))


def answer(text):
    """Your message to the assistant: an ok / no for a waiting reply? -> what to tell you ("" = sent, nothing to
    say), or None (not one - the assistant answers it as usual)."""
    import wa_chat_ui
    import wa_client
    m = ANSWER.match(text or "")
    if not m:
        return None
    s = _load()
    items = waiting(s)
    if not items:
        return None  # nothing waiting: a normal "ok" to the assistant
    if m.group(2):
        item = next((i for i in items if i["n"] == int(m.group(2))), None)
        if not item:
            return f"#{m.group(2)} isn't waiting anymore. Waiting: " + ", ".join(f"#{i['n']}" for i in items)
    elif len(items) == 1:
        item = items[0]
    else:
        return "Which one? " + ", ".join(f"#{i['n']} {i['name']}" for i in items) + f" - e.g. ok {items[-1]['n']}"
    if m.group(1).lower() in ("no", "skip"):
        item["status"] = "skipped"
        _save(s)
        return f"Skipped #{item['n']} {item['name']}."
    msgs = wa_chat_ui.messages(item["jid"])
    if msgs and msgs[-1]["me"]:  # you answered them yourself meanwhile - don't send a second reply
        item["status"] = "already answered"
        _save(s)
        return f"You already replied to {item['name']}, so I didn't send it."
    res = wa_client.send(item["send_to"], item["reply"])
    item["status"] = "sent" if res.get("ok") else "failed"
    item["done"] = time.time()
    _save(s)
    return "" if res.get("ok") else f"Couldn't send to {item['name']}: {res.get('error')}"
