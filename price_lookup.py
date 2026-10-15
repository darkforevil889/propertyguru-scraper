"""Look up a property (condo / project) on PropertyGuru by name and summarise its current listings.

1. Name -> property via PropertyGuru's search-box suggestions (plain HTTP, no Cloudflare)
2. Property -> rent and sale listings (needs real Chrome: www.propertyguru.com.my is behind Cloudflare)
"""
import json
import re
import urllib.parse
import urllib.request

BASE = "https://www.propertyguru.com.my"
AUTOCOMPLETE = "https://autocomplete.propertyguru.com/v2/search"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def clean_name(file_stem):
    """'Pavilion_Damansara-Heights (2)' -> 'Pavilion Damansara Heights'."""
    s = re.sub(r"\(\d+\)|\[\d+\]", " ", file_stem)       # copy numbers like (2)
    s = re.sub(r"[_\-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_property(name, limit=5):
    """Return PropertyGuru's matching properties (best first) for a name."""
    qs = urllib.parse.urlencode({"marketplace": "pgmy", "locale": "en", "limit": limit, "query": name,
                                 "objectType[0]": "PROPERTY"})
    req = urllib.request.Request(f"{AUTOCOMPLETE}?{qs}", headers={
        "User-Agent": UA, "Origin": BASE, "Referer": BASE + "/", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        items = json.load(r)
    out = []
    for it in items:
        if it.get("objectType") != "property":
            continue
        name_en = (it.get("displayText") or {}).get("en") or ""
        addr = (it.get("displayDescription") or {}).get("en") or ""
        out.append({"id": it["objectId"], "name": name_en, "address": addr, "type": it.get("displayType"),
                    "map": "https://www.google.com/maps/search/?api=1&query="
                           + urllib.parse.quote_plus(f"{name_en}, {addr}")})
    return out


def listings_url(pid, kind, extra=""):
    return f"{BASE}/property-for-{kind}?listingType={kind}&propertyId={pid}{extra}"


def _rows(data):
    rows = []
    for item in data.get("listingsData") or []:
        L = item.get("listingData") or {}
        if L.get("id"):
            rows.append({"price": (L.get("price") or {}).get("value"), "sqft": L.get("floorArea"),
                         "beds": L.get("bedrooms")})
    return rows


def _count(data, rows):
    heading = ((data.get("metaData") or {}).get("metaTags") or {}).get("heading") or ""
    m = re.match(r"\s*([\d,]+)", heading)
    if m:
        return int(m.group(1).replace(",", ""))
    pages = (data.get("paginationData") or {}).get("totalPages") or 0
    return len(rows) if pages <= 1 else None


def _trim(values, low_ratio=0.4, high_ratio=2.5):
    """Drop obvious typos at either end (e.g. a RM 20,000 'sale' among RM 700k units)."""
    v = sorted(x for x in values if x)
    for _ in range(3):
        if len(v) >= 4 and v[0] < low_ratio * v[2]:
            v = v[1:]
    for _ in range(3):
        if len(v) >= 4 and v[-1] > high_ratio * v[-3]:
            v = v[:-1]
    return v


def listing_stats(fetch, pid, kind):
    """fetch(url) -> page data. Small projects: read every page. Big ones: read the sorted extremes."""
    first = fetch(listings_url(pid, kind))
    rows = _rows(first)
    count = _count(first, rows)
    pages = (first.get("paginationData") or {}).get("totalPages") or 0
    if not rows:
        return {"count": 0}
    if pages <= 3:
        for n in range(2, pages + 1):
            rows += _rows(fetch(listings_url(pid, kind, f"&page={n}")))
    else:
        for sort in ("&sort=price&order=asc", "&sort=price&order=desc",
                     "&sort=size&order=asc", "&sort=size&order=desc"):
            rows += _rows(fetch(listings_url(pid, kind, sort)))
    prices = _trim([r["price"] for r in rows])
    sizes = _trim([r["sqft"] for r in rows], 0.3, 3)
    beds = sorted({r["beds"] for r in rows if r["beds"] is not None})
    return {"count": count,
            "price_min": prices[0] if prices else None, "price_max": prices[-1] if prices else None,
            "sqft_min": sizes[0] if sizes else None, "sqft_max": sizes[-1] if sizes else None,
            "beds": beds, "exact": pages <= 3}


def fmt_range(lo, hi, prefix="", suffix=""):
    if lo is None:
        return ""
    f = lambda x: f"{prefix}{x:,.0f}{suffix}"
    return f(lo) if lo == hi else f"{f(lo)} – {f(hi)}"


def fmt_beds(beds):
    b = sorted({max(x, 0) for x in beds})  # -1 / 0 = studio
    if not b:
        return ""
    label = lambda x: "Studio" if x == 0 else str(x)
    return label(b[0]) if len(b) == 1 else f"{label(b[0])} – {label(b[-1])}"
