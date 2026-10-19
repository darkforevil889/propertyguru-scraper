"""One row per condo for whole areas (KLCC, Mont Kiara ...): lowest / highest sale price, lowest / highest rent,
smallest / largest size, price per sq ft - worked out from every current sale and rent listing in the area.

Run on this PC (PropertyGuru blocks cloud servers); a full KLCC + Mont Kiara update takes about an hour:
    python condo_summary.py                  # all areas in AREAS
    python condo_summary.py "Mont Kiara"     # one area
Results: data/condo_summary_<date>.csv and online (Supabase storage owner-photos/data/condo_summary.json),
shown on the dashboard's "Condo prices" page.
"""
import csv
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

import scraper

ROOT = Path(__file__).parent
BASE = "https://www.propertyguru.com.my"
# Area name -> PropertyGuru search filter. Add more areas here later (codes from PropertyGuru's search box).
AREAS = {
    "KLCC": "districtCode=kpycl&areaCode=jgrve",
    "Mont Kiara": "districtCode=q9qzw",
}
BUCKET, ONLINE_PATH = "owner-photos", "data/condo_summary.json"
STATUS_PATH = ROOT / "data" / "condo_summary_status.json"
# Typos: a sale price posted as rent (RM 1.9M "rent"), a rent posted as sale (RM 4,500 "sale")
RENT_RANGE, SALE_RANGE = (300, 150_000), (50_000, 300_000_000)


def pause():
    time.sleep(random.uniform(1.0, 2.0))


def trim(values, low_ratio=0.4, high_ratio=2.5):
    """Sorted values without obvious typos at either end (e.g. one RM 20,000 'sale' among RM 700k units)."""
    v = sorted(x for x in values if x)
    for _ in range(3):
        if len(v) >= 4 and v[0] < low_ratio * v[2]:
            v = v[1:]
    for _ in range(3):
        if len(v) >= 4 and v[-1] > high_ratio * v[-3]:
            v = v[:-1]
    return v


def status(**kw):
    STATUS_PATH.parent.mkdir(exist_ok=True)
    STATUS_PATH.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **kw}), encoding="utf-8")


def collect(fetch, area, kind):
    """Every listing (price, size, beds ...) for one area + sale/rent."""
    url = f"{BASE}/property-for-{kind}?isCommercial=false&listingType={kind}&{AREAS[area]}&page={{n}}"
    rows, n, total, blocked = [], 1, None, 0
    while total is None or n <= total:
        try:
            d = fetch(url.format(n=n))
            blocked = 0
        except Exception as e:
            blocked += 1
            print(f"  [{area} {kind}] page {n} skipped: {str(e).splitlines()[0]}", flush=True)
            if total is None or blocked >= 3:
                break
            n += 1
            continue
        total = (d.get("paginationData") or {}).get("totalPages") or 1
        for x in d.get("listingsData") or []:
            L = x.get("listingData") or {}
            prop = L.get("property") or {}
            if not L.get("id") or not prop.get("id"):
                continue  # landed houses etc. without a project
            rows.append({"pid": prop["id"], "title": L.get("localizedTitle"), "kind": kind,
                         "price": (L.get("price") or {}).get("value"), "sqft": L.get("floorArea"),
                         "beds": L.get("bedrooms"), "type": prop.get("subTypeText"),
                         "tenure": {"F": "Freehold", "L": "Leasehold"}.get((L.get("additionalData") or {}).get("tenure")),
                         "area_text": (L.get("additionalData") or {}).get("areaText")})
        if n % 10 == 0 or n == total:
            print(f"  [{area} {kind}] page {n}/{total} - {len(rows)} listings", flush=True)
        status(state="running", area=area, kind=kind, page=n, pages=total)
        n += 1
    return rows


