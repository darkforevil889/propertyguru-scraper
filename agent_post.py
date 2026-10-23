"""Post a saved owner listing to PropertyGuru through AgentNet (your agent account).

Fills AgentNet's "Create Listing" form from a saved listing (Saved listings tab) with your usual choices:
sale / rent (rent: entire unit), available immediately, the building from PropertyGuru's search, condominium,
intermediate unit (unless the owner says studio / penthouse / corner ...), strata title for condos (individual for
landed), rooms (a studio = 1 bed 1 bath), size (the owner's, else the usual size there - market_check.py), fully
furnished with air-con / bed / fridge / water heater (unless stated otherwise), the price, a headline
"<name> <area> Fully Furnished For Rent", a short description and 5 of the owner's photos (no screenshots, no
videos). "draft" SAVES A DRAFT - nothing is posted and no credits are used; "post" fills and posts straight away;
"post_draft" posts a draft you checked (2 Universal Credits each, PropertyGuru + iProperty, 60 days). Several
listings run one after another in one Chrome window; one that fails is noted with why and the rest carry on.

Runs on this PC in Chrome (its own profile, .chrome-agentnet). Login: AGENTNET_USER / AGENTNET_PASSWORD in .env.
Progress: data/agent_post_status.json; results (draft ids): Supabase storage owner-photos/data/agent_posts.json.

    python agent_post.py --ids <message_id> [...]                     # make drafts for these saved listings
    python agent_post.py --action post_draft --ids <message_id> [...] # post their drafts (uses credits)
"""
import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PROFILE = ROOT / ".chrome-agentnet"
BASE = "https://agentnet.propertyguru.com.my"
STATUS = ROOT / "data" / "agent_post_status.json"
BUCKET, RESULTS = "owner-photos", "data/agent_posts.json"
FILLER = "Available immediately, contact me now for viewing."  # AgentNet wants 20+ words in a description


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)


def status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_batch = {}  # the run's progress (n of N, which listing), added to every status line


def say(text, **extra):
    _save(STATUS, {"state": "running", "at": time.time(), "text": text, **_batch, **extra})


def load_results(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(RESULTS))
    except Exception:
        return {}


def save_result(db, message_id, result):
    results = load_results(db)
    results[message_id] = result
    db.storage.from_(BUCKET).upload(RESULTS, json.dumps(results, ensure_ascii=False).encode(),
                                    {"content-type": "application/json", "upsert": "true"})


# ---------- what goes in the form (your rules) ----------
def plan(r, mk=None):
    """A saved listing (owner_listings row) + its PropertyGuru comparison -> the form's values."""
    from wa_parser import property_category
    mk = mk or {}
    text = " ".join(str(r.get(k) or "") for k in ("message_text", "property_type", "place"))
    low = text.lower()
    category = property_category(r.get("property_type"), r.get("place"), r.get("message_text"))
    residential = category in ("Condo / Apartment", "Bungalow", "Semi-D", "Terrace", "Townhouse", "Unknown")
    landed = category in ("Bungalow", "Semi-D", "Terrace", "Townhouse")
    studio = bool(re.search(r"\bstudio\b", low))
    if re.search(r"service[d]?\s*(residence|apartment|suite)", low):
        sub = "Service Residence"
    elif re.search(r"\bapartment\b", low) and not re.search(r"\bcondo", low):
        sub = "Apartment"
    else:
        sub = "Condominium"  # condos far more often than apartments
    unit_type = next((name for word, name in (
        (r"\bstudio\b", "Studio"), (r"penthouse", "Penthouse"), (r"duplex", "Duplex"), (r"triplex", "Triplex"),
        (r"dual\s*key", "Dual Key"), (r"\bsoho\b", "Soho"), (r"\bloft\b", "Loft"), (r"corner", "Corner Lot"),
        (r"end\s*lot", "End Lot"), (r"ground\s*floor", "Ground Floor Unit")) if re.search(word, low)), "Intermediate")
    beds = r.get("bedrooms") or (1 if studio else None) or mk.get("est_beds")
    beds = 1 if beds is not None and beds <= 0 else beds  # a studio counts as 1 bed 1 bath
    baths = r.get("bathrooms") or (1 if studio else beds)
    sqft = r.get("sqft") or mk.get("est_sqft")
    furn = r.get("furnishing") or ""
    furnishing = ("UNFUR" if re.search(r"unfurnish|bare", furn, re.I) or category == "Factory / Warehouse" else
                  "PART" if re.search(r"partial", furn, re.I) else "FULL")
    words = {"FULL": "Fully Furnished", "PART": "Partially Furnished", "UNFUR": "Unfurnished"}[furnishing]
    rent = r.get("listing_type") == "Rent"
    return {
        "residential": residential, "rent": rent, "category": category, "landed": landed,
        "name": _building(r.get("place") or ""), "sub_type": None if landed or not residential else sub,
        "unit_type": unit_type if residential else None, "title": "Individual" if landed else "Strata",
        "beds": beds if residential else None, "baths": baths if residential else None,
        "sqft": sqft, "sqft_estimated": not r.get("sqft") and bool(sqft),
        "furnishing": furnishing, "furnishing_words": words,
        "furnishing_items": {"FULL": ["AIRCO", "BED", "FRI", "HEATE"], "PART": ["AIRCO", "HEATE"]}.get(furnishing, []),
        "price": r.get("price_rm"), "photos": [p for p in r.get("photo_paths") or [] if p.lower().endswith(".jpg")],
    }


