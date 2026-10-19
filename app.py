"""Streamlit dashboard for scraped listings. Run: streamlit run app.py"""
import io
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from supabase import ClientOptions, create_client

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
SUPABASE_URL = os.environ.get("SUPABASE_URL") or st.secrets["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or st.secrets["SUPABASE_PUBLISHABLE_KEY"]

st.set_page_config(page_title="KL Tracker", page_icon="🏙️", layout="wide")


# ---------- login ----------
# "Keep me logged in": the Supabase refresh token is kept in a browser cookie for 30 days. Supabase refresh
# tokens are single-use, so every time it changes the cookie is rewritten in the same page run.
COOKIE = "kt_session"
COOKIE_DAYS = 30


def new_client():
    # No background token refresh: refreshes happen during page runs, so the cookie is always updated with them
    return create_client(SUPABASE_URL, SUPABASE_KEY, options=ClientOptions(auto_refresh_token=False))


def write_cookie(value, days=COOKIE_DAYS):
    """Queue a cookie write; it's sent with the next rendered page (see flush_cookie)."""
    st.session_state.cookie_out = (value, days)


def flush_cookie():
    out = st.session_state.pop("cookie_out", None)
    if out is None:
        return
    value, days = out
    st.html(f"""<script>
        document.cookie = "{COOKIE}=" + encodeURIComponent({value!r}) + "; Max-Age={int(days * 86400)}; path=/; "
            + "SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
        </script>""", unsafe_allow_javascript=True, width="content")


def remember(client):
    """Save the current refresh token to the cookie if it changed (or forget it if 'keep me logged in' is off)."""
    if not st.session_state.get("keep_login", True):
        return
    sess = client.auth.get_session()  # also refreshes an expired access token
    if sess and sess.refresh_token != st.session_state.get("cookie_token"):
        st.session_state.cookie_token = sess.refresh_token
        write_cookie(sess.refresh_token)


def restore_from_cookie():
    token = st.context.cookies.get(COOKIE)
    if not token or st.session_state.get("logged_out") or st.session_state.get("cookie_tried"):
        return
    st.session_state.cookie_tried = True
    client = new_client()
    try:
        res = client.auth.refresh_session(token)
        st.session_state.client = client
        st.session_state.email = res.user.email if res.user else ""
    except Exception:
        write_cookie("", days=0)  # expired / already used - forget it, show the login form


def login_screen():
    st.title("KL Tracker")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        keep = st.checkbox(f"Keep me logged in ({COOKIE_DAYS} days)", value=True)
        if st.form_submit_button("Log in", type="primary"):
            client = new_client()
            try:
                client.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.update(client=client, email=email, keep_login=keep, logged_out=False)
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")


if "client" not in st.session_state:
    restore_from_cookie()
if "client" not in st.session_state:
    flush_cookie()
    login_screen()
    st.stop()

db = st.session_state.client
remember(db)
flush_cookie()

with st.sidebar:
    st.caption(f"Logged in as {st.session_state.email}")
    if st.button("Log out"):
        try:
            db.auth.sign_out()
        except Exception:
            pass
        for k in ("client", "cookie_token"):
            st.session_state.pop(k, None)
        st.session_state.logged_out = True
        write_cookie("", days=0)
        st.rerun()
    tool = st.radio("Page", ["Listings", "Condo prices", "WhatsApp", "Phone filter", "Price filter"])


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
            f"Sale RM {pl.SALE_MIN / 1e6:g}M and above": pl.fmt_range(*sale),
            "Also matched": ", ".join(x["name"] for x in m[1:3]),
        }
        rows.append(row_of[top["id"]])
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Property": st.column_config.LinkColumn("Property", display_text=r"^[^#]*#(.*)$"),
        "Map": st.column_config.LinkColumn("Map", display_text="Open map"),
    })
    st.caption(f"Rent range only counts listings between RM {pl.RENT_MIN:,} and RM {pl.RENT_MAX:,} a month; "
               f"sale range only counts listings of RM {pl.SALE_MIN:,} and above (blank = none listed). "
               "Obvious typos (e.g. a RM 20,000 'sale') are left out. \"Also matched\" shows other properties "
               "with similar names, in case the wrong one was picked.")
    if stats:
        if st.button("Check again (refresh prices)"):
            st.session_state.pop("pg_prices", None)
            st.rerun()


