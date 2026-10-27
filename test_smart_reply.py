"""Smart reply must never ask for a place / size / photos the customer already gave. Read-only: nothing is sent."""
import sys
import bot_brain
import smart_reply

me = lambda t: {"me": True, "text": t, "kind": "text"}
them = lambda t: {"me": False, "text": t, "kind": "text"}
photo = {"me": False, "text": "", "kind": "photo"}
rules = smart_reply.load(bot_brain._db())
print("rules:", [r["reply"] for r in rules])

cases = [  # (name, chat, must NOT ask about, must ask about)
    ("Angeline (real case)", [me("Hi, any property want to sell or rent?"), them("Astana 3r2b"), them("Arcoris studio"), them("Sell"),
                             them("Astana abt 1200sqft 3r2b"), them("Arcoris studio abt 600sqft")], ["place", "size"], []),
    ("just yes", [me("Hi, any property want to sell?"), them("Yes")], [], ["place"]),
    ("area only", [me("Hi boss, got unit to rent?"), them("Got, at Sentul")], [], []),
    ("place, no size", [me("Hi, any property to rent out?"), them("I have unit at PV21 want rent")], ["place"], []),
    ("place said earlier", [me("Hi"), them("I got Arcoris unit"), me("Ok noted"), them("yes can")], ["place"], []),
    ("chinese", [me("你好, 有房子要卖吗?"), them("有, Arcoris 要卖")], ["place"], []),
    ("place+size+photo", [me("Hi"), them("Astana 1200sf 3r2b for rent"), photo], ["place", "size", "photos"], []),
]
ask = {"place": smart_reply.ASK_PLACE, "size": smart_reply.ASK_SIZE, "photos": smart_reply.ASK_PICS}
bad = 0
for name, chat, never, must in cases:
    reply = (smart_reply.suggest(rules, chat) or [""])[0]
    if name == "area only" and not reply:
        problems_extra = ["gave no reply"]
    k = smart_reply.known(chat)
    problems = [f"asks {w} again" for w in never if reply and ask[w].search(reply)] + \
               [f"should ask {w}" for w in must if not ask[w].search(reply)]
    bad += bool(problems)
    print(f"{'BAD' if problems else 'OK '} {name}: places={k['places']} areas={k['areas']} size={k['size']} "
          f"rooms={k['rooms']} photos={k['photos']}\n     -> {reply or '(no reply)'} {problems or ''}")

# The AI now writes the reply: things it must not decide alone are marked needs_kelvin (never auto-sent)
# (name, chat, needs_kelvin expected, words the reply must NOT contain)
free = [
    ("viewing time", [me("Hi, any unit to rent?"), them("Arcoris studio 650sf rm2800, can view tomorrow 3pm?")], True, []),
    ("asks fee", [me("Hi boss, want sell?"), them("Yes Astana 1200sf 3r2b"), them("How much your fee ah?")], True, ["%"]),
    ("furnished answer", [me("Hi"), them("Sky Suites 2r2b 900sf fully furnished rent 3500"), photo], False, []),
    ("not interested", [me("Hi any property sell?"), them("No thanks, already sold")], None, []),  # a polite close is fine either way
    ("malay", [me("Hi, ada rumah nak jual?"), them("Ada, kat Setapak")], False, []),  # Kelvin's own English lines are fine here
    ("english stays english", [me("Hi"), them("Astana 1200sf 3r2b for rent"), photo], False, ["berapa", "sewa"]),
    ("viewing asked in malay", [me("Hi"), them("Unit Setapak 3 bilik, boleh tengok hari sabtu?")], True, []),
]
for name, chat, want_needs, banned in free:
    got = smart_reply.suggest_full(rules, chat) or {"reply": "", "needs_kelvin": None}
    problems = ([f"needs_kelvin {got['needs_kelvin']} (want {want_needs})"] if want_needs is not None and got["needs_kelvin"] != want_needs else []) \
        + [f"says '{w}'" for w in banned if w in got["reply"]] + (["no reply"] if not got["reply"] else [])
    bad += bool(problems)
    print(f"{'BAD' if problems else 'OK '} {name}: -> {got['reply']}  [needs_kelvin={got['needs_kelvin']}] {problems or ''}")
sys.exit(1 if bad else 0)