def _building(place):
    """'Plaza Damas, Sri Hartamas (E-0-8 ...)' -> 'Plaza Damas'; 'Axon Bukit Bintang' stays (search trims it)."""
    import market_check
    return market_check.search_name(place) or place


def headline(p, area):
    """"Axon Bukit Bintang Fully Furnished For Rent" (max 70 characters)."""
    name = p["name"] if not area or area.lower() in p["name"].lower() else f"{p['name']} {area}"
    return f"{name} {p['furnishing_words']} For {'Rent' if p['rent'] else 'Sale'}"[:70]


def description(p, name, area, city):
    """Place, area, furnishing, rooms, size, "Looks New" - plus a closing line (AgentNet wants 20+ words)."""
    rooms = (f"{p['beds']} Bedroom{'s' * (p['beds'] > 1)} {p['baths']} Bathroom{'s' * ((p['baths'] or 0) > 1)}"
             if p.get("beds") else "")
    lines = [name, ", ".join(x for x in (area, city) if x), p["furnishing_words"], rooms,
             f"{p['sqft']:,} sqft" if p.get("sqft") else "", "Looks New", FILLER]
    return "\n".join(x for x in lines if x)


# ---------- the browser ----------
def open_browser(p, headless=False):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    ctx = p.chromium.launch_persistent_context(user_data_dir=str(PROFILE), channel="chrome", headless=headless,
                                               viewport={"width": 1400, "height": 1000})
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def goto(page, path):
    """Open an AgentNet page, logging in first when AgentNet asks."""
    page.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    if "/account/login" in page.url:
        page.fill('input[name="username"]', os.environ["AGENTNET_USER"])
        page.fill('input[name="password"]', os.environ["AGENTNET_PASSWORD"])
        try:
            page.check('input[name="remember_me"]')
        except Exception:
            pass
        page.click('button[type="submit"]')
        page.wait_for_url(f"{BASE}/**", timeout=60000)
        page.wait_for_timeout(6000)
        if path.split("#")[0] not in page.url:
            page.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
    return page


def new_listing(page):
    """Create Listing, from scratch: AgentNet offers to continue an unfinished one - start a new one instead
    (saved drafts aren't touched)."""
    goto(page, "/v3/create-listing")
    modal = page.locator(".resume-draft-modal")
    if modal.count():
        modal.get_by_role("button", name="Start a new listing").click()
        page.wait_for_timeout(2500)


def _chip(page, text):
    page.get_by_text(text, exact=True).first.click()
    page.wait_for_timeout(600)


def _next(page):
    page.click("[da-id=footer-next-button]")
    page.wait_for_timeout(4000)


def _dropdown(page, index, option):
    """AgentNet's own dropdowns (input name=select-input, covered by a wrapper): open, pick by text."""
    page.locator("input[name=select-input]").nth(index).click(force=True)
    page.wait_for_timeout(1200)
    page.get_by_text(option, exact=True).filter(visible=True).first.click()
    page.wait_for_timeout(1000)
    got = page.locator("input[name=select-input]").nth(index).input_value()
    if got != option:
        raise RuntimeError(f"couldn't choose \"{option}\" (it shows \"{got}\")")


