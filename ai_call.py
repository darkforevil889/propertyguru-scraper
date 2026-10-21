"""AI call checker for ipline.cn (获客通): which A-grade calls are really interested in property?

Logs in with IPLINE_USER / IPLINE_PASS from .env (own Chrome profile .ipline-profile), reads 呼出记录 filtered to
意向等级 A, and for every call combines ipline's transcript with the recording re-heard by Whisper (Groq, free -
ipline's own speech-to-text is often wrong), then the AI decides: interested / maybe / not interested + why.
Results go to data/ai_call/ (CSV with reasons + .txt of interested phone numbers).

    python ai_call.py            # first page (30 calls)
    python ai_call.py --pages 3
"""
import argparse
import base64
import csv
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent.resolve()
OUT = ROOT / "data" / "ai_call"
PROFILE = ROOT / ".ipline-profile"
CACHE = OUT / "checked.json"  # call id -> result, so a call is never heard / judged twice
SITE = "https://ipline.cn"
GRADE_A = 21  # 意向等级 A类(较强) in ipline's filter
MYT = timezone(timedelta(hours=8))
GROQ = "https://api.groq.com/openai/v1"
load_dotenv(ROOT / ".env")


# ---------- ipline ----------
class Ipline:
    """Logged-in ipline session: Chrome logs in once, then ipline's own data feed is read directly."""

    def __init__(self, headless=True):
        self.headless = headless

    def __enter__(self):
        from patchright.sync_api import sync_playwright
        self._p = sync_playwright().start()
        self.ctx = self._p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=self.headless,
                                                              no_viewport=True)
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        token = {}
        page.on("request", lambda r: r.headers.get("auth") and "/ai/" in r.url
                and token.setdefault("auth", r.headers["auth"]))
        page.goto(f"{SITE}/login", wait_until="networkidle", timeout=60000)
        if "login" in page.url:
            page.fill("input[type=email]", os.environ["IPLINE_USER"])
            page.fill("input[type=password]", os.environ["IPLINE_PASS"])
            box = page.query_selector("input[type=checkbox]")
            if box and not box.is_checked():
                box.check(force=True)
            page.click("input[type=submit]")
            page.wait_for_url(lambda u: "login" not in u, timeout=30000)
        token.clear()
        page.goto(f"{SITE}/dialcenter/callout", wait_until="networkidle", timeout=60000)
        if not token.get("auth"):
            raise RuntimeError("Couldn't log in to ipline - check IPLINE_USER / IPLINE_PASS in .env")
        self.headers = {"auth": token["auth"], "referer": f"{SITE}/dialcenter/callout"}
        return self

    def __exit__(self, *exc):
        self.ctx.close()
        self._p.stop()

    def _get(self, path):
        r = self.ctx.request.get(SITE + path, headers=self.headers, timeout=60000)
        if r.status != 200:
            raise RuntimeError(f"ipline answered {r.status} for {path.split('?')[0]}")
        return r

    def calls(self, page_no, per_page=30, grade=GRADE_A):
        """One page of 呼出记录 with 意向等级 = grade -> (calls, total, pages)."""
        j = self._get(f"/ai/touchphone/phone/{page_no}?taskId=-1&filterGrade=71&cSize={per_page}"
                      f"&selectPushUseId=&gradeText={grade}").json()
        return j.get("data") or [], j.get("total") or 0, j.get("pages") or 0

    def transcript(self, call):
        """[{'who': 'robot' | 'person' | 'note', 'text', 'clip'}] in order; clip = the person's own voice for that line."""
        rows, page_no = [], 1
        while True:
            j = self._get(f"/ai/chat/tpid/{call['id']}?userId={call['userid']}&pageNum={page_no}&pageSize=100").json()
            d = ((j.get("data") or {}).get("data")) or {}
            rows += d.get("list") or []
            if not d.get("hasNextPage") or page_no > 10:
                break
            page_no += 1
        who = {0: "robot", 1: "person"}
        return [{"who": who.get(r.get("khtype"), "note"), "text": (r.get("khcontent") or "").strip(),
                 "clip": r.get("khvoicepath") if r.get("khtype") == 1 else None}
                for r in rows if (r.get("khcontent") or "").strip() or r.get("khtype") == 1]

    def clip(self, b64path):
        """One short recording (ipline already gives its path base64-encoded)."""
        return self._get("/ai/touchphone/audio/" + b64path).body()

    def recording(self, call):
        path = call.get("recordfilepath")
        if not path:
            return None
        return self._get("/ai/touchphone/audio/" + base64.b64encode(path.encode()).decode()).body()


