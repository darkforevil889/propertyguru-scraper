"""Google Map Extractor: search Google Maps ("property agency Mont Kiara") and list each business found -
company name, category, address, postcode, state, website, email, phone number.

Runs on this PC in a hidden Chrome window (Google blocks servers). Google Maps itself has no emails: they are read
from each company's own website (its home page, then its contact page).
Each search is saved to Supabase storage owner-photos/data/gmaps/<id>.json (list: data/gmaps/index.json), so the
online dashboard shows it too; "Extract" pressed online reaches this PC through pc_tasks (keeper.py).

    python gmaps.py "property agency mont kiara" --max 50
    python gmaps.py "condo management office kl" --max 20 --no-email
"""
import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus, unquote, urljoin, urlparse

ROOT = Path(__file__).parent.resolve()
OUT = ROOT / "data" / "gmaps"
STATUS = ROOT / "data" / "gmaps_status.json"
STOP = OUT / "STOP"
BUCKET, INDEX = "owner-photos", "data/gmaps/index.json"
PROFILE = ROOT / ".chrome-gmaps"  # its own Chrome profile: can run while the PropertyGuru check uses the other one

STATES = {  # how a state is written in an address -> its name
    "wilayah persekutuan kuala lumpur": "Kuala Lumpur", "federal territory of kuala lumpur": "Kuala Lumpur",
    "kuala lumpur": "Kuala Lumpur", "selangor": "Selangor", "johor": "Johor", "pulau pinang": "Penang",
    "penang": "Penang", "perak": "Perak", "pahang": "Pahang", "negeri sembilan": "Negeri Sembilan",
    "melaka": "Melaka", "malacca": "Melaka", "kedah": "Kedah", "kelantan": "Kelantan", "terengganu": "Terengganu",
    "sabah": "Sabah", "sarawak": "Sarawak", "perlis": "Perlis", "putrajaya": "Putrajaya", "labuan": "Labuan",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
NOT_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$|@\dx\.|sentry|wixpress|example\.|domain\.com|"
                       r"yourname|youremail|email\.com$|@sentry|godaddy|cloudflare", re.I)
NO_EMAIL_SITES = re.compile(r"facebook\.com|instagram\.com|wa\.me|whatsapp\.com|linktr\.ee|tiktok\.com|"
                            r"google\.com|goo\.gl|youtube\.com|twitter\.com|x\.com/", re.I)


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


def split_address(address):
    """'J-2-7, Jalan Solaris, 50480 Kuala Lumpur, Wilayah Persekutuan Kuala Lumpur' -> ('50480', 'Kuala Lumpur')."""
    a = address or ""
    codes = re.findall(r"\b(\d{5})\b", a)
    low = a.lower()
    state = None
    for words, name in STATES.items():  # the state named last in the address
        i = low.rfind(words)
        if i >= 0 and (state is None or i > state[0]):
            state = (i, name)
    return (codes[-1] if codes else None), (state[1] if state else None)


def whatsapp_number(phone):
    """Malaysian mobile numbers (01x) can get WhatsApp - '012-345 6789' -> '+60123456789'. Landlines -> None."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("60"):
        d = "0" + d[2:]
    return "+6" + d if re.fullmatch(r"01\d{8,9}", d) else None


PLACE_JS = """() => {
  const q = s => document.querySelector(s);
  const label = s => { const e = q(s); return e ? (e.getAttribute('aria-label') || e.innerText || '') : ''; };
  const ph = q('button[data-item-id^="phone:tel:"]');
  const stars = q('div.F7nice span[aria-hidden="true"]');
  return {name: (q('h1') || {}).innerText || '',
          category: (q('button.DkEaL') || {}).innerText || '',
          address: label('button[data-item-id="address"]').replace(/^Address:\\s*/, '').trim(),
          website: (q('a[data-item-id="authority"]') || {}).href || '',
          phone: ph ? (ph.getAttribute('aria-label') || '').replace(/^Phone:\\s*/, '').trim() : '',
          rating: stars ? stars.innerText : ''};
}"""


def search(page, query, limit, progress=print):
    """The Google Maps results for a search -> [(name, link)], up to `limit` (Google shows about 120 at most)."""
    page.goto(f"https://www.google.com/maps/search/{quote_plus(query)}?hl=en", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_timeout(3500)
    if "/maps/place/" in page.url:  # only one match: Google opens it straight away
        return [(page.title().replace(" - Google Maps", ""), page.url)]
    feed = page.locator('div[role="feed"]')
    if not feed.count():
        return []
    links, still = [], 0
    while len(links) < limit and still < 8:  # Google sometimes pauses before loading more
        feed.evaluate("el => el.scrollBy(0, 4000)")
        page.wait_for_timeout(1800)
        now = page.eval_on_selector_all("a.hfpxzc", "els => els.map(e => [e.getAttribute('aria-label'), e.href])")
        still = still + 1 if len(now) == len(links) else 0
        links = now
        progress(f"Found {len(links)} so far…")
        if page.locator("text=You've reached the end of the list").count():
            break
    return [tuple(x) for x in links[:limit]]


def _decode_cf(hexstr):
    """Cloudflare hides emails as data-cfemail="…" (XOR with the first byte)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def _clean(found):
    out = []
    for e in found:
        e = e.strip(".").lower()
        if EMAIL.fullmatch(e) and not NOT_EMAIL.search(e) and e not in out:
            out.append(e)
    return out


