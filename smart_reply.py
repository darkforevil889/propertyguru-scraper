"""Smart reply: your rules "when the customer ... -> reply ...". In the WhatsApp chats window the reply whose rule
fits the customer's latest message shows as a button above the message box - you tap it, change it if you like and
press send yourself. Nothing is ever sent by itself.

Rules: Supabase storage owner-photos/data/smart_reply.json, edited on the AI Chat bot page -> "Smart reply".
"""
import json
import re

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


def load(db):
    try:
        rules = json.loads(db.storage.from_(BUCKET).download(PATH)).get("rules")
    except Exception:
        rules = None
    return rules if rules is not None else [dict(r) for r in DEFAULT]


def save(db, rules):
    rules = [{"when": r["when"].strip(), "reply": r["reply"].strip()} for r in rules
             if (r.get("when") or "").strip() and (r.get("reply") or "").strip()]
    db.storage.from_(BUCKET).upload(PATH, json.dumps({"rules": rules}, ensure_ascii=False).encode(),
                                    {"content-type": "application/json", "upsert": "true"})
    return rules


def suggest(rules, msgs):
    """msgs: the chat, oldest first, [{'me': bool, 'text', 'kind'}]. -> the reply whose rule fits what the
    customer wrote since your last message (the best one; [] when your message is the last one)."""
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
    listing = "\n".join(f"{i}. When the customer {r['when']}" for i, r in enumerate(rules, 1))
    prompt = (f"Kelvin, a Malaysian property agent, is chatting on WhatsApp.\nEarlier:\n{before or '(nothing)'}\n\n"
              f"The customer's latest message:\n\"{said}\"\n\nRules:\n{listing}\n\n"
              "Which rules clearly describe the customer's latest message (English, Malay, Chinese or a mix)? "
              "Pick the single best one; skip any rule whose reply would ask for something they already gave. "
              'Reply ONLY with JSON: {"rules": [numbers]}')
    try:
        out = bot_brain._llm([{"role": "user", "content": prompt}], tools=[], temperature=0, max_tokens=300)
        nums = json.loads(re.search(r"\{.*\}", out.get("content") or "", re.S).group(0)).get("rules") or []
    except Exception:
        return []
    picked = []
    for n in nums:
        if isinstance(n, int) and 1 <= n <= len(rules) and rules[n - 1]["reply"] not in picked:
            picked.append(rules[n - 1]["reply"])
    return picked[:1]