def _step(page, key, target):
    """Press + / - on a rooms stepper until it shows `target` (bedrooms go Select -> Studio -> 1 -> 2 ...)."""
    value = lambda: page.locator(f"[da-id={key}-increment]").locator("xpath=..").locator(
        ".field-stepper__value").inner_text().strip()
    for _ in range(15):
        v = value()
        n = 0 if v in ("Select", "Studio") else int(re.sub(r"\D", "", v) or 0)
        if v not in ("Select", "Studio") and n == target:
            return
        page.click(f"[da-id={key}-{'increment' if v in ('Select', 'Studio') or n < target else 'decrement'}]")
        page.wait_for_timeout(400)
    raise RuntimeError(f"couldn't set {key} to {target}")


def pick_property(page, name):
    """Type the building name a letter at a time and pick the matching suggestion -> (official name, area, city,
    picked text)."""
    import market_check
    box = page.get_by_placeholder("Search by property name")
    box.click()
    words = {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 3 and w not in market_check.GENERIC}
    # "Axon Bukit Bintang" gives nothing - "Axon" does: try the whole name, then fewer words
    tries = [" ".join(name.split()[:n]) for n in range(len(name.split()), 0, -1)]
    for q in tries:
        box.fill("")
        box.press_sequentially(q, delay=120)
        page.wait_for_timeout(3000)
        options = page.locator("a").filter(has_text=re.compile(r"\d{5}"))
        for i in range(min(options.count(), 10)):
            text = options.nth(i).inner_text().strip()
            first = text.splitlines()[0].lower()
            if words & set(re.findall(r"[a-z0-9]+", first)) or q.lower() in first:
                options.nth(i).click()
                page.wait_for_timeout(4000)
                addr = [x.strip() for x in text.splitlines()[-1].split(",")]
                area = addr[1] if len(addr) >= 3 else (addr[0] if addr else "")
                city = addr[-1] if len(addr) >= 2 else ""
                return text.splitlines()[0].strip(), area, city, text.replace("\n", " ")
    raise RuntimeError(f"\"{name}\" isn't in AgentNet's property search - pick it by hand")


def fill(page, p, photos):
    """Fill every step of Create Listing up to Preview. -> what was filled in (for the review)."""
    done = {}
    new_listing(page)
    say("Listing type…")
    _chip(page, "Residential" if p["residential"] else "Commercial")
    _chip(page, "Rent" if p["rent"] else "Sale")
    if p["rent"]:
        _chip(page, "Entire Unit")
    if page.get_by_text("Immediately", exact=True).count():
        _chip(page, "Immediately")
    _next(page)

    say("Location…")
    official, area, city, picked = pick_property(page, p["name"])
    done["property"] = picked
    p = {**p, "name": official or p["name"]}  # PropertyGuru's own name in the headline / description
    if p["sub_type"]:
        _dropdown(page, 1, p["sub_type"])
    if p["unit_type"]:
        _dropdown(page, 2, p["unit_type"])
    if page.locator(f"text={p['title']}").count():
        page.get_by_text(p["title"], exact=True).first.click()
        page.wait_for_timeout(500)
    done["types"] = " / ".join(page.locator("input[name=select-input]").nth(i).input_value() for i in range(3))
    _next(page)

    say("Unit details…")
    if p["beds"]:
        _step(page, "bedrooms", p["beds"])
        _step(page, "bathrooms", p["baths"] or 1)
    if p["sqft"]:
        page.locator("[da-id=built-up-input] input").first.fill(str(p["sqft"]))
    page.click(f"[da-id=furnishing-{p['furnishing']}]")
    page.wait_for_timeout(1500)
    for k in p["furnishing_items"]:
        if page.locator(f"[da-id=furnishingDetails-{k}]").count():
            page.click(f"[da-id=furnishingDetails-{k}]")
            page.wait_for_timeout(300)
    _next(page)

    say("Price…")
    page.locator("[da-id=selling-price-input] input").first.fill(str(p["price"]))
    _next(page)

    say("Description…")
    done["headline"] = headline(p, area)
    done["description"] = description(p, p["name"], area, city)
    page.locator('textarea[placeholder^="A short sentence"]').first.fill(done["headline"])
    page.locator('textarea[placeholder^="Describe the property"]').first.fill(done["description"])
    page.wait_for_timeout(1000)
    _next(page)  # AgentNet saves a draft from here on
    m = re.search(r"create-listing/(\d+)", page.url)
    done["draft_id"] = m.group(1) if m else None

    say("Uploading photos…", draft_id=done["draft_id"])
    row = page.locator("[da-id=listing-images-section-header]")
    with page.expect_file_chooser() as fc:
        row.locator("button").last.click()
    fc.value.set_files([str(f) for f in photos])
    for _ in range(60):  # wait for the uploads
        page.wait_for_timeout(2000)
        if f"Photos ({len(photos)})" in row.inner_text():
            break
    done["photos"] = len(photos)
    _next(page)  # platform posting
    credits = re.search(r"(\d+)\s+Universal Credits", page.inner_text("body"))
    done["credits"] = int(credits.group(1)) if credits else None
    _next(page)  # preview
    return done