# ---------- listening + judging (Groq, free) ----------
class DailyLimit(RuntimeError):
    """This model's free allowance for today is used up (resets within 24 h)."""


def _groq(method, path, **kw):
    """POST to Groq. Waits politely when the per-minute allowance is used up; raises DailyLimit when the
    per-day allowance is (no point waiting hours)."""
    headers = {"Authorization": f"Bearer {os.environ['BOT_LLM_API_KEY']}", "User-Agent": "KLTracker/1.0"}
    for attempt in range(8):
        r = httpx.request(method, GROQ + path, headers=headers, timeout=90, **kw)
        if r.status_code in (429, 503):
            if "per day" in r.text.lower():
                raise DailyLimit(r.text[:300])
            try:
                wait = float(r.headers.get("retry-after") or 10)
            except ValueError:
                wait = 10
            time.sleep(min(max(wait, 2), 60))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("The free AI service stayed busy - try again in a few minutes")


LISTEN = 4  # 4 = + answer hint for the clips; 2 = the person's own voice clips, 3 = + the whole recording (each hearing catches what the other misses)
LANGS = ("english", "malay", "chinese", "mandarin", "cantonese", "indonesian", "tamil")  # anything else = misheard


def _whisper(name, data, mime, prompt, language=None):
    form = {"model": "whisper-large-v3", "response_format": "verbose_json", "prompt": prompt}
    if language:
        form["language"] = language
    j = _groq("POST", "/audio/transcriptions", files={"file": (name, data, mime)}, data=form)
    lang = (j.get("language") or "").lower()
    if not language and lang and not any(x in lang for x in LANGS):
        return _whisper(name, data, mime, prompt, "ms")  # e.g. Malay heard as Swedish - try again as Malay
    return [s for s in j.get("segments") or []
            if s.get("text", "").strip() and s.get("no_speech_prob", 0) <= 0.6 and not NOISE.search(s["text"])]


def hear(audio):
    """Whole recording -> what was said (both voices mixed; a quiet person may be missed)."""
    segs = _whisper("call.mp3", audio, "audio/mpeg", "Phone call in Malaysia about selling or renting property. "
                                                     "English, Malay, Chinese, Manglish.")
    return "\n".join(f"[{s['start']:.0f}s] {s['text'].strip()}" for s in segs)
# Whisper "hears" these on silence / line noise - never real answers
NOISE = re.compile(r"点赞|订阅|转发|打赏|明镜|字幕|by bwd6|thank(s| you) for watching|subscribe|amara\.org|"
                   r"terima kasih kerana menonton", re.I)


# Whisper picks between similar-sounding words using this hint (e.g. "which property?" was heard as "we property")
PERSON_HINT = ("Answers on a phone call in Malaysia after being asked 'Do you want to sell or rent your property?'. "
               "Common answers: Which property? Where property? What property? Where is it? Yes. Okay. Can. Sure. "
               "No. Not interested. Who are you? Boleh. Takde. Apa? English, Malay, Chinese, Manglish.")


def hear_person(ip, turns):
    """Re-hear everything the person said from their own voice clips - one Whisper request per call: the clips are
    joined with a second of silence between them and each piece of text is put back on the line it came from."""
    import io
    import wave
    spans, frames, fmt, pos = [], [], None, 0.0
    for t in turns:
        if t["who"] != "person" or not t.get("clip"):
            continue
        try:
            w = wave.open(io.BytesIO(ip.clip(t["clip"])))
        except Exception:
            continue
        f = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        if fmt and f != fmt:
            continue
        fmt = fmt or f
        n = w.getnframes()
        frames += [w.readframes(n), b"\x00" * f[2] * f[0] * f[1]]  # + 1 s silence
        spans.append((pos, pos + n / f[2], t))
        pos += n / f[2] + 1.0
    if not spans:
        return
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(fmt[0])
        out.setsampwidth(fmt[1])
        out.setframerate(fmt[2])
        out.writeframes(b"".join(frames))
    segs = _whisper("person.wav", buf.getvalue(), "audio/wav", PERSON_HINT)
    got = {id(t): [] for *_, t in spans}
    for s in segs:
        text = s["text"].strip()
        mid = (s["start"] + s["end"]) / 2
        hit = min(spans, key=lambda sp: 0 if sp[0] <= mid <= sp[1] else min(abs(mid - sp[0]), abs(mid - sp[1])))
        got[id(hit[2])].append(text)
    for *_, t in spans:
        t["heard"] = " ".join(got[id(t)])


