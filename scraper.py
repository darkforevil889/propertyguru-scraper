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
PG = "https://www.propertyguru.com.my"
SEARCHES = {
    "kl-rent-8k": (f"{PG}/property-for-rent?listingType=rent&page={{page}}"
                   "&isCommercial=false&regionCode=58jok&_freetextDisplay=Kuala+Lumpur&minPrice=8000"),
    # KL shop lots / offices / warehouses / factories for rent from RM 8k a month
    "kl-commercial-rent-8k": (f"{PG}/commercial-properties-for-rent?listingType=rent&isCommercial=true"
                              "&regionCode=58jok&minPrice=8000&page={page}"),
    # KL homes and commercial for sale from RM 3 million
    "kl-sale-3m": f"{PG}/property-for-sale?listingType=sale&isCommercial=false&regionCode=58jok&minPrice=3000000&page={{page}}",
    "kl-commercial-sale-3m": (f"{PG}/commercial-properties-for-sale?listingType=sale&isCommercial=true"
                              "&regionCode=58jok&minPrice=3000000&page={page}"),
    # Selangor equivalents - add when wanted (big: ~1,800 more pages):
    #   regionCode=45nk1 with the same paths as above
}
# Searches whose listings also get their own page opened for full details (slow - one page per listing).
# The big new searches only use the search result pages (price, size, type, location, tenure, agent, link).
DETAIL_SEARCHES = {"kl-rent-8k"}
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TRACKED = "listing_id,price_rm,initial_price_rm,previous_price_rm,price_changed_at,first_seen,details_scraped_at"


def polite_pause(lo=3.0, hi=8.0):
    time.sleep(random.uniform(lo, hi))


CHALLENGE_WAIT = 120  # seconds; set by --challenge-wait


def load_next_data(page, url):
    """Navigate and return the page's __NEXT_DATA__ JSON, waiting out any Cloudflare check."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = resp.status if resp else None
    # If a challenge appears on your PC you can solve it in the open Chrome window meanwhile
    for _ in range(CHALLENGE_WAIT):
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


def _whole(v):
    try:
        return int(round(float(v))) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
        "price_rm": _whole(price.get("value")),  # the database stores whole numbers (commercial can be 27892.8)
        "psf_rm": float(psf.group().replace(",", "")) if psf else None,
        "floor_area_sqft": _whole(L.get("floorArea")),
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
    ap.add_argument("--challenge-wait", type=int, default=120,
                    help="seconds to wait on a Cloudflare check before giving up on a page")
    ap.add_argument("--only", default="", help=f"comma-separated search keys to run (default all: {','.join(SEARCHES)})")
    args = ap.parse_args()
    chosen = [s.strip() for s in args.only.split(",") if s.strip()] or list(SEARCHES)
    if unknown := [s for s in chosen if s not in SEARCHES]:
        raise SystemExit(f"Unknown search(es) {unknown}. Known: {list(SEARCHES)}")
    global CHALLENGE_WAIT
    CHALLENGE_WAIT = args.challenge_wait

    load_dotenv(ROOT / ".env")
    secret = os.environ.get("SUPABASE_SECRET_KEY")
    if not secret:
        raise SystemExit("SUPABASE_SECRET_KEY is empty in .env - paste your Supabase secret key there first.")
    db = create_client(os.environ["SUPABASE_URL"], secret)

    listings, new_count, details_done, details_failed, pages_failed = {}, 0, 0, 0, 0
    blocked_streak = 0
    stopped_early = False
    try:  # keep Windows awake while scraping (a sleeping PC pauses the run for hours)
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass
    with sync_playwright() as p:
        browser = Browser(p)
        for search_key in chosen:
            search_url = SEARCHES[search_key]
            total_pages, n, page_blocked_streak = None, 1, 0
            while (not args.pages or n <= args.pages) and (total_pages is None or n <= total_pages):
                try:
                    data = browser.data(search_url.format(page=n))
                except Exception as e:
                    pages_failed += 1
                    print(f"[{search_key}] page {n} SKIPPED: {str(e).splitlines()[0]}")
                    page_blocked_streak += 1
                    if total_pages is None or page_blocked_streak >= 3:
                        # Blocked repeatedly: stop instead of burning hours on pages that won't load
                        print(f"[{search_key}] {page_blocked_streak} search pages blocked in a row - stopping early")
                        stopped_early = True
                        break
                    n += 1
                    continue
                page_blocked_streak = 0
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
                for row in (queued if search_key in DETAIL_SEARCHES else []):
                    if args.no_details or (args.max_details and details_done >= args.max_details):
                        break
                    if blocked_streak >= 3:  # listing pages keep getting blocked - don't waste hours retrying
                        if not args.no_details:
                            print("    3 listing pages blocked in a row - skipping listing pages for the rest of this run")
                        args.no_details = True
                        break
                    try:
                        detail = parse_detail(browser.data(row["url"]))
                        detail["details_scraped_at"] = datetime.now(timezone.utc).isoformat()
                        db.table("listings").update(detail).eq("listing_id", row["listing_id"]).execute()
                        row.update(detail)
                        details_done += 1
                        blocked_streak = 0
                        print(f"    detail: {row['title']} ({row.get('furnishing') or 'furnishing n/a'})")
                    except Exception as e:
                        details_failed += 1
                        blocked_streak += 1
                        print(f"    detail FAILED {row['url']}: {str(e).splitlines()[0]}")
                    polite_pause()
                n += 1
        browser.close()

    print(f"Done: {len(listings)} listings seen, {new_count} new, {details_done} details saved, "
          f"{details_failed} details failed, {pages_failed} search pages skipped"
          f"{' (stopped early: blocked by Cloudflare)' if stopped_early else ''}.")
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
