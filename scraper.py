"""PropertyGuru Malaysia rental scraper -> Supabase.

Usage:
    python scraper.py                      # all search pages, details for NEW listings only
    python scraper.py --pages 3            # first 3 search pages (20 listings each)
    python scraper.py --max-details 50     # open at most 50 listing pages this run
    python scraper.py --no-details         # search pages only (fast)
    python scraper.py --csv                # also save a CSV copy in data/
"""
import argparse
import csv
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright
from supabase import create_client

# Add more searches here later (e.g. Selangor). Key = short name stored with each listing.
SEARCHES = {
    "kl-rent-8k": ("https://www.propertyguru.com.my/property-for-rent?listingType=rent&page={page}"
                   "&isCommercial=false&regionCode=58jok&_freetextDisplay=Kuala+Lumpur&minPrice=8000"),
}
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TRACKED = "listing_id,price_rm,initial_price_rm,previous_price_rm,price_changed_at,first_seen,details_scraped_at"


def polite_pause(lo=3.0, hi=8.0):
    time.sleep(random.uniform(lo, hi))


def load_next_data(page, url):
    """Navigate and return the page's __NEXT_DATA__ JSON, waiting out any Cloudflare check."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = resp.status if resp else None
    # Up to ~2 minutes: if a challenge appears you can solve it in the open Chrome window
    for _ in range(120):
        if page.locator("#__NEXT_DATA__").count():
            return json.loads(page.locator("#__NEXT_DATA__").inner_text())["props"]["pageProps"]["pageData"]["data"]
        page.wait_for_timeout(1000)
    raise RuntimeError(f"No data on {url} (HTTP {status}) - blocked or page layout changed")


class Browser:
    """Real Chrome that restarts itself if it crashes or its window gets closed."""

    def __init__(self, p):
        self.p, self.ctx, self.page = p, None, None
        self.start()

    def start(self):
        self.close()
        self.ctx = self.p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / ".chrome-profile"),
            channel="chrome",      # your real installed Chrome = far less likely to be blocked
            headless=False,        # headless is detected much more often by Cloudflare
            no_viewport=True,
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def screenshot(self):
        """Save what the browser shows (e.g. a Cloudflare block) to debug/ for troubleshooting."""
        try:
            (ROOT / "debug").mkdir(exist_ok=True)
            self.page.screenshot(path=str(ROOT / "debug" / f"failed_{datetime.now():%Y%m%d_%H%M%S}.png"))
        except Exception:
            pass

    def close(self):
        if self.ctx:
            try:
                self.ctx.close()
            except Exception:
                pass
            self.ctx = None

    def data(self, url, attempts=3):
        for attempt in range(1, attempts + 1):
            try:
                return load_next_data(self.page, url)
            except Exception as e:
                print(f"    attempt {attempt}/{attempts} failed: {str(e).splitlines()[0]}")
                if attempt == attempts:
                    self.screenshot()
                    raise
                time.sleep(15 * attempt)
                self.start()  # fresh browser recovers from crashes / closed windows


def parse_card(item):
    L = item.get("listingData") or {}
    if not L.get("id"):
        return None
    add = L.get("additionalData") or {}
    price = L.get("price") or {}
    prop = L.get("property") or {}
    agent = L.get("agent") or {}
    mrt = L.get("mrt") or {}
    psf = re.search(r"[\d.,]+", (L.get("pricePerArea") or {}).get("localeStringValue", "") or "")
    build_year = next((f.get("text", "").replace("Built: ", "") for f in L.get("listingFeatures", [])
                       if isinstance(f, dict) and f.get("dataAutomationId") == "listing-card-v2-build-year"), "")
    return {
        "listing_id": L["id"],
        "title": L.get("localizedTitle"),
        "price_rm": price.get("value"),
        "psf_rm": float(psf.group().replace(",", "")) if psf else None,
        "floor_area_sqft": L.get("floorArea"),
        "bedrooms": L.get("bedrooms"),
        "bathrooms": L.get("bathrooms"),
        "property_type": prop.get("subTypeText"),
        "tenure": {"F": "Freehold", "L": "Leasehold"}.get(add.get("tenure"), add.get("tenure")),
        "build_year": build_year,
        "address": L.get("fullAddress"),
        "area": add.get("areaText"),
        "district": add.get("districtText"),
        "region": add.get("regionText"),
        "nearest_station": mrt.get("nearbyText"),
        "posted_on": (L.get("postedOn") or {}).get("text"),
        "status": L.get("statusCode"),
        "agent_name": agent.get("name"),
        "agent_license": agent.get("license"),
        "agency_id": agent.get("agencyId"),
        "photo_count": next((int(m["text"]) for m in L.get("mediaItems", [])
                             if m.get("mediaType") == "images" and str(m.get("text", "")).isdigit()), None),
        "thumbnail": L.get("thumbnail"),
        "url": L.get("url"),
    }


def parse_detail(d):
    D = d.get("listingDetail") or {}
    loc = D.get("location") or {}
    point = loc.get("point") or {}
    unit = D.get("unitDetails") or {}
    dates = D.get("dates") or {}
    details = [i.get("value") for i in ((d.get("detailsData") or {}).get("metatable") or {}).get("items", [])]
    completed = next((re.search(r"\d{4}", v).group() for v in details if v and v.startswith("Completed in")), "")
    title_type = next((v for v in details if v and v.endswith(" title")), "")
    desc = (d.get("descriptionBlockData") or {}).get("description") or ""
    desc = re.sub(r"<br\s*/?>", "\n", desc)
    desc = re.sub(r"<[^>]+>", "", desc).strip()
    return {
        "latitude": point.get("lat"),
        "longitude": point.get("lon"),
        "postcode": (loc.get("address") or {}).get("postalCode"),
        "furnishing": (unit.get("furnishing") or {}).get("description"),
        "furnishing_items": "; ".join(f.get("description", "") for f in unit.get("furnishingDetails") or []),
        "tenancy": (unit.get("tenancy") or {}).get("value"),
        "facilities": "; ".join(f.get("text", "") for f in (d.get("facilitiesData") or {}).get("data", [])),
        "amenities": "; ".join(f.get("text", "") for f in (d.get("amenitiesData") or {}).get("data", [])),
        "developer": (d.get("listingData") or {}).get("developer"),
        "completed_year": completed,
        "title_type": title_type,
        "headline": (d.get("descriptionBlockData") or {}).get("subtitle"),
        "description": desc,
        "first_posted": (dates.get("firstPosted") or {}).get("date"),
        "last_updated": (dates.get("updated") or {}).get("date"),
        "expires": (dates.get("expiry") or {}).get("date"),
    }


def save_page(db, rows, search_key):
    """Upsert one search page of listings; record price history for new/changed prices."""
    now = datetime.now(timezone.utc).isoformat()
    ids = [r["listing_id"] for r in rows]
    existing = {e["listing_id"]: e for e in
                db.table("listings").select(TRACKED).in_("listing_id", ids).execute().data}
    history, need_details = [], []
    for r in rows:
        old = existing.get(r["listing_id"])
        r["search_key"] = search_key
        r["last_seen"] = now
        if old is None:
            r.update(first_seen=now, initial_price_rm=r["price_rm"],
                     previous_price_rm=None, price_changed_at=None)
            history.append({"listing_id": r["listing_id"], "price_rm": r["price_rm"], "seen_at": now})
        else:
            r.update(first_seen=old["first_seen"], initial_price_rm=old["initial_price_rm"],
                     previous_price_rm=old["previous_price_rm"], price_changed_at=old["price_changed_at"])
            if r["price_rm"] is not None and r["price_rm"] != old["price_rm"]:
                r.update(previous_price_rm=old["price_rm"], price_changed_at=now)
                history.append({"listing_id": r["listing_id"], "price_rm": r["price_rm"], "seen_at": now})
        if old is None or not old["details_scraped_at"]:
            need_details.append(r)
    db.table("listings").upsert(rows, on_conflict="listing_id").execute()
    if history:
        db.table("price_history").insert(history).execute()
    return need_details, sum(1 for r in rows if r["listing_id"] not in existing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=0, help="max search pages per search (0 = all)")
    ap.add_argument("--no-details", action="store_true", help="skip opening individual listing pages")
    ap.add_argument("--max-details", type=int, default=0, help="max listing pages to open this run (0 = no limit)")
    ap.add_argument("--csv", action="store_true", help="also save a CSV copy of this run in data/")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    secret = os.environ.get("SUPABASE_SECRET_KEY")
    if not secret:
        raise SystemExit("SUPABASE_SECRET_KEY is empty in .env - paste your Supabase secret key there first.")
    db = create_client(os.environ["SUPABASE_URL"], secret)

    listings, new_count, details_done, details_failed, pages_failed = {}, 0, 0, 0, 0
    with sync_playwright() as p:
        browser = Browser(p)
        for search_key, search_url in SEARCHES.items():
            total_pages, n = None, 1
            while (not args.pages or n <= args.pages) and (total_pages is None or n <= total_pages):
                try:
                    data = browser.data(search_url.format(page=n))
                except Exception as e:
                    pages_failed += 1
                    print(f"[{search_key}] page {n} SKIPPED: {str(e).splitlines()[0]}")
                    if total_pages is None:  # can't even load page 1 - give up on this search
                        break
                    n += 1
                    continue
                total_pages = (data.get("paginationData") or {}).get("totalPages") or 1
                rows = {r["listing_id"]: r for r in map(parse_card, data.get("listingsData") or []) if r}
                rows = [r for lid, r in rows.items() if lid not in listings]  # skip repeats across pages
                queued = []
                if rows:
                    queued, added = save_page(db, rows, search_key)
                    new_count += added
                    listings.update((r["listing_id"], r) for r in rows)
                print(f"[{search_key}] page {n}/{total_pages}: {len(listings)} listings, {new_count} new")
                polite_pause()

                # Open this page's new listings right away, so a run that stops early still has full data
                for row in queued:
                    if args.no_details or (args.max_details and details_done >= args.max_details):
                        break
                    try:
                        detail = parse_detail(browser.data(row["url"]))
                        detail["details_scraped_at"] = datetime.now(timezone.utc).isoformat()
                        db.table("listings").update(detail).eq("listing_id", row["listing_id"]).execute()
                        row.update(detail)
                        details_done += 1
                        print(f"    detail: {row['title']} ({row.get('furnishing') or 'furnishing n/a'})")
                    except Exception as e:
                        details_failed += 1
                        print(f"    detail FAILED {row['url']}: {str(e).splitlines()[0]}")
                    polite_pause()
                n += 1
        browser.close()

    print(f"Done: {len(listings)} listings seen, {new_count} new, {details_done} details saved, "
          f"{details_failed} details failed, {pages_failed} search pages skipped.")
    rows = list(listings.values())
    if not (args.csv and rows):
        return
    DATA_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fields = list(dict.fromkeys(k for r in rows for k in r))
    csv_path = DATA_DIR / f"listings_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel opens it cleanly
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (DATA_DIR / f"listings_{stamp}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(rows)} listings -> {csv_path}")


if __name__ == "__main__":
    main()