def emails_in(html, host):
    """The company's emails on a page. Trusted: "email us" links (mailto:), Cloudflare-protected ones, and addresses
    at the website's own domain. Any other address in the text only when there are 1-2 of them - a page full of
    random gmail addresses is blog comments / spam, not the company."""
    html = html or ""
    sure = _clean([unquote(m).strip() for m in re.findall(r'mailto:([^"\'?<>\s]+)', html, re.I)]
                  + [_decode_cf(h) for h in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html)])
    text = _clean(EMAIL.findall(re.sub(r"<[^>]+>", " ", html)))
    own = [e for e in text if host and (e.split("@")[1] in host or host.endswith(e.split("@")[1]))]
    found = list(dict.fromkeys(sure + own))
    if not found and len(text) <= 2:
        found = text
    return found


def _base(host):
    """'www.shop.example.com.my' -> 'example.com.my' (the registered name, roughly)."""
    parts = (host or "").lower().split(":")[0].removeprefix("www.").split(".")
    keep = 3 if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "edu", "gov", "co") else 2
    return ".".join(parts[-keep:])


# The company's own "link in bio" pages - a website that sends you there is still theirs
LINK_PAGES = re.compile(r"linktr\.ee|beacons\.ai|bio\.link|carrd\.co|taplink|wixsite\.com|business\.site|"
                        r"facebook\.com|instagram\.com", re.I)


def _same_site(a, b):
    return _base(a) == _base(b)


def find_email(page, website):
    """Emails on a company's website: its home page, else its contact / about page. -> 'a@x.com; b@x.com' or ''."""
    if not website or NO_EMAIL_SITES.search(website):
        return ""
    host = urlparse(website).netloc.lower().removeprefix("www.")
    try:
        page.goto(website, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(2000)
        now = urlparse(page.url).netloc
        if not _same_site(now, host) and not LINK_PAGES.search(now):
            return ""  # sends you to another company's site (expired / taken-over domain) - not their emails
        found = emails_in(page.content(), host)
        if not found:
            here = urlparse(page.url).netloc
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, e.innerText || ''])")
            contact = [h for h, t in links if urlparse(h).netloc == here
                       and re.search(r"contact|hubungi|about|enquir", f"{h} {t}", re.I)]
            for h in list(dict.fromkeys(contact))[:2]:
                page.goto(urljoin(page.url, h), wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1500)
                found = emails_in(page.content(), host)
                if found:
                    break
        # the company's own address first ("info@company.com" before a web designer's)
        found.sort(key=lambda e: 0 if host and (e.split("@")[1] in host or host.endswith(e.split("@")[1])) else 1)
        return "; ".join(found[:3])
    except Exception:
        return ""


def upload(db, job):
    """The search up to Supabase (shown on both dashboards) and its line in the list of searches."""
    if not db:
        return
    store = db.storage.from_(BUCKET)
    opts = {"content-type": "application/json", "upsert": "true"}
    store.upload(f"data/gmaps/{job['id']}.json", json.dumps(job, ensure_ascii=False).encode(), opts)
    index = load_index(db)
    index = [j for j in index if j["id"] != job["id"]] + [{k: job.get(k) for k in (
        "id", "query", "at", "state", "max", "emails")} | {"count": len(job["rows"]),
                                                             "areas": len(job.get("areas") or [])}]
    store.upload(INDEX, json.dumps(index, ensure_ascii=False).encode(), opts)


def load_index(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(INDEX))
    except Exception:
        return []


def load_job(db, job_id):
    try:
        return json.loads(db.storage.from_(BUCKET).download(f"data/gmaps/{job_id}.json"))
    except Exception:
        return None