# Greetings / goodbyes say nothing about interest - left out of what the dashboard shows
FILLER = re.compile(r"^\W*(?:(?:hello|helo|hallo|hi|hey|yes|ya|yeah|bye|thank you|thanks|terima kasih|salam|"
                    r"喂|你好|哈囉|謝謝|谢谢|拜拜)[\s.!?,~。？！-]*)+$", re.I)


MALAY = re.compile(r"\b(saya|anda|awak|tidak|takde|tak|mana|terima|kasih|dari|boleh|apa|ini|nak|sudah|belum|"
                   r"dengan|untuk|maaf|ada)\b", re.I)


def _strip_filler(text):
    # The AI's own notes "(confused)" and Malay "translations" of English words: not what the person said
    text = re.sub(r"\s*\(([^()]*)\)", lambda m: "" if (m.group(1)[:1].islower() or MALAY.search(m.group(1)))
                  else m.group(0), text or "")
    parts = re.split(r"(?<=[.!?。？！])\s+|\s*/\s*|;\s*", text)
    keep = []
    for p in parts:
        p = re.sub(r"[,\s]*\b(?:thank you,?\s*)?(?:bye[- ]?)+\W*$", "", p.strip(), flags=re.I).strip(" ,")
        if p and not FILLER.match(p):
            keep.append(p)
    return " ".join(keep)


def after_q1(turns, key="text"):
    """What the person said from the robot's 'Do you want to sell or rent your property?' on, without
    hello / bye / thank you. key: 'text' = ipline's version, 'heard' = our re-hearing."""
    start = next((i for i, t in enumerate(turns) if t.get("who") == "robot"
                  and re.search(r"sell|rent", t.get("text", ""), re.I)), -1)
    words = [_strip_filler(t.get(key, "")) for t in turns[start + 1:] if t.get("who") == "person"]
    return " / ".join(w for w in words if w)


def clean_said(text):
    """The AI's summary of the person's words, without greetings / goodbyes."""
    return _strip_filler(text)


def conversation(turns, which="both"):
    """Turns -> readable lines. which: 'both' (re-heard + ipline's guess), 'heard' or 'ipline'."""
    out = []
    for t in turns:
        if t["who"] == "robot":
            out.append(f"ROBOT: {t['text']}")
        elif t["who"] == "person":
            heard, guess = (t.get("heard") or "").strip(), (t.get("text") or "").strip()
            if which == "ipline":
                out.append(f"PERSON: {guess or '…'}")
            elif which == "heard":
                out.append(f"PERSON: {heard or '(nothing clear)'}")
            else:
                out.append(f"PERSON: re-heard \"{heard or '(nothing clear)'}\"  |  ipline guessed \"{guess}\"")
    return "\n".join(out)


RULES = 8  # bump when JUDGE changes -> stored calls are judged again (without listening again)

JUDGE = """You check recorded phone calls for a Malaysian property agent. An AI robot phoned people, asked if they \
want to sell or rent out their property, then offered to WhatsApp details. ipline graded these calls "A" \
automatically, but its grading and its speech-to-text are often wrong.

Speech recognition on these phone lines is poor, so you get THREE hearings, and each can be wrong:
A) The conversation line by line. For every PERSON line: "re-heard" = the person's own voice clip heard again, \
and "ipline guessed" = ipline's own recognition.
B) The whole recording heard again - both voices mixed. The robot's lines are known (e.g. "I'm Lily, property \
agent here...", "Do you want to sell or rent your property?", "How about I WhatsApp you detail shortly?", "Thank \
you, bye bye"); everything else is the person. A quiet person may be missing here.
Work out what the person really said: prefer what two hearings agree on (also when they only sound alike, e.g. \
"Detail and so" ~ "Detail untuk?"); otherwise prefer the version that makes sense as a reply to the robot. \
Phone lines blur similar words: a short reply to Q1 like "we property" / "where property" / "what property" is \
almost always the question "which / where property?" - report it as that question. Ignore \
text that makes no sense in a property call (it's misheard). Only the PERSON counts - never the robot. People may \
speak English, Malay, Chinese or a mix.

The robot asks two questions: Q1 "Do you want to sell or rent your property?" and later Q2 "How about I WhatsApp \
you details shortly?". Work out what the person answered to each, then fill in these facts (true / false) - \
judge each one on its own, the decision is made from them afterwards:
- machine: a MACHINE answered, not a person - voicemail or recorded message ("leave a message after the beep", \
"press #", "voice message of 01...", "the number you dialled", "not available", "tidak dapat dihubungi", "请留言", \
"按#结束", "不在服务区", an operator / call-assistant voice).
- refused: hostile, rude or annoyed, or clearly says no / not now / not interested / don't want / don't like / \
no property / not owner / wrong number / busy-go-away / stop calling.
- q1_yes: the answer to Q1 is yes / ok / can / sure / boleh / ya, or they say they have a property to sell or rent \
out, or that they want to buy or rent one (e.g. "nak beli rumah", "we have property", "seeking the sale").
- q1_asks_property: in answer to Q1 they ask about the property or the offer - "what property?", "where \
property?", "where is it?", "which property?", "which one?", "which agency?", price, area, size. NOT a bare \
"sorry?" / "huh?" / "what?" / "hello?" that only means they didn't catch it.
- q2_yes: the answer to Q2 is yes / ok / okay / can / sure / boleh / ya / yeah / alright / "WhatsApp me" / "send \
me the details".
- confused: somewhere they clearly don't understand the call or who is calling - "what are you saying?", "I don't \
know what you're saying", "minta apa?", "apa ni?", "untuk apa?", "detail untuk?", "who are you?", "I don't know \
you", or replies that have nothing to do with the questions. A short "sorry?" / "huh?" / "hello?" alone is NOT \
confused.

Reply ONLY with JSON: {"q1": "<their answer to Q1, short>", "q2": "<their answer to Q2, short>", \
"machine": bool, "refused": bool, "q1_yes": bool, "q1_asks_property": bool, "q2_yes": bool, "confused": bool, \
"said": "<what the person said from Q1 on - their real words, short, no hello / bye / thank you; add an English \
translation in brackets only if not English>"}"""