# ---------- WhatsApp owner listings ----------
MYT = timezone(timedelta(hours=8))
PHOTO_BUCKET = "owner-photos"


@st.cache_data(ttl=60, show_spinner="Loading owner listings…")
def load_owner_listings(_db):
    rows, start = [], 0
    while True:
        batch = (_db.table("owner_listings").select("*").order("posted_at", desc=True)
                 .range(start, start + 999).execute().data)
        rows += batch
        if len(batch) < 1000:
            return rows
        start += 1000


def signed_urls(paths, ttl=3600):
    """{path: temporary private link}, cached in the session for 50 minutes so links stay the same between
    page refreshes (Supabase signs them for 1 hour)."""
    cache = st.session_state.setdefault("signed_cache", {})
    now = datetime.now().timestamp()
    missing = [p for p in dict.fromkeys(paths) if p not in cache or cache[p][1] < now]
    if missing:
        try:
            for s in db.storage.from_(PHOTO_BUCKET).create_signed_urls(missing, ttl):
                url = s.get("signedURL") or s.get("signedUrl")
                if url:
                    cache[s["path"]] = (url, now + ttl - 600)
        except Exception:
            pass
    return {p: cache[p][0] for p in paths if p in cache}


def saved_listings_tab():
    from wa_export import describe, from_row, listing_zip

    st.caption("Everything saved from your WhatsApp scans (stored online, so this tab also works on the online "
               "app). Click a row to see the full description and download it with its pictures.")
    try:
        rows = load_owner_listings(db)
    except Exception as e:
        st.warning(f"Couldn't load owner listings ({str(e).splitlines()[0]}). If this is the first time, run "
                   "schema_owner_listings.sql once in the Supabase SQL Editor, then scan on the WhatsApp page.")
        return
    if not rows:
        st.info("Nothing saved yet - scan some chats on the WhatsApp page (on your PC).")
        return

    f1, f2, f3 = st.columns([1, 2, 2])
    kind = f1.radio("Type", ["All", "Rent", "Sale"], horizontal=True)
    text = f2.text_input("Search location / phone / message")
    prices = [r["price_rm"] for r in rows if r.get("price_rm")]
    lo, hi = (min(prices), max(prices)) if prices else (0, 1)
    pr = f3.slider("Price (RM)", lo, max(hi, lo + 1), (lo, max(hi, lo + 1)))
    shown = [r for r in rows
             if (kind == "All" or r.get("listing_type") == kind)
             and (not r.get("price_rm") or pr[0] <= r["price_rm"] <= pr[1])
             and (not text or text.lower() in " ".join(str(r.get(k) or "") for k in
                                                       ("place", "phones", "sender_phone", "message_text")).lower())]
    st.caption(f"{len(shown):,} of {len(rows):,} listings")

    # Signed (temporary, private) links to each listing's first picture for the thumbnails. Reused for 50 minutes:
    # new links on every refresh would make the table look "different" and clear the row you clicked.
    first_pic = {r["message_id"]: next((p for p in r.get("photo_paths") or [] if p.endswith(".jpg")), None)
                 for r in shown[:300]}
    thumbs = signed_urls([p for p in first_pic.values() if p])
    table = pd.DataFrame([{
        "Picture": thumbs.get(first_pic.get(r["message_id"])),
        "Owner": r.get("owner_name"),
        "Number": (r.get("phones") or "").replace("; ", ", ") or r.get("sender_phone"),
        "Price (RM)": r.get("price_rm"),
        "Rent / Sale": r.get("listing_type"),
        "Location": r.get("place"),
        "Unit": r.get("unit_no"),
        "Rental (RM/mo)": r.get("rental_rm"),
        "Sqft": r.get("sqft"),
        "Furnishing": r.get("furnishing"),
        "Photos": len(r.get("photo_paths") or []),
        "Posted": pd.to_datetime(r.get("posted_at"), utc=True).tz_convert(MYT).strftime("%Y-%m-%d"),
    } for r in shown])
    picked = st.dataframe(table, hide_index=True, width="stretch", height=480, on_select="rerun",
                          selection_mode="single-row", row_height=60, key="saved_listings_table",
                          column_config={"Picture": st.column_config.ImageColumn("Picture", width="small"),
                                         "Price (RM)": st.column_config.NumberColumn(format="%,d")})
    sel = [i for i in picked.selection.rows if i < len(shown)]
    if not sel:
        st.caption("👆 Click a row for its description and pictures.")
        return

    r = shown[sel[0]]
    listing = from_row(r)
    st.subheader(f"{r.get('listing_type') or ''} · {r.get('place') or 'Listing'} · "
                 f"{'RM ' + format(r['price_rm'], ',') if r.get('price_rm') else ''}")
    name = re.sub(r"[^A-Za-z0-9 ]+", "", r.get("place") or "listing").strip() or "listing"
    paths = r.get("photo_paths") or []
    b1, b2 = st.columns(2)
    b1.download_button("Download description (.txt)", describe(listing).encode("utf-8-sig"),
                       f"{name}.txt", "text/plain", on_click="ignore")
    if paths and b2.button(f"Prepare ZIP: description + {len(paths)} files", key=f"zip-{r['message_id']}"):
        with st.spinner("Collecting pictures…"):
            photos = []
            for p in paths:
                try:
                    photos.append((p, db.storage.from_(PHOTO_BUCKET).download(p)))
                except Exception:
                    pass
        st.session_state.owner_zip = (r["message_id"], listing_zip(listing, photos), f"{name}.zip")
    oz = st.session_state.get("owner_zip")
    if oz and oz[0] == r["message_id"]:
        b2.download_button(f"Download {oz[2]}", oz[1], oz[2], "application/zip", type="primary", on_click="ignore")
    st.text(describe(listing))
    if paths:
        links = signed_urls(paths)
        if len(links) < len(paths):
            st.warning(f"{len(paths) - len(links)} file(s) couldn't be loaded.")
        cols = st.columns(4)
        for n, p in enumerate(paths):
            url = links.get(p)
            if not url:
                continue
            col, ext = cols[n % 4], p.rsplit(".", 1)[-1].lower()
            # the file name must be URL-encoded - a space would break the link
            link = f"{url}&download={quote(f'{name}_{n + 1:02d}.{ext}')}"
            if ext == "pdf":
                col.markdown(f"📄 **PDF**  \n[⬇ Download PDF {n + 1}]({link})")
            elif ext == "mp4":
                col.video(url)
                col.markdown(f"[⬇ Download video {n + 1}]({link})")
            else:
                col.image(url, width="stretch")
                col.markdown(f"[⬇ Download picture {n + 1}]({link})")