def photo_files(db, paths, folder):
    """Up to 5 of the owner's photos, downloaded - WhatsApp screenshots (prices, chats) left out."""
    from wa_ocr import is_chat_screenshot
    out = []
    for p in paths:
        f = folder / Path(p).name
        try:
            f.write_bytes(db.storage.from_(BUCKET).download(p))
        except Exception:
            continue
        if not is_chat_screenshot(f):
            out.append(f)
        if len(out) == 5:
            break
    return out


def _click_post(page):
    """Preview -> "Post now" -> AgentNet's "Confirm posting ... using 2 Universal Credits?" box -> Confirm.
    -> True when AgentNet then opens your Active listings (what it does once the listing is live)."""
    page.click("[da-id=footer-post-now-button]")
    page.wait_for_timeout(4000)
    box = page.locator("[role=dialog]").filter(visible=True).filter(has_text="Confirm posting")
    if not box.count():
        return False
    box.get_by_role("button", name="Confirm", exact=True).click()
    for _ in range(15):
        page.wait_for_timeout(2000)
        if "listing-management" in page.url:
            return True
    return False


def _one(db, page, r, mk, action, folder):
    """One listing: "draft" = fill + save as draft, "post" = fill + post, "post_draft" = post its saved draft."""
    mid = r["message_id"]
    prev = load_results(db).get(mid) or {}
    if prev.get("state") in ("posted", "check"):  # "check" = Post now pressed, unconfirmed: never pay twice
        raise RuntimeError("already posted (or posting to check in AgentNet) - not posted again")
    if action == "post" and prev.get("draft_id") and prev.get("state") == "draft":
        action = "post_draft"  # it has a draft (the one you checked): post that, don't make another
    if action == "post_draft":
        if not prev.get("draft_id"):
            raise RuntimeError("no draft yet - make a draft first")
        goto(page, f"/v3/create-listing/{prev['draft_id']}")
        # a saved draft reopens at Gallery, with Preview greyed out until you go through the steps: Next, Next
        for _ in range(8):
            if "#/preview" in page.url:
                break
            _next(page)
        if not page.locator("[da-id=footer-post-now-button]").count():
            raise RuntimeError(f"the draft didn't reach Preview (stuck at {page.url.split('#')[-1]}) - open it in "
                               "AgentNet to see what's missing")
        result = {**prev}
    else:
        p = plan(r, mk)
        problems = [x for x, ok in (("no price", p["price"]), ("no building name", p["name"])) if not ok]
        photos = photo_files(db, p["photos"], folder)
        if len(photos) < 5:
            problems.append(f"only {len(photos)} photo(s) - PropertyGuru needs at least 5")
        if problems:
            raise RuntimeError("; ".join(problems))
        done = fill(page, p, photos)
        result = {**done, "plan": {k: v for k, v in p.items() if k != "photos"},
                  "edit_url": f"{BASE}/v3/create-listing/{done['draft_id']}#/preview" if done.get("draft_id") else None}
    result.update(place=r.get("place"), owner=r.get("owner_name"), at=time.time())
    result.pop("error", None)
    try:  # the preview, to look at on the dashboard (also online)
        shot = folder / f"{re.sub(r'[^A-Za-z0-9]', '', mid)[:40]}.png"
        page.screenshot(path=str(shot))
        key = f"agent_post/{shot.name}"
        db.storage.from_(BUCKET).upload(key, shot.read_bytes(), {"content-type": "image/png", "upsert": "true"})
        result["shot"] = key
    except Exception:
        pass
    if action in ("post", "post_draft"):
        say("Posting…")
        ok = _click_post(page)
        result.update(state="posted" if ok else "check", posted_at=time.time(),
                      note=None if ok else "Pressed Post now but AgentNet didn't clearly confirm - check Active "
                                           "Listings in AgentNet")
    else:
        page.click("[da-id=listing-creation-header-save-exit-button]")  # keep it as a draft
        page.wait_for_timeout(5000)
        result["state"] = "draft"
    save_result(db, mid, result)
    return result


STOP = ROOT / "data" / "agent_post_stop"