def delete_job(db, job_id):
    db.storage.from_(BUCKET).remove([f"data/gmaps/{job_id}.json"])
    index = [j for j in load_index(db) if j["id"] != job_id]
    db.storage.from_(BUCKET).upload(INDEX, json.dumps(index, ensure_ascii=False).encode(),
                                    {"content-type": "application/json", "upsert": "true"})


# "Split by area": Google Maps stops at about 120 places a search, so a big search runs once per area
# ("property agency Bangsar", "property agency Mont Kiara", ...) and the results are joined, each place once.
AREAS = {
    "Kuala Lumpur": [
        "KLCC", "Bukit Bintang", "Jalan Ampang", "Ampang Hilir", "Mont Kiara", "Sri Hartamas", "Desa Sri Hartamas",
        "Bangsar", "Bangsar South", "Mid Valley", "Brickfields", "Damansara Heights", "Bukit Damansara",
        "Taman Tun Dr Ismail", "Segambut", "Desa ParkCity", "Kepong", "Bandar Menjalara", "Jinjang", "Sentul",
        "Setapak", "Wangsa Maju", "Titiwangsa", "Setiawangsa", "Keramat", "Chow Kit", "Pudu", "Cheras",
        "Taman Connaught", "Bandar Tun Razak", "Sri Petaling", "Bukit Jalil", "OUG", "Kuchai Lama", "Taman Desa",
        "Old Klang Road", "Seputeh", "Sungai Besi", "Salak Selatan"],
    "Selangor": [
        "Petaling Jaya", "Damansara Utama", "Bandar Utama", "Kota Damansara", "Mutiara Damansara", "Ara Damansara",
        "Kelana Jaya", "Subang Jaya", "USJ", "Sunway", "Puchong", "Shah Alam", "Setia Alam", "Kota Kemuning",
        "Bukit Jelutong", "Glenmarie", "Klang", "Bandar Botanic", "Cyberjaya", "Seri Kembangan", "Serdang",
        "Kajang", "Bangi", "Semenyih", "Balakong", "Ampang", "Pandan Indah", "Selayang", "Batu Caves", "Rawang",
        "Sungai Buloh", "Sri Damansara", "Puncak Alam", "Sepang", "Dengkil"],
}


def place_key(link):
    """The same place has the same id in every search's link."""
    return (re.search(r"!19s([^?&!]+)", link) or re.search(r"!1s([^!]+)", link) or [None, link.split("?")[0]])[1]