def whatsapp_page():
    st.title("WhatsApp owner listings")
    scan, saved = st.tabs(["Scan chats", "Saved listings"])
    with scan:
        scan_chats_tab()
    with saved:
        saved_listings_tab()


def scan_chats_tab():
    import time

    import wa_client
    from phone_filter import extract_from_text
    from wa_export import make_zip
    from wa_parser import group_by_owner, group_listings, uses_separators

    st.caption("Link your WhatsApp by QR code, pick chats (individual or groups), and turn property posts into "
               "listings (type, price, sqft, place, furnishing, phone numbers) with their photos. Read-only: "
               "nothing is ever sent from your WhatsApp. Works on your PC only.")
    try:
        s = wa_client.ensure_running()
    except Exception as e:
        st.error(str(e))
        return

    if s["status"] not in ("ready", "syncing"):
        @st.fragment(run_every="3s")
        def login_box():
            try:  # also restarts the connector if it stopped because its QR code expired
                cur = wa_client.ensure_running()
            except Exception as e:
                st.error(str(e))
                return
            if cur.get("status") in ("ready", "syncing"):
                st.rerun(scope="app")
            if cur.get("status") == "qr" and cur.get("qr"):
                import base64
                st.image(base64.b64decode(cur["qr"].split(",", 1)[1]), width=300)
                st.markdown("On your phone: **WhatsApp → ⋮ (or Settings) → Linked devices → Link a device**, "
                            "then scan this code. It appears on your phone as **KL Tracker**.")
            elif cur.get("status") in ("error", "logged_out"):
                st.error(cur.get("error") or "WhatsApp disconnected.")
                if st.button("Start again"):
                    wa_client.logout()
                    st.rerun(scope="app")
            else:
                st.info("Connecting to WhatsApp… (first start can take ~30 seconds)")
        login_box()
        return

    me, hist = s.get("me") or {}, s.get("history") or {}
    top = st.columns([4, 1])
    top[0].success(f"Connected as **{me.get('name') or '?'}** (+{me.get('number') or '?'})")
    if top[1].button("Log out WhatsApp"):
        wa_client.logout()
        st.session_state.pop("wa_result", None)
        st.rerun()
    st.caption(f"History from your phone: {hist.get('messages', 0):,} messages received so far "
               f"({hist.get('batches', 0)} batches). Right after linking your phone keeps sending older chats "
               "for a few minutes - press **Reload chat list** to see them. New messages are added automatically "
               "while this PC is on.")

    if "wa_chats" not in st.session_state or st.button("Reload chat list"):
        st.session_state.wa_chats = wa_client.chats()
    chats = st.session_state.wa_chats
    if not chats:
        st.info("No messages stored yet - wait a minute for your phone to send history, then Reload chat list.")
        return
    label = {c["jid"]: f"{'👥' if c['is_group'] else '👤'} {c['name'] or ('+' + c['phone'] if c['phone'] else c['jid'])}"
                       f"{' (+' + c['phone'] + ')' if c['phone'] and c['name'] else ''} · {c['messages']} msgs"
             for c in chats}
    groups = [c["jid"] for c in chats if c["is_group"]]

    # Settings kept on this PC: your own numbers (never recorded as an owner's) + your listing groups
    settings = wa_client.load_settings()
    linked = [n for n, _, _ in extract_from_text(me.get("number") or "") if n]
    with st.expander("⚙️ My numbers & listing groups", expanded=not settings["listing_groups"]):
        nums_text = st.text_area("My own numbers - never recorded as an owner's number (one per line)",
                                 "\n".join(settings["my_numbers"]), height=90,
                                 help="e.g. the numbers you use to blast. The number linked here is always ignored.")
        my_groups = st.multiselect("My listing groups (where owner listings are posted)", groups,
                                   [g for g in settings["listing_groups"] if g in groups], format_func=label.get,
                                   help="They're pre-selected for scanning. Group names come from WhatsApp - "
                                        "press Reload chat list if a new group is missing.")
        if st.button("Save settings"):
            settings["my_numbers"] = list(dict.fromkeys(n for n, _, _ in extract_from_text(nums_text) if n))
            settings["listing_groups"] = my_groups
            wa_client.save_settings(settings)
            st.success(f"Saved: {len(settings['my_numbers'])} own number(s), {len(my_groups)} listing group(s).")
    mine = set(settings["my_numbers"]) | set(linked)

    c1, c2 = st.columns([3, 1])
    views = (["My listing groups"] if settings["listing_groups"] else []) + ["Individual chats", "Groups", "All"]
    kind = c2.radio("Show", views, label_visibility="collapsed")
    if kind == "My listing groups":
        options = [g for g in settings["listing_groups"] if g in label]
        default = options
    else:
        options = [c["jid"] for c in chats if kind == "All" or (kind == "Groups") == c["is_group"]]
        default = []
    picked = c1.multiselect("Chats to scan (newest first)", options, default, format_func=label.get,
                            key=f"pick-{kind}")
    days = st.slider("Messages from the last N days", 1, 365, 60)
    read_pictures = st.checkbox("Read WhatsApp screenshots and PDFs", value=True,
                                help="Screenshots of WhatsApp chats and PDF files are read on this PC (about 1-3 "
                                     "seconds each); ordinary photos are skipped but still attached. In chats where "
                                     "you put a '.' between owners, everything between two dots counts as one "
                                     "owner (one number, possibly several properties).")

    if picked and st.button("Scan messages", type="primary"):
        run_dir = ROOT_DIR / "wa_exports" / datetime.now().strftime("%Y%m%d_%H%M%S")
        by_key = {c["jid"]: c["jids"] for c in chats}
        msgs = wa_client.messages([j for k in picked for j in by_key.get(k, [k])], time.time() - days * 86400)
        files_msgs = [m for m in msgs if m["has_image"] or m["is_pdf"] or m["is_video"]]
        if read_pictures and files_msgs:
            from wa_ocr import is_chat_screenshot, read_pdf, read_text
            with st.spinner(f"Downloading {len(files_msgs)} pictures / PDFs / videos…"):
                files = wa_client.download([(m["chatId"], m["id"]) for m in files_msgs], run_dir / "media")
            for m in files_msgs:
                if m["id"] in files:
                    m["photo"] = files[m["id"]]
            # Read WhatsApp screenshots and PDFs; ordinary photos are skipped (they're only attached)
            to_read = [m for m in files_msgs if m.get("photo") and
                       (m["is_pdf"] or is_chat_screenshot(run_dir / "media" / m["photo"]))]
            skipped = sum(1 for m in files_msgs if m.get("photo")) - len(to_read)
            bar = st.progress(0.0, text="Reading screenshots / PDFs…")
            for i, m in enumerate(to_read, 1):
                bar.progress(i / len(to_read), text=f"Reading screenshots / PDFs… {i}/{len(to_read)} "
                                                    f"({skipped} ordinary photos skipped)")
                path = run_dir / "media" / m["photo"]
                if m["is_pdf"]:
                    m["pdf_text"] = read_pdf(path)
                else:
                    m["ocr_text"] = read_text(path)
            bar.empty()
        # Chats where you separate owners with a "." message are grouped per owner; others per message
        by_chat = {}
        for m in msgs:
            by_chat.setdefault(m["chatId"], []).append(m)
        listings = []
        for chat_msgs in by_chat.values():
            listings += (group_by_owner(chat_msgs, mine, settings.get("owner_overrides")) if uses_separators(chat_msgs)
                         else group_listings(chat_msgs))
        refs = [p for l in listings for p in l["photos"] if isinstance(p, tuple)]
        if refs:
            with st.spinner(f"Downloading {len(refs)} photos…"):
                files = wa_client.download(refs, run_dir / "media")
            for l in listings:
                l["photos"] = [p if isinstance(p, str) else files[p[1]] for p in l["photos"]
                               if isinstance(p, str) or p[1] in files]
        for l in listings:  # sender's number in local 01x format when possible
            nums = [n for n, _, _ in extract_from_text(l["sender_phone"] or "") if n]
            l["sender_phone"] = nums[0] if nums else l["sender_phone"]
            # Your own numbers are never an owner's: drop them from the message's numbers / the sender
            l["phones"] = [p for p in l["phones"] if p not in mine]
            if l["sender_phone"] in mine:
                l["sender_phone"], l["sender_name"] = None, "you"
        st.session_state.wa_result = {"listings": listings, "media": str(run_dir / "media"),
                                      "messages": len(msgs), "saved": None}
        # Upload each file ONCE to the private bucket (owner-photos/files/<file>); listings just refer to it.
        # Supabase's free plan allows up to 50 MB per file - bigger videos stay in the ZIP only.
        uploaded, upload_error, too_big = {}, None, 0
        needed = list(dict.fromkeys(p for l in listings for p in l["photos"]))
        if needed:
            bar = st.progress(0.0, text="Uploading pictures / PDFs / videos…")
            for i, p in enumerate(needed, 1):
                bar.progress(i / len(needed), text=f"Uploading pictures / PDFs / videos… {i}/{len(needed)}")
                src = run_dir / "media" / p
                if not src.exists():
                    continue
                if src.stat().st_size > 50 * 1024 * 1024:
                    too_big += 1
                    continue
                ext = p.rsplit(".", 1)[-1].lower()
                ctype = {"pdf": "application/pdf", "mp4": "video/mp4"}.get(ext, "image/jpeg")
                try:
                    db.storage.from_(PHOTO_BUCKET).upload(f"files/{p}", src.read_bytes(),
                                                          {"content-type": ctype, "upsert": "true"})
                    uploaded[p] = f"files/{p}"
                except Exception as e:
                    upload_error = upload_error or str(e).splitlines()[0]
            bar.empty()
        # Save to Supabase (re-scans update the same rows instead of duplicating)
        rows = [{"message_id": l["id"], "chat": l["chat"],
                 "photo_paths": [uploaded[p] for p in l["photos"] if p in uploaded],
                 "posted_at": datetime.fromtimestamp(l["posted_at"], timezone.utc).isoformat(),
                 "sender_name": l["sender_name"], "sender_phone": l["sender_phone"], "listing_type": l["type"],
                 "price_rm": l["price_rm"], "sqft": l["sqft"], "bedrooms": l["bedrooms"],
                 "bathrooms": l["bathrooms"], "furnishing": l["furnishing"], "place": l["place"],
                 "phones": "; ".join(l["phones"]), "photo_count": len(l["photos"]), "message_text": l["text"],
                 "scanned_by": st.session_state.email,
                 # added later (schema_owner_listings_v2.sql)
                 "owner_name": l.get("owner_name"), "unit_no": l.get("unit_no"), "address": l.get("address"),
                 "rental_rm": l.get("rental_rm"), "tenure": l.get("tenure"), "property_type": l.get("property_type"),
                 "price_psf": l.get("price_psf"), "owner_no": l.get("owner_no")} for l in listings]
        new_cols = ("owner_name", "unit_no", "address", "rental_rm", "tenure", "property_type", "price_psf", "owner_no")
        notes = [f"{too_big} video(s) over 50 MB kept in the ZIP only"] if too_big else []
        try:
            try:
                for i in range(0, len(rows), 500):
                    db.table("owner_listings").upsert(rows[i:i + 500], on_conflict="message_id").execute()
            except Exception as e:
                if "column" not in str(e).lower():
                    raise
                # Online table not updated yet: save without the new fields, and say so
                for i in range(0, len(rows), 500):
                    db.table("owner_listings").upsert([{k: v for k, v in r.items() if k not in new_cols}
                                                       for r in rows[i:i + 500]], on_conflict="message_id").execute()
                notes.append("owner name / unit / rental not saved online yet - run schema_owner_listings_v2.sql")
            # This scan replaces what earlier scans saved for the same chats (old, less accurate results)
            try:
                chats_scanned = list({l["chat"] for l in listings})
                if chats_scanned:
                    db.table("owner_listings").delete().in_("chat", chats_scanned) \
                        .not_.in_("message_id", [r["message_id"] for r in rows]).execute()
            except Exception:
                pass  # needs the delete permission from schema_owner_listings_v2.sql
            if upload_error:
                notes.append(f"some files not uploaded ({upload_error})")
            st.session_state.wa_result["saved"] = True if not notes else "; ".join(notes)
            load_owner_listings.clear()
        except Exception as e:
            st.session_state.wa_result["saved"] = str(e).splitlines()[0]

    res = st.session_state.get("wa_result")
    if not res:
        return
    listings, media = res["listings"], Path(res["media"])
    k1, k2, k3 = st.columns(3)
    k1.metric("Messages read", f"{res['messages']:,}")
    k2.metric("Listings found", f"{len(listings):,}")
    k3.metric("Photos", f"{sum(len(l['photos']) for l in listings):,}")
    if res["saved"] is True:
        st.caption("✅ Saved online - see them any time in the **Saved listings** tab.")
    elif res["saved"]:
        st.warning(f"Not saved to Supabase: {res['saved']}. If the table doesn't exist yet, run "
                   "schema_owner_listings.sql in the Supabase SQL Editor once.")
    if not listings:
        st.info("No property listings found in those messages.")
        return
    st.download_button(f"Download ZIP (a folder per listing with description.txt + photos, plus Excel)",
                       make_zip(listings, media), f"WhatsApp listings {datetime.now():%Y-%m-%d %H%M}.zip",
                       "application/zip", type="primary", on_click="ignore")
    st.dataframe(pd.DataFrame([{
        "Owner #": l.get("owner_no"), "Owner": l.get("owner_name"),
        "Phones": ", ".join(l["phones"]) or l["sender_phone"],
        "Type": l["type"], "Price (RM)": l["price_rm"], "RM/sqft": l.get("price_psf"), "Place": l["place"],
        "Unit": l.get("unit_no"), "Rental (RM/mo)": l.get("rental_rm"), "Sqft": l["sqft"],
        "Beds": l["bedrooms"], "Baths": l["bathrooms"], "Furnishing": l["furnishing"], "Tenure": l.get("tenure"),
        "Files": len(l["photos"]),
        "Posted": datetime.fromtimestamp(l["posted_at"], MYT).strftime("%Y-%m-%d %H:%M"),
        "From picture": "📷" if l.get("from_picture") else "",
        "Chat": l["chat"], "Message": l["text"][:200]} for l in listings]), hide_index=True, width="stretch")


