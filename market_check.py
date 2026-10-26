"""Compare saved owner listings with what's on PropertyGuru right now: is the owner's price OK to post?

For each listing: find its property on PropertyGuru by name (search-box suggestions, plain HTTP), read the
current rent or sale listings there (needs real Chrome on this PC - PropertyGuru blocks servers), compare the
owner's price with similar-sized units (or RM per sq ft), and give a verdict with the reason.
Results: Supabase storage owner-photos/data/market_check.json (the dashboard's Saved listings tab shows them).

    python market_check.py            # every saved listing
    python market_check.py --ids a b  # only these listings (message ids)
"""
import argparse
import json
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
STATUS = ROOT / "data" / "market_check_status.json"
BUCKET, RESULTS = "owner-photos", "data/market_check.json"
BASE = "https://www.propertyguru.com.my"
# Both sites belong to one company: same building numbers, same search links and page data
SITES = {"pg": ("PropertyGuru", BASE), "ip": ("iProperty", "https://www.iproperty.com.my")}
GENERIC = {"the", "residence", "residences", "residency", "suite", "suites", "condo", "condominium", "tower",
           "towers", "park", "jalan", "taman", "@", "klcc", "kl", "and", "at", "of", "city"}


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


def load_results(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(RESULTS))
    except Exception:
        return {}


def search_name(place):
    """'sky suite B15-06' -> 'sky suite', 'Plaza Damas, Sri Hartamas (E-0-8 ...)' -> 'Plaza Damas'."""
    s = re.split(r"[,(@]", place or "")[0]
    s = re.sub(r"\b[A-Z]{0,2}-?\d+[A-Z]?(?:-\d+[A-Z]?)+\b", " ", s, flags=re.I)  # unit numbers B15-06, 33A-8
    return re.sub(r"\s+", " ", s).strip(" -&")


# Towns outside KL / Selangor: if a listing names one, the PropertyGuru match must be there too
# ("Taman Segar, Segamat" is not Taman Segar in Cheras)
FAR = re.compile(r"segamat|johor|muar|batu pahat|kluang|penang|pulau pinang|sabah|kota kinabalu|sarawak|kuching|"
                 r"melaka|malacca|ipoh|perak|negeri sembilan|seremban|kedah|kelantan|terengganu|pahang|kuantan|"
                 r"perlis|langkawi", re.I)
KL_SEL = re.compile(r"kuala lumpur|klcc|selangor|wilayah|mont kiara|kiara|petaling|subang|shah alam|cheras|ampang|"
                    r"bangsar|damansara|hartamas|puchong|klang|cyberjaya|putrajaya|setapak|sentul|kepong|usj", re.I)


def find(place):
    """The PropertyGuru property for a listing's place, or None if nothing clearly matches."""
    import price_lookup
    q = search_name(place)
    if len(q) < 3:
        return None
    words = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) >= 3 and w not in GENERIC}
    far = FAR.search(place or "")
    matches = []
    compact = re.sub(r"\W", "", q.lower())
    # "Axon Bukit Bintang" finds nothing, "Axon" does: drop words from the end (the area people add after the
    # name) - but then the dropped words must be in the property's address (Axon in Bukit Bintang, not elsewhere)
    parts, found = q.split(), []
    for cut in range(len(parts), 0, -1):
        head, tail = " ".join(parts[:cut]), " ".join(parts[cut:]).lower()
        if len(head) < 3:
            break
        found = [p for p in price_lookup.find_property(head, limit=10)
                 if not tail or tail in f"{p['name']} {p['address']}".lower()]
        if found:
            break
    for prop in found:
        name_words = set(re.findall(r"[a-z0-9]+", prop["name"].lower()))
        where = f"{prop['name']} {prop['address']}".lower()
        joined = re.sub(r"\W", "", prop["name"].lower())  # "PV 128" = "Platinum Mondrian PV128"
        if not (words & name_words or (len(compact) >= 4 and compact in joined)
                or (not words and q.lower() in prop["name"].lower())):
            continue
        if far and far.group().lower() not in where:
            continue  # same name, different town
        matches.append(prop)
    # your area first: KL / Selangor before same-named places elsewhere (Sky Suites KLCC, not Kota Kinabalu)
    matches.sort(key=lambda m: 0 if (far or KL_SEL.search(f"{m['name']} {m['address']}")) else 1)
    return matches[0] if matches else None


def other_names(listing):
    """More names to try on PropertyGuru than the title: what's in brackets ("Puncak Sentul (Sentul Point)") and the
    building / street parts of the address ("G-01, Kelana Damansara Suite, No. 3, ...")."""
    out = re.findall(r"\(([^)]{4,40})\)", listing.get("place") or "")
    for part in re.split(r"[,\n]", listing.get("address") or ""):
        part = part.strip()
        if len(part) >= 5 and not re.match(r"^(no\.?\s*\d|\d{5}|jalan|jln|lorong|lot\b|[A-Z]?-?\w{0,3}-\w+)", part, re.I) \
                and not FAR.search(part) and not re.search(r"\d{5}|selangor|kuala lumpur|wp\b", part, re.I):
            out.append(part)
    return list(dict.fromkeys(out))