def run(query, limit=50, emails=True, db=None, job_id=None, areas=None):
    """Search Google Maps and open each result (and its website for emails) -> the saved search.
    areas = ["Bangsar", "Mont Kiara", ...]: one search per area ("<query> Bangsar"), each place once, up to
    `limit` places in all."""
    from patchright.sync_api import sync_playwright
    job = {"id": job_id or uuid.uuid4().hex[:10], "query": query, "max": limit, "emails": emails,
           "areas": areas or [], "at": time.time(), "state": "running", "rows": []}
    STOP.unlink(missing_ok=True)
    searches = [(f"{query} {a}", a) for a in areas] if areas else [(query, None)]

    def say(text, done=0, total="?"):
        _save(STATUS, {"state": "running", "at": time.time(), "id": job["id"], "query": query, "done": done,
                       "total": total, "text": text, "areas": len(areas or [])})

    def save_up():
        _save(OUT / f"{job['id']}.json", job)
        try:
            upload(db, job)
        except Exception:
            pass

    say("Opening Google Maps…")
    seen = set()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(user_data_dir=str(PROFILE), channel="chrome", headless=True,
                                                   locale="en-US")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            site_tab = ctx.new_page()  # websites open here, so Google Maps stays put
            for n, (q, area) in enumerate(searches, 1):
                if STOP.exists() or len(job["rows"]) >= limit:
                    break
                where = f"Area {n} of {len(searches)}: {area} · " if area else ""
                total = limit if area else "?"
                links = search(page, q, 120 if area else limit,
                               progress=lambda t: say(where + t, len(job["rows"]), total))
                new = [(name, link) for name, link in links if place_key(link) not in seen]
                if not area:
                    total = len(new)
                for name, link in new:  # open each new place: its details, then its website for an email
                    if STOP.exists() or len(job["rows"]) >= limit:
                        break
                    if place_key(link) in seen:
                        continue  # Google sometimes lists a place twice in one search
                    seen.add(place_key(link))
                    say(f"{where}{name}", len(job["rows"]), total)
                    try:
                        page.goto(link, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_selector("h1", timeout=15000)
                        page.wait_for_timeout(1200)
                        info = page.evaluate(PLACE_JS)
                    except Exception as e:
                        info = {"name": name, "category": "", "address": "", "website": "", "phone": "",
                                "rating": "", "error": str(e).splitlines()[0][:100]}
                    postcode, state = split_address(info.get("address"))
                    row = {"name": info.get("name") or name, "category": info.get("category"),
                           "address": info.get("address"), "postcode": postcode, "state": state,
                           "website": info.get("website"), "email": "", "phone": info.get("phone"),
                           "whatsapp": whatsapp_number(info.get("phone")), "rating": info.get("rating"),
                           "maps_url": link.split("?")[0], "area": area}
                    if emails and row["website"]:
                        say(f"{where}{row['name']} - looking for an email on its website", len(job["rows"]), total)
                        row["email"] = find_email(site_tab, row["website"])
                    job["rows"].append(row)
                    if len(job["rows"]) % 5 == 0:  # rows appear on the dashboard as they come
                        save_up()
                job["areas_done"] = n
                save_up()
        finally:
            ctx.close()
    job["state"] = "stopped" if STOP.exists() else "done"
    job["finished"] = time.time()
    save_up()
    _save(STATUS, {"state": "done", "at": time.time(), "id": job["id"], "query": query, "done": len(job["rows"]),
                   "total": len(job["rows"])})
    return job


def start(query, limit=50, emails=True, areas=None):
    """Run a search in the background on this PC. -> its id (the dashboard shows it when it's done)."""
    import subprocess
    import sys
    job_id = uuid.uuid4().hex[:10]
    _save(STATUS, {"state": "running", "at": time.time(), "id": job_id, "query": query, "done": 0, "total": "?",
                   "text": "Starting…"})
    _save(OUT / f"{job_id}.params.json", {"query": query, "max": limit, "emails": emails, "areas": areas or []})
    (ROOT / "logs").mkdir(exist_ok=True)
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(python if python.exists() else sys.executable), "-u", str(ROOT / "gmaps.py"),
                      "--id", job_id], cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # company names in any language
                     stdout=open(ROOT / "logs" / "gmaps.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    return job_id


def stop():
    OUT.mkdir(parents=True, exist_ok=True)
    STOP.write_text("stop", encoding="utf-8")


def request(db, query, limit, emails, areas=None):
    """Online dashboard: ask the PC to run this search."""
    db.table("pc_tasks").insert({"kind": "gmaps", "params": {"query": query, "max": limit, "emails": emails,
                                                             "areas": areas or []}}).execute()


def request_stop(db):
    db.table("pc_tasks").insert({"kind": "gmaps_stop", "params": {}}).execute()


def run_requests(db, log=print):
    """PC side (keeper.py): searches / stops asked for on the online dashboard, one search at a time."""
    for t in db.table("pc_tasks").select("id").eq("kind", "gmaps_stop").eq("status", "pending").execute().data:
        stop()
        db.table("pc_tasks").update({"status": "started"}).eq("id", t["id"]).execute()
    tasks = db.table("pc_tasks").select("id,params").eq("kind", "gmaps").eq("status", "pending") \
        .order("id").execute().data
    s = status()
    if not tasks or (s.get("state") == "running" and time.time() - s.get("at", 0) < 300):
        return
    t = tasks[0]
    p = t.get("params") or {}
    start(p.get("query") or "", int(p.get("max") or 50), bool(p.get("emails", True)), p.get("areas") or None)
    db.table("pc_tasks").update({"status": "started"}).eq("id", t["id"]).execute()
    log(f"Google Map Extractor started from the online page: {p.get('query')!r}"
        + (f" in {len(p['areas'])} areas" if p.get("areas") else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--max", type=int, default=50)
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--areas", help='"Bangsar|Mont Kiara|..." or a region: "Kuala Lumpur"')
    ap.add_argument("--id")
    a = ap.parse_args()
    params = OUT / f"{a.id}.params.json"  # from start(): the dashboard's search
    if a.id and params.exists():
        pr = json.loads(params.read_text(encoding="utf-8"))
        a.query, a.max, a.no_email, areas = pr["query"], pr["max"], not pr["emails"], pr.get("areas") or None
    else:
        areas = (AREAS.get(a.areas) or a.areas.split("|")) if a.areas else None
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(ROOT / ".env")
    try:
        db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    except Exception:
        db = None
    try:
        j = run(a.query, a.max, not a.no_email, db, a.id, areas)
        print(f"{a.query!r}: {len(j['rows'])} places, {sum(1 for r in j['rows'] if r['email'])} with email")
    except Exception as e:
        _save(STATUS, {"state": "error", "at": time.time(), "id": a.id, "query": a.query, "error": str(e)[:300]})
        raise
