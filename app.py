"""Streamlit dashboard for scraped listings. Run: streamlit run app.py"""
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
    tool = st.radio("Page", ["Listings", "Phone filter"], horizontal=True)


# ---------- phone filter ----------
def phone_filter_page():
    from phone_filter import filter_file

    st.title("Phone filter")
    st.caption("Upload an Excel or CSV file. Every cell on every sheet is scanned; only Malaysian mobile numbers "
               "are kept, cleaned to 0XXXXXXXXX, with duplicates removed. Nothing is saved.")
    up = st.file_uploader("Excel / CSV file", type=["xlsx", "xls", "csv"])
    if not up:
        with st.expander("What gets kept and removed"):
            st.markdown(
                "- **Kept:** mobile numbers 010, 011, 012, 013, 014, 016, 017, 018, 019\n"
                "- **Fixed:** `60123456789` / `+60 12-345 6789` → `0123456789`; "
                "`123456789` (Excel dropped the 0) → `0123456789`\n"
                "- **Removed:** landlines (`03 3783737`, `33838383`), 015 internet/VoIP numbers, "
                "foreign numbers (`+65…`, `+1…`), incomplete numbers, duplicates")
        return
    try:
        numbers, rejected, dupes = filter_file(up.name, up.getvalue())
    except Exception as e:
        st.error(f"Couldn't read this file: {e}")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Mobile numbers kept", f"{len(numbers):,}")
    c2.metric("Duplicates removed", f"{dupes:,}")
    c3.metric("Other numbers removed", f"{len(rejected):,}")

    txt_name = Path(up.name).stem + ".txt"
    st.download_button(f"Download {txt_name}", "\r\n".join(numbers).encode("utf-8"), txt_name, "text/plain",
                       type="primary", disabled=not numbers)
    left, right = st.columns(2)
    with left:
        st.subheader("Kept")
        st.dataframe(pd.DataFrame({"Number": numbers}), hide_index=True, height=400)
    with right:
        st.subheader("Removed")
        st.dataframe(pd.DataFrame(rejected, columns=["Found in file", "Reason"]), hide_index=True, height=400)


if tool == "Phone filter":
    phone_filter_page()
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