def find_any(listing):
    """The listing's building on PropertyGuru - by its title, else by other names in its address."""
    for name in [listing.get("place")] + other_names(listing):
        prop = find(name if name == listing.get("place") else f"{name}, {listing.get('place') or ''}") \
            if name else None
        if prop:
            return prop
    return None


PG_PROJECT = re.compile(r"propertyguru\.com\.my/(?!property-listing|property-for)[a-z-]+/([a-z0-9-]+?)-(\d{2,7})"
                        r"(?:[/?#\"&]|$)", re.I)


GOOGLE_RESULTS = """() => [...document.querySelectorAll('h3')].map((h, i) => {
  const blk = h.closest('div[data-hveid], div.g, div[jscontroller]') || h.parentElement;
  return {i, cite: blk ? ((blk.querySelector('cite') || {}).innerText || '') : '', title: h.innerText};
})"""


def google_find(page, listing):
    """When PropertyGuru's own search doesn't know the name (misspelt, nickname): Google "<name> PropertyGuru",
    click the first PropertyGuru result (Google hides the real links) and, if it lands on a building page, use that
    building. -> {'id', 'name'} or None."""
    from urllib.parse import quote_plus
    place = listing.get("place") or ""
    q = f"{search_name(place)} PropertyGuru"
    try:
        page.goto(f"https://www.google.com/search?q={quote_plus(q)}&hl=en", wait_until="domcontentloaded",
                  timeout=30000)
        page.wait_for_timeout(2500)
        hit = next((r for r in page.evaluate(GOOGLE_RESULTS) if "propertyguru.com.my" in r["cite"]), None)
        if not hit:
            return None
        page.locator("h3").nth(hit["i"]).click()
        page.wait_for_url("**propertyguru.com.my/**", timeout=30000)
    except Exception:
        return None
    m = PG_PROJECT.search(page.url)
    if m and not re.search(r"/(bm/)?(hartanah|senarai|property-for|property-listing)", page.url):
        return {"id": int(m.group(2)), "name": m.group(1).replace("-", " ").title(), "address": "",
                "via": "Google"}
    return None


def find_freetext(listing):
    """Shops / houses: PropertyGuru's free-text search for the listing's taman + town ("Taman Muhibbah Segamat") -
    the same street area, closer than the whole town. -> {'name', 'query'} or None."""
    from urllib.parse import quote_plus
    text = " , ".join(x for x in (listing.get("address"), listing.get("place")) if x)
    parts = [p.strip() for p in re.split(r"[,\n(]", text) if p.strip()]
    far = FAR.search(text)
    town = far.group() if far else next((p for p in parts if KL_SEL.search(p) and not re.search(r"\d", p)), "")
    taman = next((p for p in parts if re.match(r"(taman|tmn|bandar|kawasan|seksyen|kampung|kg|usj|ss)\b", p, re.I)),
                 None)
    if not taman:
        return None
    words = re.sub(r"\s+", " ", f"{taman} {town}").strip()
    return {"name": words.title(), "query": f"freetext={quote_plus(words)}"}