def summarise(area, rows):
    by_pid = {}
    for r in rows:
        by_pid.setdefault(r["pid"], []).append(r)
    out = []
    for pid, rs in by_pid.items():
        sale = trim([r["price"] for r in rs if r["kind"] == "sale" and SALE_RANGE[0] <= (r["price"] or 0) <= SALE_RANGE[1]])
        rent = trim([r["price"] for r in rs if r["kind"] == "rent" and RENT_RANGE[0] <= (r["price"] or 0) <= RENT_RANGE[1]])
        sizes = trim([r["sqft"] for r in rs if r["sqft"] and 150 <= r["sqft"] <= 50_000], 0.3, 3)
        psf = trim([r["price"] / r["sqft"] for r in rs if r["kind"] == "sale" and r["sqft"]
                    and SALE_RANGE[0] <= (r["price"] or 0) <= SALE_RANGE[1] and 150 <= r["sqft"] <= 50_000])
        beds = sorted({max(r["beds"], 0) for r in rs if r["beds"] is not None})
        common = lambda k: statistics.mode([r[k] for r in rs if r[k]]) if any(r[k] for r in rs) else None
        out.append({
            "area": area, "condo": common("title"), "property_type": common("type"), "tenure": common("tenure"),
            "sale_listings": sum(r["kind"] == "sale" for r in rs),
            "sale_min": sale[0] if sale else None, "sale_max": sale[-1] if sale else None,
            "rent_listings": sum(r["kind"] == "rent" for r in rs),
            "rent_min": rent[0] if rent else None, "rent_max": rent[-1] if rent else None,
            "sqft_min": sizes[0] if sizes else None, "sqft_max": sizes[-1] if sizes else None,
            "psf_min": round(psf[0]) if psf else None, "psf_max": round(psf[-1]) if psf else None,
            "bedrooms": (f"{beds[0] or 'Studio'}" if len(beds) == 1 else f"{beds[0] or 'Studio'} - {beds[-1]}") if beds else None,
            "sale_url": f"{BASE}/property-for-sale?listingType=sale&propertyId={pid}",
            "rent_url": f"{BASE}/property-for-rent?listingType=rent&propertyId={pid}",
            "project_id": pid,
        })
    return sorted(out, key=lambda r: (r["condo"] or "").lower())


def run(areas):
    results, started = [], datetime.now(timezone.utc)
    with sync_playwright() as p:
        browser = scraper.Browser(p)

        def fetch(url):
            data = browser.data(url)
            pause()
            return data

        for area in areas:
            rows = collect(fetch, area, "sale") + collect(fetch, area, "rent")
            condos = summarise(area, rows)
            print(f"[{area}] {len(rows)} listings -> {len(condos)} condos", flush=True)
            results += condos
        browser.close()
    return results, started


def save(rows, started):
    (ROOT / "data").mkdir(exist_ok=True)
    out = ROOT / "data" / f"condo_summary_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("saved", out, flush=True)
    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_SECRET_KEY"):
        return
    from supabase import create_client
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    try:  # keep areas that weren't refreshed this time
        old = json.loads(db.storage.from_(BUCKET).download(ONLINE_PATH))
    except Exception:
        old = {"rows": [], "updated": {}}
    areas = {r["area"] for r in rows}
    updated = {k: v for k, v in (old.get("updated") or {}).items() if isinstance(old.get("updated"), dict)}
    updated.update({a: started.isoformat() for a in areas})
    merged = [r for r in old.get("rows", []) if r["area"] not in areas] + rows
    payload = json.dumps({"updated": updated, "rows": merged}, ensure_ascii=False).encode()
    db.storage.from_(BUCKET).upload(ONLINE_PATH, payload, {"content-type": "application/json", "upsert": "true"})
    print(f"saved online: {len(merged)} condos ({', '.join(sorted({r['area'] for r in merged}))})", flush=True)


if __name__ == "__main__":
    chosen = sys.argv[1:] or list(AREAS)
    unknown = [a for a in chosen if a not in AREAS]
    if unknown:
        raise SystemExit(f"Unknown area(s) {unknown}. Known: {list(AREAS)}")
    status(state="running", area=chosen[0], kind="sale", page=0, pages=None)
    try:
        result, start = run(chosen)
        if result:
            save(result, start)
        status(state="done", condos=len(result), areas=chosen)
    except Exception as e:
        status(state="failed", error=str(e).splitlines()[0])
        raise