# ---------- condo prices (one row per condo: KLCC, Mont Kiara ...) ----------
@st.cache_data(ttl=300, show_spinner="Loading condo prices…")
def load_condo_summary(_db):
    import json
    return json.loads(_db.storage.from_(PHOTO_BUCKET).download("data/condo_summary.json"))


def condo_prices_page():
    import json
    import subprocess
    import sys

    st.title("Condo prices")
    st.caption("One row per condo: lowest / highest sale price, lowest / highest rent and smallest / largest size, "
               "worked out from all current PropertyGuru listings (obvious typos left out).")

    # Update button - only on the PC (PropertyGuru blocks cloud servers)
    status_file = ROOT_DIR / "data" / "condo_summary_status.json"
    on_pc = (ROOT_DIR / "condo_summary.py").exists() and bool(os.environ.get("SUPABASE_SECRET_KEY"))
    if on_pc:
        try:
            stt = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stt = {}
        recent = stt.get("at") and (datetime.now() - datetime.fromisoformat(stt["at"])).total_seconds() < 600
        if stt.get("state") == "running" and recent:
            st.info(f"⏳ Updating on this PC: {stt.get('area')} {stt.get('kind')} - page {stt.get('page')} of "
                    f"{stt.get('pages') or '?'} (a full update takes about an hour). Reload to see progress.")
        elif st.button("🔄 Update now (runs on this PC, ~1 hour)"):
            (ROOT_DIR / "logs").mkdir(exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen([sys.executable, "-u", str(ROOT_DIR / "condo_summary.py")], cwd=ROOT_DIR,
                             creationflags=flags, stdout=open(ROOT_DIR / "logs" / "condo_summary.log", "a"),
                             stderr=subprocess.STDOUT)
            st.success("Started - it runs in the background; you can close this page.")

    try:
        data = load_condo_summary(db)
    except Exception:
        st.info("No condo prices yet - click **Update now** on the PC (first run takes about an hour).")
        return
    df = pd.DataFrame(data.get("rows") or [])
    if df.empty:
        st.info("No condo prices yet.")
        return
    updated = data.get("updated") or {}
    if isinstance(updated, dict):
        st.caption("Last updated: " + ", ".join(
            f"{a} {pd.to_datetime(t, utc=True).tz_convert(MYT):%d %b %Y %H:%M}" for a, t in sorted(updated.items())))
    if st.button("Refresh", key="condo-refresh"):
        load_condo_summary.clear()
        st.rerun()

    f1, f2, f3 = st.columns([2, 2, 1])
    areas = f1.multiselect("Area", sorted(df.area.unique()), default=sorted(df.area.unique()))
    text = f2.text_input("Search condo name")
    has = f3.radio("Show", ["All", "For sale", "For rent"], horizontal=False)
    view = df[df.area.isin(areas)]
    if text:
        view = view[view.condo.str.contains(text, case=False, na=False)]
    if has == "For sale":
        view = view[view.sale_listings > 0]
    elif has == "For rent":
        view = view[view.rent_listings > 0]
    st.caption(f"{len(view):,} condos")

    cols = {"condo": "Condo", "area": "Area", "sale_min": "Sale from (RM)", "sale_max": "Sale to (RM)",
            "sale_listings": "For sale", "rent_min": "Rent from (RM)", "rent_max": "Rent to (RM)",
            "rent_listings": "For rent", "sqft_min": "Size from (sqft)", "sqft_max": "Size to (sqft)",
            "psf_min": "RM psf from", "psf_max": "RM psf to", "bedrooms": "Bedrooms", "tenure": "Tenure",
            "property_type": "Type", "sale_url": "Sale listings", "rent_url": "Rent listings"}
    table = view[[c for c in cols if c in view.columns]].rename(columns=cols)
    money = st.column_config.NumberColumn(format="%,d")
    st.dataframe(table, hide_index=True, width="stretch", height=600, column_config={
        **{c: money for c in ("Sale from (RM)", "Sale to (RM)", "Rent from (RM)", "Rent to (RM)",
                               "Size from (sqft)", "Size to (sqft)", "RM psf from", "RM psf to")},
        "Sale listings": st.column_config.LinkColumn(display_text="Open"),
        "Rent listings": st.column_config.LinkColumn(display_text="Open"),
    })
    xbuf = io.BytesIO()
    table.to_excel(xbuf, index=False)
    st.download_button("Download Excel", xbuf.getvalue(), f"Condo prices {datetime.now():%Y-%m-%d}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", on_click="ignore")


if tool == "Condo prices":
    condo_prices_page()
    st.stop()
if tool == "WhatsApp":
    whatsapp_page()
    st.stop()
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
