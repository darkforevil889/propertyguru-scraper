"""Streamlit dashboard for scraped listings. Run: streamlit run app.py"""
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent / ".env")
SUPABASE_URL = os.environ.get("SUPABASE_URL") or st.secrets["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or st.secrets["SUPABASE_PUBLISHABLE_KEY"]

st.set_page_config(page_title="KL Tracker", page_icon="🏙️", layout="wide")


# ---------- login ----------
def login_screen():
    st.title("KL Tracker")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary"):
            client = create_client(SUPABASE_URL, SUPABASE_KEY)
            try:
                client.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.client = client
                st.session_state.email = email
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")


if "client" not in st.session_state:
    login_screen()
    st.stop()

db = st.session_state.client

with st.sidebar:
    st.caption(f"Logged in as {st.session_state.email}")
    if st.button("Log out"):
        db.auth.sign_out()
        del st.session_state["client"]
        st.rerun()
    tool = st.radio("Page", ["Listings", "Phone filter", "Price filter"])


# ---------- phone filter ----------
def phone_filter_page():
    from phone_filter import filter_file

    st.title("Phone filter")
    st.caption("Upload one or more Excel or CSV files. Every cell on every sheet is scanned; only Malaysian mobile "
               "numbers are kept, cleaned to 0XXXXXXXXX, with duplicates removed. Nothing is saved.")
    uploads = st.file_uploader("Excel / CSV files", type=["xlsx", "xls", "csv"], accept_multiple_files=True)
    if not uploads:
        with st.expander("What gets kept and removed"):
            st.markdown(
                "- **Kept:** mobile numbers 010, 011, 012, 013, 014, 016, 017, 018, 019\n"
                "- **Fixed:** `60123456789` / `+60 12-345 6789` → `0123456789`; "
                "`123456789` (Excel dropped the 0) → `0123456789`\n"
                "- **Removed:** landlines (`03 3783737`, `33838383`), 015 internet/VoIP numbers, "
                "foreign numbers (`+65…`, `+1…`), incomplete numbers, duplicates")
        return
    # Process each file on its own (duplicates removed within each file)
    results, used_names = [], set()
    for up in uploads:
        try:
            numbers, rejected, dupes = filter_file(up.name, up.getvalue())
        except Exception as e:
            st.error(f"Couldn't read {up.name}: {e}")
            continue
        txt_name, i = Path(up.name).stem + ".txt", 2
        while txt_name.lower() in used_names:  # two uploads with the same name
            txt_name, i = f"{Path(up.name).stem} ({i}).txt", i + 1
        used_names.add(txt_name.lower())
        results.append({"file": up.name, "txt": txt_name, "numbers": numbers, "rejected": rejected, "dupes": dupes})
    if not results:
        return

    # Combined list: every file merged, duplicates across files removed too
    combined, seen, cross_dupes = [], set(), 0
    for r in results:
        for n in r["numbers"]:
            if n in seen:
                cross_dupes += 1
            else:
                seen.add(n)
                combined.append((n, r["file"]))
    to_txt = lambda nums: "\r\n".join(nums).encode("utf-8")

    mode = "Separate"
    if len(results) > 1:
        mode = st.radio("Download as", ["One combined file", "Separate file for each upload"], horizontal=True)
        mode = "Combined" if mode.startswith("One") else "Separate"

    c1, c2, c3 = st.columns(3)
    if mode == "Combined":
        c1.metric("Mobile numbers kept", f"{len(combined):,}")
        c2.metric("Duplicates removed", f"{sum(r['dupes'] for r in results) + cross_dupes:,}",
                  help=f"Includes {cross_dupes:,} numbers that appeared in more than one file")
    else:
        c1.metric("Mobile numbers kept", f"{sum(len(r['numbers']) for r in results):,}")
        c2.metric("Duplicates removed", f"{sum(r['dupes'] for r in results):,}",
                  help="Duplicates are removed within each file; a number can appear in more than one file")
    c3.metric("Other numbers removed", f"{sum(len(r['rejected']) for r in results):,}")

    if mode == "Combined":
        name = st.text_input("Combined file name", f"Combined {datetime.now():%Y-%m-%d}").strip() or "Combined"
        name = name if name.lower().endswith(".txt") else name + ".txt"
        st.download_button(f"Download {name} ({len(combined):,} numbers)", to_txt(n for n, _ in combined), name,
                           "text/plain", type="primary", disabled=not combined, on_click="ignore")
    else:
        for r in results:
            st.download_button(f"Download {r['txt']} ({len(r['numbers']):,} numbers)", to_txt(r["numbers"]),
                               r["txt"], "text/plain", key=f"dl-{r['txt']}", type="primary",
                               disabled=not r["numbers"], on_click="ignore")
        if len(results) > 1:
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for r in results:
                    z.writestr(r["txt"], to_txt(r["numbers"]))
            st.download_button("Download all as one ZIP", buf.getvalue(), f"Phone numbers {datetime.now():%Y-%m-%d}.zip",
                               "application/zip", on_click="ignore")

    left, right = st.columns(2)
    with left:
        st.subheader("Kept")
        kept = (pd.DataFrame(combined, columns=["Number", "From file"]) if mode == "Combined" else
                pd.DataFrame([(n, r["file"]) for r in results for n in r["numbers"]], columns=["Number", "From file"]))
        st.dataframe(kept, hide_index=True, height=400)
    with right:
        st.subheader("Removed")
        st.dataframe(pd.DataFrame([(raw, why, r["file"]) for r in results for raw, why in r["rejected"]],
                                  columns=["Found in file", "Reason", "From file"]), hide_index=True, height=400)


# ---------- price filter ----------
def price_filter_page():
    import random
    import time

    import price_lookup as pl

    st.title("Price filter")
    st.caption("Upload Excel / CSV files named after properties (e.g. `The Conlay.xlsx`). Only the file **names** "
               "are used: each is looked up on PropertyGuru and summarised from its current listings.")
    uploads = st.file_uploader("Files named after properties", type=["xlsx", "xls", "csv"],
                               accept_multiple_files=True, key="price_files")
    if not uploads:
        return
    # Same name with different capitals / spacing / symbols counts once
    by_key = {}
    for u in uploads:
        n = pl.clean_name(Path(u.name).stem)
        by_key.setdefault(pl.name_key(n), n)
    names = list(by_key.values())

    # 1) Match names to PropertyGuru properties (fast, works anywhere)
    matches = st.session_state.setdefault("pg_matches", {})
    for n in names:
        if n not in matches:
            try:
                matches[n] = pl.find_property(n)
            except Exception as e:
                matches[n] = []
                st.warning(f"Lookup failed for {n}: {e}")

    # 2) Price ranges (needs Chrome on this PC; ~25s per property, remembered for this session)
    stats = st.session_state.setdefault("pg_prices", {})
    todo = list(dict.fromkeys(m[0]["id"] for n in names if (m := matches[n]) and m[0]["id"] not in stats))
    if todo:
        st.info(f"Checking {len(todo)} propert{'y' if len(todo) == 1 else 'ies'} on PropertyGuru "
                f"(about {len(todo) * 25 // 60 + 1} min). A Chrome window will open - please leave it open.")
        if st.button("Start", type="primary"):
            bar = st.progress(0.0)
            try:
                import tempfile
                from patchright.sync_api import sync_playwright
                import scraper
                with sync_playwright() as p:
                    ctx = p.chromium.launch_persistent_context(tempfile.mkdtemp(), channel="chrome",
                                                               headless=False, no_viewport=True)
                    page = ctx.pages[0]

                    def fetch(url):
                        data = scraper.load_next_data(page, url)
                        time.sleep(random.uniform(1.5, 3))
                        return data

                    for i, pid in enumerate(todo):
                        name = next(m[0]["name"] for m in matches.values() if m and m[0]["id"] == pid)
                        bar.progress(i / len(todo), text=f"{name} ({i + 1}/{len(todo)})")
                        try:
                            stats[pid] = {k: pl.price_range(fetch, pid, k) for k in ("rent", "sale")}
                        except Exception as e:
                            st.warning(f"{name}: couldn't load listings ({str(e).splitlines()[0]})")
                    ctx.close()
            except Exception as e:
                st.error("Couldn't open Chrome. Price ranges only work when the dashboard runs on your PC "
                         f"(run_dashboard.bat), not on the online app. Details: {str(e).splitlines()[0]}")
            bar.empty()
            st.rerun()

    # 3) Results table - one row per PropertyGuru property, even if several files point to it
    rows, row_of = [], {}
    for n in names:
        m = matches[n]
        if not m:
            rows.append({"File": n, "Property": None, "Location": "No match on PropertyGuru"})
            continue
        top = m[0]
        if top["id"] in row_of:
            row_of[top["id"]]["File"] += f", {n}"
            continue
        rent, sale = stats.get(top["id"], {}).get("rent", (None, None)), stats.get(top["id"], {}).get("sale", (None, None))
        row_of[top["id"]] = {
            "File": n,
            "Property": pl.listings_url(top["id"], "rent") + "#" + top["name"],
            "Location": top["address"],
            "Map": top["map"],
            f"Rent RM {pl.RENT_MIN:,}–{pl.RENT_MAX:,} (per month)": pl.fmt_range(*rent),
            "Sale price (RM)": pl.fmt_range(*sale),
            "Also matched": ", ".join(x["name"] for x in m[1:3]),
        }
        rows.append(row_of[top["id"]])
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Property": st.column_config.LinkColumn("Property", display_text=r"^[^#]*#(.*)$"),
        "Map": st.column_config.LinkColumn("Map", display_text="Open map"),
    })
    st.caption(f"Rent range only counts listings between RM {pl.RENT_MIN:,} and RM {pl.RENT_MAX:,} a month. "
               "Obvious typos (e.g. a RM 20,000 'sale') are left out. \"Also matched\" shows other properties "
               "with similar names, in case the wrong one was picked.")
    if stats:
        if st.button("Check again (refresh prices)"):
            st.session_state.pop("pg_prices", None)
            st.rerun()


