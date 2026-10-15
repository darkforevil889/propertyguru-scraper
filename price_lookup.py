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


RENT_MIN, RENT_MAX = 5000, 90000  # only rentals in this band count towards the rent range
SALE_MIN = 5_000_000              # only sales at or above this count towards the sale range


def name_key(name):
    """'A-B  c' and 'a b C' -> 'a b c' so near-identical file names count once."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def listings_url(pid, kind, extra=""):
    band = f"&minPrice={RENT_MIN}&maxPrice={RENT_MAX}" if kind == "rent" else f"&minPrice={SALE_MIN}"
    return f"{BASE}/property-for-{kind}?listingType={kind}&propertyId={pid}{band}{extra}"


def price_range(fetch, pid, kind):
    """Lowest and highest current price: just the price-sorted first pages (2 page loads)."""
    prices = []
    for order in ("asc", "desc"):
        prices += [r["price"] for r in _rows(fetch(listings_url(pid, kind, f"&sort=price&order={order}")))]
    if kind == "rent":
        prices = [x for x in prices if x and RENT_MIN <= x <= RENT_MAX]
    else:
        prices = [x for x in prices if x and x >= SALE_MIN]
    v = _trim(prices)
    return (v[0], v[-1]) if v else (None, None)


def _rows(data):
    rows = []
    for item in data.get("listingsData") or []:
        L = item.get("listingData") or {}
        if L.get("id"):
            rows.append({"price": (L.get("price") or {}).get("value")})
    return rows


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


def fmt_range(lo, hi, prefix="", suffix=""):
    if lo is None:
        return ""
    f = lambda x: f"{prefix}{x:,.0f}{suffix}"
    return f(lo) if lo == hi else f"{f(lo)} – {f(hi)}"