def _autocomplete(q):
    import urllib.parse
    import urllib.request
    import price_lookup
    qs = urllib.parse.urlencode({"marketplace": "pgmy", "locale": "en", "limit": 10, "query": q})
    req = urllib.request.Request(f"{price_lookup.AUTOCOMPLETE}?{qs}", headers={
        "User-Agent": price_lookup.UA, "Origin": BASE, "Referer": BASE + "/", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def find_area(listing):
    """No named building (shops, houses): the listing's area on PropertyGuru - its taman / township, else its town.
    -> {'name', 'query'} where query is the search filter for that area, or None."""
    text = " , ".join(x for x in (listing.get("address"), listing.get("place")) if x)
    parts = [p.strip() for p in re.split(r"[,\n(]", text) if p.strip()]
    far = FAR.search(text)
    town = far.group() if far else next((p for p in parts if KL_SEL.search(p) and not re.search(r"\d", p)), "")
    tamans = [p for p in parts if re.match(r"(taman|tmn|bandar|kawasan|seksyen|kampung|kg|usj|ss)\b", p, re.I)]
    for q in [f"{t} {town}".strip() for t in tamans] + ([town] if town else []):
        for it in _autocomplete(q):
            if it.get("objectType") not in ("township", "city", "district", "area", "region"):
                continue
            label = f"{(it.get('displayText') or {}).get('en', '')}, {(it.get('displayDescription') or {}).get('en', '')}"
            if town and town.lower() not in label.lower():
                continue  # same name, different town
            loc = (it.get("meta") or {}).get("location") or {}
            codes = [f"regionCode={loc['level300']}" if loc.get("level300") else "",
                     f"districtCode={loc['level600']}" if loc.get("level600") else "",
                     f"areaCode={loc['level700']}" if loc.get("level700") else ""]
            return {"name": label.strip(", "), "query": "&".join(c for c in codes if c)}
    return None


def search_url(site, kind, where, commercial):
    """A site's search link: where = "propertyId=22311" (a building) or an area filter. Shops / offices are in the
    commercial section - the homes section of Plaza Damas doesn't show its shop lots."""
    path = f"commercial-properties-for-{kind}" if commercial else f"property-for-{kind}"
    return f"{SITES[site][1]}/{path}?listingType={kind}&{where}&isCommercial={'true' if commercial else 'false'}"


def market(fetch, where, kind, commercial=False, site="pg"):
    """Current listings of a building or an area on one site: the cheapest 40, a page from the middle and the
    dearest 20."""
    import scraper
    seen, out, base = set(), [], SITES[site][1]
    # pages 1-2 of the cheapest, one page from the middle of the price range and page 1 of the dearest - so the
    # "typical" price is the real middle, not just the bottom of the market
    plan, total_pages = [("asc", 1), ("asc", 2), ("mid", 0), ("desc", 1)], 1
    for order, page in plan:
        if order == "mid":
            if total_pages <= 3:
                continue  # 3 pages or fewer: the other pages already cover it
            order, page = "asc", (total_pages + 1) // 2
        elif page == 2 and total_pages < 2:
            continue  # there is no page 2
        elif order == "desc" and total_pages < 2:
            continue
        data = fetch(f"{search_url(site, kind, where, commercial)}&sort=price&order={order}&page={page}")
        if order == "asc" and page == 1:
            total_pages = (data.get("paginationData") or {}).get("totalPages") or 1
        for item in data.get("listingsData") or []:
            c = scraper.parse_card(item)
            if c and c["listing_id"] not in seen and c["price_rm"]:
                seen.add(c["listing_id"])
                out.append({"price": c["price_rm"], "sqft": c["floor_area_sqft"], "beds": c["bedrooms"],
                            "type": c.get("property_type"), "site": site, "id": c["listing_id"],
                            "psf": c["price_rm"] / c["floor_area_sqft"] if c["floor_area_sqft"] else None,
                            "url": base + c["url"] if (c.get("url") or "").startswith("/") else c.get("url")})
        if len(out) < 20:
            break  # fewer than a page - we have them all
    import price_lookup
    keep = set(price_lookup._trim([m["price"] for m in out]))  # drop obvious typos
    return [m for m in out if m["price"] in keep]


def merge(pg, ip):
    """Both sites' listings as one list. The same ad on both (same listing number, or same price and size) counts
    once, marked site 'both'."""
    out = [dict(m) for m in pg]
    for u in ip:
        twin = next((m for m in out if m["site"] == "pg" and (m["id"] == u["id"] or (
            m["price"] == u["price"] and m["sqft"] == u["sqft"]))), None)
        if twin:
            twin["site"], twin["ip_url"] = "both", u["url"]
        else:
            out.append(dict(u))
    return out


def _rm(x, rent):
    return f"RM {x:,.0f}" + ("/mo" if rent else "")


# ---------- is a cheap PropertyGuru listing real, or bait? (its own description) ----------
LELONG = re.compile(r"lelong|auction|bank sale|reserve price|bidding|pre-?auction|lelongan", re.I)
# The listed price isn't the real price -> fake / bait
BAIT = re.compile(r"starting (?:from|price|at)|start(?:s|ing)? from|price(?:s)? from|from rm|call (?:for|to get) "
                  r"(?:the )?(?:actual|best|real|latest) price|price on request|booking fee", re.I)
# Real price, but not like-for-like (Bumiputera-only) -> left out of the comparison, marked "check"
BUMI = re.compile(r"bumi(?:putera)?\s*(?:lot|price|only|unit)|\bbumi\b", re.I)
# Worth a look, but not fake
CHECK_WORDS = re.compile(r"many units?|more units?|other units?|several units?|new launch", re.I)
AMOUNT = re.compile(r"rm\s*([\d,]+(?:\.\d+)?)\s*(k|mil(?:lion)?|m)?\b", re.I)
CHECKS = ROOT / "data" / "pg_listing_checks.json"  # url -> what its description showed (re-checked after a week)


# Amounts that aren't the price: "Cashback RM300k", "save RM250k", "deposit RM 10,000"
NOT_PRICE = re.compile(r"(cash\s*back|rebate|save|saving|discount|deposit|booking|maintenance|service charge|"
                       r"rental|rented|tenanted|valuation|bank value|loan)\W{0,3}(\w+\W{1,3}){0,2}$", re.I)


def _amounts(text):
    out = []
    for m in AMOUNT.finditer(text or ""):
        num, unit = m.groups()
        if NOT_PRICE.search(text[max(0, m.start() - 40):m.start()]):
            continue
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        v *= {"k": 1e3, "m": 1e6}.get((unit or "")[:1].lower(), 1)
        out.append(v)
    return out


def _snip(text, find):
    """A short quote from a description: the words around the first thing find(line) spots (a re.Match)."""
    for line in (text or "").splitlines():
        line = line.strip(" -*•\t")
        m = find(line) if line else None
        if m:
            s, e = max(0, m.start() - 35), min(len(line), m.end() + 35)
            return ("…" if s else "") + line[s:e].strip() + ("…" if e < len(line) else "")
    return None


def _amount_in(test):
    """find() for _snip: an "RM …" amount in the line that passes test(value)."""
    return lambda line: next((m for m in AMOUNT.finditer(line) if not NOT_PRICE.search(
        line[max(0, m.start() - 40):m.start()]) and test((_amounts(m.group(0)) or [0])[0])), None)


def check_listing(fetch, unit, rent):
    """Open one listing (PropertyGuru or iProperty) and read its description -> what it shows: 'fake' / 'check'
    notes and 'said', a quote of what it states ("Asking price RM 3.1mil"). verdict() turns it into real / fake."""
    import scraper
    d = scraper.parse_detail(fetch(unit["url"]))
    text = f"{d.get('headline') or ''}\n{d.get('description') or ''}"
    price, fake, check = unit["price"], [], []
    # "Not a Lelong Unit!" / "bukan lelong" is the opposite
    lelong = _snip(text, lambda line: next((m for m in LELONG.finditer(line) if not re.search(
        r"\b(not|no|non|bukan|tak|tidak)\b[\s-]*(an?\s+)?$", line[:m.start()], re.I)), None))
    if lelong:
        fake.append(f"auction / lelong - \"{lelong}\" - not the real market price")
    bait = _snip(text, BAIT.search)
    if bait:
        fake.append(f"description says \"{bait}\" - the listed price isn't the real price")
    # A different price in its own description (listed RM 4,500 but "RM 5,000" in the text) = bait. Only amounts
    # of the same size as the price count (not deposits, maintenance fees, ...).
    near = [a for a in _amounts(text) if 0.6 * price <= a <= 1.8 * price]
    stated = _snip(text, _amount_in(lambda v: abs(v - price) <= 0.03 * price))
    if near and not stated:
        other = _snip(text, _amount_in(lambda v: 0.6 * price <= v <= 1.8 * price))
        fake.append(f"listed at {_rm(price, rent)} but the description says \"{other}\"")
    bumi = bool(BUMI.search(text))
    if bumi:
        check.append("Bumiputera lot - Bumi-only price, not like-for-like")
    words = _snip(text, CHECK_WORDS.search)
    if words:
        check.append(f"description says \"{words}\" - make sure this price is for this unit")
    # what it states, quoted: "Asking price RM 3.1mil" - or, with no price in it, how it starts
    first = next((x.strip() for x in text.splitlines() if len(x.strip()) > 3), "")
    said = f"states \"{stated}\"" if stated else f"no price in its description - \"{first[:80]}\"" if first \
        else "no description"
    return {"fake": fake, "check": check, "said": said, "stated": bool(stated), "bumi": bumi,
            "headline": (d.get("headline") or "")[:120], "posted": d.get("first_posted"), "at": time.time(), "v": 3}


def verdict(seen, price, typical):
    """Real / check / fake for a listing from what its description showed - and how far below the typical price
    it is right now (worked out each time: the typical price changes, the description doesn't)."""
    fake, check = list(seen["fake"]), list(seen["check"])
    if typical and price < 0.7 * typical:
        check.append(f"{(1 - price / typical) * 100:.0f}% below the typical price - unusually cheap")
    # the quote of what it states, unless a note above already quotes it
    notes = fake + check + ([seen["said"]] if not fake and (seen["stated"] or not (seen["check"])) else [])
    return {"status": "fake" if fake else "check" if check else "real", "why": "; ".join(notes),
            "exclude": bool(fake) or seen["bumi"]}  # left out of the price comparison


def check_cheapest(fetch, units, typical, rent, cache):
    """Check the descriptions of these (the 5 cheapest) - each listing opened once a week at most."""
    for u in units:
        seen = cache.get(u.get("url") or "")
        if not (seen and seen.get("v") == 3 and time.time() - seen.get("at", 0) < 7 * 86400):
            try:
                seen = cache[u["url"]] = check_listing(fetch, u, rent)
                time.sleep(1.0)
            except Exception as e:
                u["check"] = {"status": "check", "why": f"couldn't open it ({str(e).splitlines()[0][:80]})"}
                continue
        u["check"] = verdict(seen, u["price"], typical)
    return units


RESIDENTIAL = {"Condo / Apartment", "Bungalow", "Semi-D", "Terrace", "Townhouse"}


def same_kind(listing_category, units):
    """Only PropertyGuru units of the same kind: a shop office at Plaza Damas isn't priced like its condos."""
    from wa_parser import property_category
    want_home = listing_category in RESIDENTIAL or listing_category == "Unknown"
    out = []
    for u in units:
        cat = property_category(u.get("type"), "")
        if cat == "Unknown":
            out.append(u)  # PropertyGuru didn't say - keep it
        elif want_home and cat in RESIDENTIAL:
            out.append(u)  # home with home
        elif not want_home and cat == listing_category:
            out.append(u)  # shop with shop, office with office
    return out


STUDIO = re.compile(r"\bstudio\b", re.I)


def rooms_wanted(listing):
    """The owner's rooms as PropertyGuru writes them (a studio is -1). Owners say "one room studio" for both, so a
    studio matches studio and 1-bedroom units. -> ({-1, 0, 1} / {2}, "studio" / "2-bedroom") or (None, None)."""
    if STUDIO.search(f"{listing.get('message_text') or ''} {listing.get('property_type') or ''}"):
        return {-1, 0, 1}, "studio / 1-bedroom"
    if listing.get("bedrooms"):
        return {int(listing["bedrooms"])}, f"{listing['bedrooms']}-bedroom"
    return None, None


def _beds(u):
    try:
        return int(u.get("beds"))
    except (TypeError, ValueError):
        return None


def similar_units(listing, units, sized):
    """Units like the owner's: the same size (within 20%) when the owner gave one, else - owner said only "studio"
    or "2 bedroom" - the same rooms. -> (units, words for them)."""
    if not sized:
        return [], ""
    sqft = listing.get("sqft")
    if sqft:
        return [u for u in units if u["sqft"] and abs(u["sqft"] - sqft) <= 0.2 * sqft], "similar-size units"
    want, word = rooms_wanted(listing)
    if want:
        return [u for u in units if _beds(u) in want], f"{word} units"
    return [], ""


def estimate(listing, units):
    """The owner left out the size or the rooms: what's usual for the other one there on PropertyGuru / iProperty.
    -> {'est_sqft' or 'est_beds', 'est_note'} or {}."""
    import collections
    want, word = rooms_wanted(listing)
    sqft = listing.get("sqft")
    if not sqft and want:
        sizes = [u["sqft"] for u in units if u["sqft"] and _beds(u) in want]
        if sizes:
            s = int(statistics.median(sizes))
            return {"est_sqft": s, "est_note": f"about {s:,} sf - the usual size of {word} units there online "
                                               f"({min(sizes):,}-{max(sizes):,} sf, {len(sizes)} listed)"}
    if sqft and not want:
        near = [_beds(u) for u in units if u["sqft"] and abs(u["sqft"] - sqft) <= 0.1 * sqft and _beds(u) is not None]
        if near:
            b = collections.Counter(near).most_common(1)[0][0]
            rooms = "a studio" if b <= 0 else f"{b} bedroom{'s' * (b > 1)}"
            return {"est_beds": b, "est_note": f"usually {rooms} at this size ({sqft:,} sf) there online"}
    return {}


def lowest5(listing, units):
    """The 5 cheapest PropertyGuru units of the same kind - same size (within 20%), or same rooms when the owner
    gave no size, when there are any. -> (units, True if they are like the owner's)."""
    from wa_parser import property_category
    category = property_category(listing.get("property_type"), listing.get("place"), listing.get("message_text"))
    pool = same_kind(category, units)
    sized = category in ("Condo / Apartment", "Unknown")  # shop / house sizes are stated differently (land vs floors)
    similar, _ = similar_units(listing, pool, sized)
    pick = similar or pool
    keep = ("price", "sqft", "psf", "beds", "url", "type", "site", "ip_url")
    return [{k: u.get(k) for k in keep} for u in sorted(pick, key=lambda u: u["price"])[:5]], bool(similar)


def lowest_real(res, kind):
    """Cheapest of the 5 that doesn't look fake (for the dashboard table)."""
    real = [u for u in res.get(f"{kind}_lowest") or [] if not (u.get("check") or {}).get("exclude")]
    return min((u["price"] for u in real), default=None)


def judge(listing, units, prop, left_out=0):
    """The verdict for one listing against PropertyGuru's current units -> dict for the dashboard.
    prop = {'name', 'urls': {'sale', 'rent'}, 'area': True if it's an area, not one building}; left_out = cheap
    listings dropped because their own description shows they're lelong / bait."""
    from wa_parser import property_category
    rent = listing.get("listing_type") == "Rent"
    price, sqft = listing.get("price_rm"), listing.get("sqft")
    category = property_category(listing.get("property_type"), listing.get("place"), listing.get("message_text"))
    all_units, units = units, same_kind(category, units)
    base = {"property": prop["name"] if prop else None, "property_url": None, "ip_url": None, "count": len(units),
            "checked_at": time.time(), "kind": "rent" if rent else "sale", "area": bool(prop and prop.get("area")),
            "sites": 2}
    if prop:
        base["property_url"], base["ip_url"] = prop["urls"]["pg"][base["kind"]], prop["urls"]["ip"][base["kind"]]
    if not prop:
        return {**base, "verdict": "none", "label": "❓ Not found online",
                "why": "Building / area not found on PropertyGuru or iProperty",
                "reason": f"Couldn't find \"{search_name(listing.get('place'))}\" on PropertyGuru or iProperty - "
                          "neither the building nor its area - so there's nothing to compare with."}
    if not price:
        return {**base, "verdict": "none", "label": "❓ No owner price", "why": "The owner didn't give a price",
                "reason": "The owner didn't give a price, so it can't be compared."}
    if not units:
        other = f" (only {len(all_units)} of a different kind, e.g. {all_units[0]['type']})" if all_units else ""
        what = f"{category.lower() if category != 'Unknown' else ''} {'rent' if rent else 'sale'}".strip()
        return {**base, "verdict": "none", "label": "❓ No listings now",
                "why": f"No {what} listings there now{other}",
                "reason": f"No {what} listings at {prop['name']} on PropertyGuru or iProperty right now{other} - "
                          "nothing to compare with (could also mean little competition)."}
    # Sizes only mean the same thing for condos / apartments: for shops and houses one side often gives the land
    # area and the other the built-up area of all floors - so those are compared on the total price
    sized = category in ("Condo / Apartment", "Unknown")
    similar, similar_word = similar_units(listing, units, sized)
    basis = "price"
    if not sized:
        pool, mine, sizes = [u["price"] for u in units], price, ""
        unit_word = "units (total price - shop / house sizes are stated differently)"
    elif len(similar) >= (3 if sqft else 2):  # same size (or same rooms): compare prices directly
        pool, mine, unit_word = [u["price"] for u in similar], price, similar_word
        sized_units = [u["sqft"] for u in similar if u["sqft"]]
        sizes = f" ({min(sized_units):,}-{max(sized_units):,} sf)" if sized_units else ""
    elif sqft and sum(1 for u in units if u["psf"]) >= 3:  # different sizes: compare RM per sq ft
        pool, mine, unit_word, sizes, basis = [u["psf"] for u in units if u["psf"]], price / sqft, "units", "", "psf"
    else:  # no size from the owner: plain prices (sizes may differ)
        pool, mine, unit_word, sizes = [u["price"] for u in units], price, "units (sizes may differ)", ""

    def fmt(x):
        return f"RM {x:,.2f} psf" if basis == "psf" else _rm(x, rent)

    low, typical = min(pool), statistics.median(pool)
    diff = (mine - typical) / typical * 100
    have = f"Owner asks {_rm(price, rent)}" + (f" for {sqft:,} sf (RM {price / sqft:,.2f} psf)" if sqft else "") + "."
    kind_word = category.lower() + " " if prop.get("area") and category != "Unknown" else ""
    market_txt = (f" On PropertyGuru & iProperty now: {len(pool)} {kind_word}{unit_word} {'in' if prop.get('area') else 'at'} "
                  f"{prop['name']}{sizes} for {'rent' if rent else 'sale'} - lowest {fmt(low)}, typical "
                  f"{fmt(typical)}.") + (f" ({left_out} cheaper listing{'s' * (left_out > 1)} left out - their own "
                                         "description shows lelong / bait / a Bumi-only price.)" if left_out else "")
    if mine <= low:
        v, label, why = "good", "✅ Good to post", f"Cheapest online ({fmt(mine)} vs lowest {fmt(low)})."
    elif diff <= 0:
        v, label, why = "good", "✅ Good to post", ("At the typical price - competitive." if diff > -1 else
                                                     f"{-diff:.0f}% below the typical price - competitive.")
    elif diff <= 15:
        v, label, why = "ok", "⚠️ A bit high", (f"{diff:.0f}% above the typical price - OK to post, but expect "
                                                 f"buyers / tenants to compare with the lowest at {fmt(low)}.")
    else:
        v, label, why = "high", "❌ Too high", (f"{diff:.0f}% above the typical price - hard to get response; "
                                               f"suggest the owner gets closer to {fmt(typical)}.")
    short = f"Owner {fmt(mine)} vs lowest {fmt(low)}, typical {fmt(typical)} ({len(pool)} listed) - " + why
    return {**base, "verdict": v, "label": label, "reason": have + market_txt + " " + why, "why": short,
            "lowest": low, "typical": typical, "owner": mine, "basis": basis,
            "cheapest_url": min(units, key=lambda u: u["price"])["url"]}


def run(db, ids=None, progress=print, rows=None, store=True):
    """Compare saved listings (all, or only `ids`) - opens Chrome on this PC. Returns {message_id: result}.
    rows: listings given directly instead of saved ones; store=False keeps the
    results out of the dashboard's saved comparison."""
    from patchright.sync_api import sync_playwright
    import scraper
    from wa_parser import property_category
    if rows is None:
        rows = db.table("owner_listings").select("message_id,place,address,listing_type,price_rm,sqft,"
                                                 "property_type,message_text,bedrooms").execute().data
        if ids:
            rows = [r for r in rows if r["message_id"] in set(ids)]
    results, cache = (load_results(db) if store else {}), {}
    try:
        checks = json.loads(CHECKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        checks = {}
    _save(STATUS, {"state": "running", "at": time.time(), "done": 0, "total": len(rows), "text": "Starting Chrome…"})
    with sync_playwright() as p:
        browser = scraper.Browser(p)
        try:
            for i, r in enumerate(rows, 1):
                _save(STATUS, {"state": "running", "at": time.time(), "done": i - 1, "total": len(rows),
                               "text": f"{r.get('place')}"})
                try:
                    if property_category(r.get("property_type"), r.get("place"), r.get("message_text")) == "Land":
                        results[r["message_id"]] = {"verdict": "none", "label": "❓ Land - not compared",
                                                    "reason": "Land is priced by location, size and zoning - there's no "
                                                              "fair like-for-like listing on PropertyGuru to compare.",
                                                    "checked_at": time.time()}
                        progress(f"{r.get('place')!r}: land - skipped")
                        continue
                    category = property_category(r.get("property_type"), r.get("place"), r.get("message_text"))
                    kind = "rent" if r.get("listing_type") == "Rent" else "sale"
                    # its building on PropertyGuru - or, for shops / houses with no named building, its area / town
                    # 1) its building (PropertyGuru search, then Google), 2) shops / houses: the same taman by
                    # free text, 3) its area / town
                    src, found = None, find_any(r)
                    if not found and category not in ("Shop / Shop office", "Factory / Warehouse"):
                        found = google_find(browser.page, r)  # misspelt / nickname - Google knows
                    # shops / offices / factories: the commercial section (also inside a building like Plaza Damas)
                    com = category not in RESIDENTIAL and category != "Unknown"

                    def place(name, where, area=False):
                        return {"name": name, "area": area, "where": where, "key": (where, com),
                                "urls": {site: {k: search_url(site, k, where, com) for k in ("sale", "rent")}
                                         for site in SITES}}

                    src = place(found["name"], f"propertyId={found['id']}") if found else None
                    both, failed = {"sale": [], "rent": []}, set()

                    def load(s):
                        for k in both:
                            got = []
                            for site in SITES:  # PropertyGuru and iProperty, merged (an ad on both counts once)
                                if (s["key"], k, site) not in cache:
                                    try:
                                        cache[(s["key"], k, site)] = market(browser.data, s["where"], k, com, site)
                                    except Exception as e:
                                        if site == "pg":
                                            raise
                                        cache[(s["key"], k, site)] = []  # iProperty down: PropertyGuru alone
                                        failed.add(SITES[site][0])
                                        progress(f"    {SITES[site][0]} didn't load: {str(e).splitlines()[0][:100]}")
                                    time.sleep(1.5)
                                got.append(cache[(s["key"], k, site)])
                            both[k] = merge(*got)
                        return any(both.values())

                    if src:  # both sale and rent - the dashboard shows the 5 cheapest of each
                        load(src)
                    else:
                        for area in (find_freetext(r), find_area(r)):
                            if not area:
                                continue
                            src = place(area["name"], area["query"], area=True)
                            if load(src):
                                break  # found listings there - no need to widen to the whole town
                    lists, fake_urls = {}, set()
                    for k in both:  # read the descriptions of the 5 cheapest: lelong / "starting from" = fake
                        units, same = lowest5(r, both[k])
                        typ = statistics.median([u["price"] for u in units]) if units else None
                        same_kind_all = same_kind(category, both[k])
                        sized = category in ("Condo / Apartment", "Unknown")  # shop / house sizes aren't like-for-like
                        sim = [u["price"] for u in similar_units(r, same_kind_all, sized)[0]] \
                            or [u["price"] for u in same_kind_all]
                        typ = statistics.median(sim) if sim else typ
                        lists[k] = (check_cheapest(browser.data, units, typ, k == "rent", checks), same)
                        fake_urls |= {u["url"] for u in units if (u.get("check") or {}).get("exclude")}
                    _save(CHECKS, checks)
                    real = [u for u in both[kind] if u.get("url") not in fake_urls]
                    res = judge(r, real if r.get("price_rm") else [], src,
                                left_out=len(both[kind]) - len(real))
                    for k, (units, same) in lists.items():
                        res[f"{k}_lowest"], res[f"{k}_same_size"] = units, same
                    res["owner_sqft"] = r.get("sqft")
                    # owner gave only rooms (or only a size): what's usual for the other one there
                    est = estimate(r, same_kind(category, both["sale"] + both["rent"])) \
                        if category in ("Condo / Apartment", "Unknown") else {}
                    if est:
                        res.update(est)
                        res["reason"] = (res.get("reason") or "") + f" Size / rooms not given: {est['est_note']}."
                    if failed:
                        res["reason"] = (res.get("reason") or "") + f" ({', '.join(failed)} didn't load this time.)"
                    # Same price as the owner's, same place = probably this very unit, already advertised (by an
                    # agent or the owner)
                    twins = [u for u in both[kind] if r.get("price_rm") and u["price"] == r["price_rm"]]
                    if twins:
                        res["same_unit_url"] = twins[0]["url"]
                        res["reason"] = (res.get("reason") or "") + (
                            f" 👀 Looks like this unit is already advertised online at the same price "
                            f"({_rm(r['price_rm'], kind == 'rent')}) - see \"Already listed?\" below.")
                    results[r["message_id"]] = res
                except Exception as e:
                    results[r["message_id"]] = {"verdict": "none", "label": "❓ Couldn't check",
                                                "reason": f"PropertyGuru didn't load ({str(e).splitlines()[0][:120]}).",
                                                "checked_at": time.time()}
                progress(f"{r.get('place')!r}: {results[r['message_id']]['label']}")
                if store:
                    db.storage.from_(BUCKET).upload(RESULTS, json.dumps(results, ensure_ascii=False).encode(),
                                                    {"content-type": "application/json", "upsert": "true"})
        finally:
            browser.close()
    _save(STATUS, {"state": "done", "at": time.time(), "done": len(rows), "total": len(rows)})
    return results


def request(db, ids):
    """Online dashboard: ask the PC to compare these listings."""
    db.table("pc_tasks").insert({"kind": "market_check", "params": {"ids": list(ids)}}).execute()


def run_requests(db, log=print):
    """PC side (keeper.py): start comparisons asked for on the online dashboard, one at a time."""
    import subprocess
    tasks = db.table("pc_tasks").select("id,params").eq("kind", "market_check").eq("status", "pending") \
        .order("id").execute().data
    if not tasks:
        return
    s = status()
    if s.get("state") == "running" and time.time() - s.get("at", 0) < 600:
        return
    ids = sorted({i for t in tasks for i in (t.get("params") or {}).get("ids") or []})
    start(ids)
    for t in tasks:
        db.table("pc_tasks").update({"status": "started"}).eq("id", t["id"]).execute()
    log(f"PropertyGuru comparison started from the online page: {len(ids)} listing(s)")


AUTO = ROOT / "data" / "market_auto.json"  # when the last automatic comparison started


def auto(db, log=print):
    """keeper.py, every few minutes: compare saved listings by themselves - new ones, and ones a scan changed since
    they were last compared. Never while a comparison runs; at most every 30 minutes (so a failing run doesn't
    keep opening Chrome)."""
    s = status()
    if s.get("state") == "running" and time.time() - s.get("at", 0) < 600:
        return
    try:
        last = json.loads(AUTO.read_text(encoding="utf-8")).get("at", 0)
    except (OSError, ValueError):
        last = 0
    if time.time() - last < 1800:
        return
    results = load_results(db)

    def changed(r):
        done = results.get(r["message_id"])
        if not done:
            return True
        try:
            return datetime.fromisoformat(r["scanned_at"]).timestamp() > done.get("checked_at", 0)
        except (TypeError, ValueError):
            return False

    rows = db.table("owner_listings").select("message_id,scanned_at").execute().data
    todo = [r["message_id"] for r in rows if changed(r)]
    if todo:
        _save(AUTO, {"at": time.time(), "ids": todo})
        start(todo)
        log(f"PropertyGuru & iProperty comparison started by itself: {len(todo)} new / changed listing(s)")


def start(ids=None):
    """Run the comparison in the background on this PC."""
    import subprocess
    import sys
    _save(STATUS, {"state": "running", "at": time.time(), "done": 0, "total": len(ids or []), "text": "Starting…"})
    (ROOT / "logs").mkdir(exist_ok=True)
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(python if python.exists() else sys.executable), "-u", str(ROOT / "market_check.py")]
                     + (["--ids", *ids] if ids else []), cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # ✅ / ❌ in the log, not a crash
                     stdout=open(ROOT / "logs" / "market_check.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(ROOT / ".env")
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    try:
        run(db, a.ids)
    except Exception as e:
        _save(STATUS, {"state": "error", "at": time.time(), "error": str(e)[:300]})
        raise