if tool == "Phone filter":
    phone_filter_page()
    st.stop()
if tool == "Price filter":
    price_filter_page()
    st.stop()


# ---------- data ----------
@st.cache_data(ttl=600, show_spinner="Loading listings…")
def load_listings(_db):
    rows, start, size = [], 0, 1000  # Supabase returns max 1000 rows per request
    while True:
        batch = _db.table("listings").select("*").range(start, start + size - 1).execute().data
        rows += batch
        if len(batch) < size:
            break
        start += size
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ["first_seen", "last_seen", "price_changed_at"]:
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    for c in ["price_rm", "initial_price_rm", "previous_price_rm", "psf_rm", "floor_area_sqft", "bedrooms",
              "bathrooms", "latitude", "longitude"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Furnishing etc. only come from the listing's own page; make "not opened yet" explicit
    pending = df.details_scraped_at.isna()
    df.loc[pending, "furnishing"] = "Pending (details not scraped yet)"
    df.loc[~pending, "furnishing"] = df.loc[~pending, "furnishing"].fillna("Not specified")
    return df


df = load_listings(db)

with st.sidebar:
    if st.button("Refresh data"):
        load_listings.clear()
        st.rerun()

if df.empty:
    st.info("No listings yet - run the scraper first.")
    st.stop()

# ---------- filters ----------
with st.sidebar:
    st.header("Filters")
    active_days = st.slider("Seen in the last N days", 1, 60, 3,
                            help="Listings not seen recently were probably taken down.")
    pmin, pmax = int(df.price_rm.min()), int(df.price_rm.max())
    price = st.slider("Monthly rent (RM)", pmin, pmax, (pmin, min(pmax, 50000)), step=500)
    districts = st.multiselect("District", sorted(df.district.dropna().unique()))
    beds = st.multiselect("Bedrooms", sorted(df.bedrooms.dropna().astype(int).unique()))
    types = st.multiselect("Property type", sorted(df.property_type.dropna().unique()))
    furnish = st.multiselect("Furnishing", sorted(df.furnishing.dropna().unique()))
    text = st.text_input("Search name / address")

cutoff = datetime.now(timezone.utc) - timedelta(days=active_days)
f = df[(df.last_seen >= cutoff) & df.price_rm.between(*price)]
if districts:
    f = f[f.district.isin(districts)]
if beds:
    f = f[f.bedrooms.isin(beds)]
if types:
    f = f[f.property_type.isin(types)]
if furnish:
    f = f[f.furnishing.isin(furnish)]
if text:
    f = f[f.title.str.contains(text, case=False, na=False) | f.address.str.contains(text, case=False, na=False)]

# ---------- page ----------
st.title("KL Tracker")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Listings", f"{len(f):,}")
k2.metric("Median rent", f"RM {f.price_rm.median():,.0f}" if len(f) else "–")
k3.metric("Median RM psf", f"{f.psf_rm.median():.2f}" if f.psf_rm.notna().any() else "–")
k4.metric("New in last 7 days", f"{(f.first_seen >= datetime.now(timezone.utc) - timedelta(days=7)).sum():,}")

# Clickable property name: the name rides in the URL fragment and display_text shows it
f = f.assign(property_link=f.url.fillna("") + "#" + f.title.fillna("Listing"))
COLS = {"property_link": "Property", "price_rm": "Rent (RM)", "psf_rm": "RM psf", "floor_area_sqft": "Sqft",
        "bedrooms": "Beds", "bathrooms": "Baths", "property_type": "Type", "furnishing": "Furnishing",
        "district": "District", "area": "Area", "nearest_station": "Nearest station",
        "posted_on": "Posted", "agent_name": "Agent"}
LINK = {"Property": st.column_config.LinkColumn("Property", display_text=r"^[^#]*#(.*)$")}

tab_list, tab_map, tab_drops, tab_district = st.tabs(["Listings", "Map", "Price changes", "By district"])

with tab_list:
    st.dataframe(f.sort_values("price_rm")[list(COLS)].rename(columns=COLS),
                 column_config=LINK, hide_index=True, width="stretch", height=600)
    st.download_button("Download CSV", f.drop(columns="property_link").to_csv(index=False).encode("utf-8-sig"),
                       "listings.csv", "text/csv")

with tab_map:
    pts = f.dropna(subset=["latitude", "longitude"])
    st.caption(f"{len(pts):,} of {len(f):,} listings have coordinates (filled in when the listing page is scraped).")
    st.map(pts, latitude="latitude", longitude="longitude", size=40)

with tab_drops:
    ch = f[f.previous_price_rm.notna()].copy()
    if ch.empty:
        st.info("No price changes detected yet - they appear after the scraper has run on more than one day.")
    else:
        ch["change_rm"] = ch.price_rm - ch.previous_price_rm
        ch["change_pct"] = (ch.change_rm / ch.previous_price_rm * 100).round(1)
        show = {"property_link": "Property", "previous_price_rm": "Was (RM)", "price_rm": "Now (RM)",
                "change_rm": "Change (RM)", "change_pct": "Change %", "price_changed_at": "Changed on",
                "district": "District"}
        st.dataframe(ch.sort_values("change_pct")[list(show)].rename(columns=show),
                     column_config=LINK, hide_index=True, width="stretch")

with tab_district:
    g = (f.groupby("district")
          .agg(listings=("listing_id", "count"), median_rent=("price_rm", "median"), median_psf=("psf_rm", "median"))
          .sort_values("listings", ascending=False))
    st.bar_chart(g.median_psf, horizontal=True, x_label="Median RM psf")
    st.dataframe(g.rename(columns={"listings": "Listings", "median_rent": "Median rent (RM)",
                                   "median_psf": "Median RM psf"}).round(2), width="stretch")