def decide(f):
    """Your rules, applied in order -> (verdict, short reason)."""
    if f.get("machine"):
        return "not interested", "Machine / voicemail answered"
    if f.get("refused"):
        return "not interested", "Refused or annoyed"
    if f.get("q1_asks_property"):
        return "interested", "Asked about the property"
    if f.get("q1_yes"):
        return "interested", "Said yes / has or wants a property"
    if f.get("q2_yes") and not f.get("confused"):
        return "interested", "Agreed to WhatsApp"
    if f.get("q2_yes"):
        return "not interested", "Said ok to WhatsApp but didn't understand the call"
    if f.get("confused"):
        return "not interested", "Didn't understand the call"
    return "not interested", "No clear yes"


# Free models tried in turn - each has its own daily allowance. Qwen got the tricky calls right in testing
# (gpt-oss-20b didn't); the chat bot's main model (gpt-oss-120b) comes after it so the bot keeps its allowance longer.
MODELS = [m.strip() for m in os.environ.get(
    "AI_CALL_MODELS", "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b").split(",")
    if m.strip()]


def judge(turns, full=""):
    msg = (f"A) Line by line:\n{conversation(turns) or '(empty call)'}\n\n"
           f"B) Whole recording heard again:\n{full or '(nothing heard)'}")
    text, daily = "", 0
    for model in MODELS:
        body = {"model": model, "temperature": 0, "max_tokens": 1200,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": JUDGE}, {"role": "user", "content": msg}]}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"  # only simple facts to fill in - saves most of the allowance
        try:
            text = _groq("POST", "/chat/completions", json=body)["choices"][0]["message"]["content"] or ""
            break
        except DailyLimit:
            daily += 1  # this model is done for today - next one
        except httpx.HTTPStatusError as e:  # bad JSON from the model / model not available - next one
            if e.response.status_code not in (400, 404):
                raise
    else:
        if daily:
            raise DailyLimit("today's free AI allowance is used up on every model")
        raise RuntimeError("no AI model could read this call")
    try:
        facts = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except (AttributeError, ValueError):
        return {"verdict": "not interested", "said": "", "reason": "AI answer unreadable - please check: " + text[:150]}
    for k in ("machine", "refused", "q1_yes", "q1_asks_property", "q2_yes", "confused"):
        facts[k] = facts.get(k) is True or str(facts.get(k)).lower() == "true"
    verdict, reason = decide(facts)
    return {**facts, "verdict": verdict, "reason": reason}


# ---------- run ----------
def _load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


AUDIO = OUT / "audio"           # recordings kept so you can listen on the dashboard
TICKS = OUT / "confirmed.json"  # call id -> True / False as you ticked it on the dashboard
STATUS = OUT / "status.json"


def _save(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_ticks():
    try:
        return json.loads(TICKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_ticks(ticks):
    OUT.mkdir(parents=True, exist_ok=True)
    _save(TICKS, ticks)


def results():
    """Every call checked so far (newest call first)."""
    rows = []
    for k, v in _load_cache().items():
        if v.get("phone"):
            if "q1_yes" in v:  # rules may have changed since - apply today's to the stored facts
                v["verdict"], v["reason"] = decide(v)
            rows.append({"call_id": k, **v})
    return sorted(rows, key=lambda r: r.get("called") or "", reverse=True)


def audio_file(call_id):
    p = AUDIO / f"{call_id}.mp3"
    return p if p.exists() else None


def check(pages=1, start_page=1, progress=print, headless=True):
    """Check A-grade calls page by page (already-checked calls are skipped). Returns (results, csv_path, txt_path)."""
    OUT.mkdir(parents=True, exist_ok=True)
    AUDIO.mkdir(exist_ok=True)
    cache, results = _load_cache(), []

    def step(**kw):
        _save(STATUS, {"state": "running", "at": time.time(), "start": start_page, "pages": pages, **kw})

    with Ipline(headless=headless) as ip:
        for page_no in range(start_page, start_page + pages):
            calls, total, n_pages = ip.calls(page_no)
            progress(f"Page {page_no} of {n_pages} ({total:,} A-grade calls in total): {len(calls)} calls")
            if not calls:
                break
            for i, c in enumerate(calls, 1):
                key = str(c["id"])
                when = datetime.fromtimestamp(c.get("dialtime", 0) / 1000, MYT).strftime("%Y-%m-%d %H:%M")
                entry = cache.get(key) or {}
                step(page=page_no, of_pages=n_pages, call=i, calls=len(calls), phone=c["phone"])
                try:
                    if entry.get("listen") != LISTEN:
                        progress(f"  {i}/{len(calls)} {c['phone']}: listening…")
                        turns = ip.transcript(c)
                        hear_person(ip, turns)
                        entry["turns"] = [{k: t.get(k, "") for k in ("who", "text", "heard")} for t in turns]
                        mp3 = AUDIO / f"{key}.mp3"
                        if not mp3.exists():
                            audio = ip.recording(c)  # whole call - also for listening on the dashboard
                            if audio:
                                mp3.write_bytes(audio)
                        entry["full"] = hear(mp3.read_bytes()) if mp3.exists() else ""
                        entry.update(listen=LISTEN, rules=None)
                    entry["heard"] = conversation(entry["turns"], "heard")
                    entry["ipline"] = conversation(entry["turns"], "ipline")
                    if entry.get("rules") != RULES:
                        entry.update(judge(entry["turns"], entry.get("full", "")), rules=RULES)
                    entry.update(phone=c["phone"], called=when, seconds=c.get("conversationtime"), page=page_no)
                    cache[key] = entry
                    _save(CACHE, cache)
                except DailyLimit:
                    raise  # nothing more can be checked today - stop instead of failing every call
                except Exception as e:
                    results.append({"phone": c["phone"], "called": when, "seconds": c.get("conversationtime"),
                                    "verdict": "error", "said": "", "reason": str(e)[:200], "heard": "",
                                    "ipline": "", "call_id": key})
                    continue
                results.append({"call_id": key, **{k: entry.get(k, "") for k in
                                                   ("phone", "called", "seconds", "verdict", "said", "reason",
                                                    "heard", "ipline")}})
    _save(STATUS, {"state": "done", "at": time.time(), "checked": len(results),
                   "errors": sum(r["verdict"] == "error" for r in results)})
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path, txt_path = OUT / f"ai_call_{stamp}.csv", OUT / f"interested_{stamp}.txt"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["phone", "verdict", "said", "reason", "called", "seconds", "heard",
                                          "ipline", "call_id"])
        w.writeheader()
        w.writerows(results)
    phones = list(dict.fromkeys(r["phone"] for r in results if r["verdict"] == "interested"))
    txt_path.write_text("\n".join(phones) + ("\n" if phones else ""), encoding="utf-8")
    return results, csv_path, txt_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--show", action="store_true", help="show the Chrome window")
    a = ap.parse_args()
    try:
        res, csv_path, txt_path = check(a.pages, a.start, headless=not a.show)
    except DailyLimit:
        _save(STATUS, {"state": "error", "at": time.time(),
                       "error": "Today's free AI allowance is used up. Calls checked so far are saved - press "
                                "Check calls again tomorrow and it carries on where it stopped."})
        raise SystemExit("Today's free AI allowance is used up - run again tomorrow.")
    except Exception as e:
        OUT.mkdir(parents=True, exist_ok=True)
        _save(STATUS, {"state": "error", "at": time.time(), "error": str(e)[:300]})
        raise
    for r in res:
        print(f"{r['phone']}  {r['verdict'].upper():15} {r['said']}  -> {r['reason']}")
    print("CSV:", csv_path, "\nTXT:", txt_path)
