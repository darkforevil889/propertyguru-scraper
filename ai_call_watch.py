"""Watches ipline.cn's 单任务外呼 after YOU press 全部开始呼叫 and WhatsApps you how it's going (read only - it never
starts, stops or changes calls). keeper.py runs check() every 10 minutes on ipline's calling days / hours.

You get one message each, per day:
  * calls are moving (numbers left are going down / calls live)
  * almost finished (5% or 50 numbers left, whichever is more)
  * finished (nothing left to call)
  * stopped moving: still numbers left but nothing called for 40 minutes, with ipline's own message (e.g. 全局静默)
  * at 9:30 if nothing has moved yet, with ipline's message - so you know to check
State: data/ai_call/watch.json (the assistant reads it for "call finish already?").

    python ai_call_watch.py          # one check now, prints what it saw
"""
import json
import re
import time
from datetime import datetime

import ai_call

STATE = ai_call.OUT / "watch.json"
EVERY = 10 * 60
STUCK = 40 * 60


def load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_ipline():
    """What ipline shows now: numbers left to call, calls live, its red status line, calling days / hours."""
    with ai_call.Ipline() as ip:
        task = (ip._get("/ai/task/getSingleTaskData").json().get("data")) or {}
        left = ip._get("/ai/notouchphone/phone/1?taskId=0").json().get("total") or 0
        live = ((ip._get("/ai/calling/getcallingnum").json().get("data")) or {}).get("CallingNum") or 0
    week = task.get("DialWeek") or {}
    hours = [(r["starttime"], r["endtime"]) for r in task.get("DialTimeRange") or [] if r.get("operate") == 1]
    return {"left": int(left), "live": int(live), "note": re.sub(r"<[^>]+>", "", task.get("message") or "").strip(),
            "days": [d for d in range(7) if week.get(f"day{d + 1}") == 1], "hours": hours, "bot": task.get("ai2wordName")}


def calling_time(st, now):
    """Inside ipline's calling days / hours (as last read; Mon-Fri 9-6 until the first read)."""
    days, hours = st.get("days", [0, 1, 2, 3, 4]), st.get("hours") or [("09:00", "18:00")]
    hm = now.strftime("%H:%M")
    return now.weekday() in days and any(a <= hm <= b for a, b in hours)


def check(log=print, send=None, force=False):
    """One look (keeper calls it every ~minute; it only reads ipline every 10 minutes on calling days / hours)."""
    if send is None:
        import bot_client
        send = bot_client.assistant_say
    st, now = load(), datetime.now()
    if not force and (not calling_time(st, now) or time.time() - st.get("at", 0) < EVERY):
        return
    s = ai_call.status()
    if s.get("state") == "running" and time.time() - s.get("at", 0) < 600:
        return  # the AI call check has ipline's browser open - look next time
    seen = read_ipline()
    day = now.strftime("%Y-%m-%d")
    if st.get("day") != day:  # new day: start counting from this first reading
        st = {"day": day, "first_left": seen["left"], "sent": [], "moved_at": None}
    if seen["left"] < st.get("left", seen["left"]) or seen["live"]:
        st["moved_at"] = time.time()
    st.update({k: seen[k] for k in ("left", "live", "note", "days", "hours", "bot")}, at=time.time())
    sent, first, left = st["sent"], st["first_left"], seen["left"]
    done_today = max(0, first - left)
    note = f'\n\nipline says: "{seen["note"]}"' if seen["note"] else ""

    def tell(key, text):
        if key not in sent:
            try:
                send(text)
                sent.append(key)
                log(f"ai call watch: told you '{key}' ({left} left)")
            except Exception as e:  # bot not up - try again next check
                log(f"ai call watch: couldn't WhatsApp '{key}': {str(e)[:150]}")

    moved = st.get("moved_at")
    if moved and "started" not in sent:
        tell("started", f"ipline calls moving liao. {seen['live']} on the line now, {left:,} numbers left to call.")
    if moved and left and left <= max(50, first * 0.05):
        tell("near", f"calls almost finish - only {left:,} numbers left ({done_today:,} called today).")
    if moved and not left and not seen["live"]:
        tell("finished", f"all calls finished. {done_today:,} numbers called today. want me check who's "
                         f"interested? just say check the calls.")
    if moved and left and not seen["live"] and time.time() - moved > STUCK:
        tell("stuck", f"calls stopped moving leh - nothing called for {int((time.time() - moved) // 60)} min, "
                      f"still {left:,} numbers left.{note}")
    elif moved and seen["live"] and "stuck" in sent:  # moving again after being stuck: tell again if it stops later
        sent.remove("stuck")
    if not moved and left and now.strftime("%H:%M") >= "09:30":
        tell("not_started", f"ipline not calling yet - still {left:,} numbers waiting.{note}")
    ai_call._save(STATE, st)
    return st


def progress():
    """For the assistant: today's calling progress as last seen."""
    st = load()
    if not st:
        return {"result": "not watched yet - it looks every 10 min during ipline's calling hours"}
    return {"day": st.get("day"), "numbers_left": st.get("left"), "on_the_line_now": st.get("live"),
            "called_today": max(0, (st.get("first_left") or 0) - (st.get("left") or 0)),
            "ipline_message": st.get("note") or None, "finished": "finished" in st.get("sent", []),
            "last_checked": datetime.fromtimestamp(st["at"]).strftime("%H:%M") if st.get("at") else None}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(read_ipline(), ensure_ascii=False))