def run_batch(db, ids, action="draft"):
    """Several saved listings, one after another in one Chrome window. A listing that fails is noted (why) and
    the rest carry on. action: "draft" | "post" | "post_draft"."""
    from patchright.sync_api import sync_playwright
    import market_check
    STOP.unlink(missing_ok=True)
    rows = {r["message_id"]: r for r in db.table("owner_listings").select("*").in_("message_id", ids).execute().data}
    markets = market_check.load_results(db)
    counts = {"ok": 0, "failed": 0}
    word = {"draft": "Saving drafts", "post": "Posting", "post_draft": "Posting drafts"}[action]
    _batch.update(action=action, total=len(ids), ids=ids)
    credits = None
    with sync_playwright() as pw, tempfile.TemporaryDirectory() as tmp:
        ctx, page = open_browser(pw)
        try:
            for i, mid in enumerate(ids, 1):
                if STOP.exists():
                    break
                r = rows.get(mid)
                _batch.update(done=i - 1, id=mid, place=(r or {}).get("place"))
                say(f"{word} {i} of {len(ids)}: {(r or {}).get('place')}")
                try:
                    if not r:
                        raise RuntimeError("this saved listing is gone")
                    _one(db, page, r, markets.get(mid), action, Path(tmp))
                    counts["ok"] += 1
                except Exception as e:
                    counts["failed"] += 1
                    prev = load_results(db).get(mid) or {}
                    save_result(db, mid, {**prev, "state": prev.get("state") or "error", "at": time.time(),
                                          "place": (r or {}).get("place"), "owner": (r or {}).get("owner_name"),
                                          "error": str(e).splitlines()[0][:200]})
            try:  # credits left, shown on the posting page
                credits = int(re.sub(r"\D", "", page.locator("[da-id=credit-type-dropdown-trigger]").first
                                     .inner_text()) or 0) or None
            except Exception:
                pass
        finally:
            ctx.close()
    _batch.clear()
    _save(STATUS, {"state": "done", "at": time.time(), "action": action, "total": len(ids), **counts,
                   "credits": credits, "stopped": STOP.exists()})
    return counts


def start(ids, action="draft"):
    """Run in the background on this PC (the dashboard's buttons)."""
    import subprocess
    import sys
    ids = [ids] if isinstance(ids, str) else list(ids)
    _save(STATUS, {"state": "running", "at": time.time(), "text": "Starting Chrome…", "action": action,
                   "total": len(ids), "done": 0, "ids": ids})
    (ROOT / "logs").mkdir(exist_ok=True)
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(python if python.exists() else sys.executable), "-u", str(ROOT / "agent_post.py"),
                      "--action", action, "--ids", *ids], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                     stdout=open(ROOT / "logs" / "agent_post.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)


def stop():
    STOP.parent.mkdir(parents=True, exist_ok=True)
    STOP.write_text("stop", encoding="utf-8")


def request(db, ids, action="draft"):
    """Online dashboard: ask the PC to do it."""
    db.table("pc_tasks").insert({"kind": "agent_post", "params": {"ids": list(ids), "action": action}}).execute()


def request_stop(db):
    db.table("pc_tasks").insert({"kind": "agent_post_stop", "params": {}}).execute()


def run_requests(db, log=print):
    """PC side (keeper.py): drafts / posts asked for on the online dashboard, one batch at a time."""
    for t in db.table("pc_tasks").select("id").eq("kind", "agent_post_stop").eq("status", "pending").execute().data:
        stop()
        db.table("pc_tasks").update({"status": "started"}).eq("id", t["id"]).execute()
    tasks = db.table("pc_tasks").select("id,params").eq("kind", "agent_post").eq("status", "pending") \
        .order("id").execute().data
    s = status()
    if not tasks or (s.get("state") == "running" and time.time() - s.get("at", 0) < 600):
        return
    t = tasks[0]
    prm = t.get("params") or {}
    ids = prm.get("ids") or [prm["id"]]
    action = prm.get("action") or ("post_draft" if prm.get("post") else "draft")
    start(ids, action)
    db.table("pc_tasks").update({"status": "started"}).eq("id", t["id"]).execute()
    log(f"PropertyGuru {action} started from the online page: {len(ids)} listing(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="+", required=True, help="saved listings (message ids)")
    ap.add_argument("--action", default="draft", choices=["draft", "post", "post_draft"])
    a = ap.parse_args()
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(ROOT / ".env")
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    try:
        print(run_batch(db, a.ids, a.action))
    except Exception as e:
        _save(STATUS, {"state": "error", "at": time.time(), "error": str(e)[:300]})
        raise
