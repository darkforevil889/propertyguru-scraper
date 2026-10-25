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
    pages = ["Listings", "Condo prices", "WhatsApp listing", "PropertyGuru posting", "WhatsApp blast", "Chat bot",
             "AI call", "Google Map Extractor", "Phone filter", "Price filter"]
    if st.session_state.get("page") not in pages:  # e.g. a page that was renamed since
        st.session_state.page = {"WhatsApp": "WhatsApp listing"}.get(st.session_state.get("page"), pages[0])
    tool = st.radio("Page", pages, key="page")


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
                 .order("message_id").range(start, start + 999).execute().data)
        rows += batch
        if len(batch) < 1000:
            return rows
        start += 1000


@st.cache_data(ttl=60, show_spinner=False)
def load_market_check(_db):
    """PropertyGuru comparisons of saved listings (market_check.py) - {message_id: result}."""
    import market_check
    return market_check.load_results(_db)


def blast_remote_status(_db):
    """The PC's last heartbeat (pc_status 'pc') - progress of things it runs for the online dashboard."""
    try:
        rows = _db.table("pc_status").select("data").eq("name", "pc").execute().data
        return rows[0]["data"] if rows else {}
    except Exception:
        return {}


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


def agent_post_state():
    """PropertyGuru posting (agent_post.py, runs on the PC) -> (module, on_pc, status, {message_id: result})."""
    import agent_post
    on_pc = (ROOT_DIR / "agent_post.py").exists() and bool(os.environ.get("AGENTNET_USER"))
    s = agent_post.status() if on_pc else (blast_remote_status(db).get("agent_post") or {})
    return agent_post, on_pc, s, agent_post.load_results(db)


def agent_post_busy(s):
    return s.get("state") == "running" and datetime.now().timestamp() - s.get("at", 0) < 600


def agent_post_progress(agent_post, on_pc):
    """While the PC works: "Saving drafts 2 of 5: Stonor 3 - Uploading photos…" + Stop."""
    @st.fragment(run_every="4s")
    def progress():
        s = agent_post.status() if on_pc else (blast_remote_status(db).get("agent_post") or {})
        if not agent_post_busy(s):
            st.rerun(scope="app")
        total = s.get("total") or 1
        st.progress(min(1.0, (s.get("done") or 0) / total),
                    text=f"⏳ On your PC ({s.get('done', 0)} of {total} done): {s.get('place') or ''} - a Chrome "
                         "window is filling AgentNet")
        if s.get("steps"):  # this listing, step by step
            st.markdown(agent_post_steps(s["steps"]))
        if st.button("⏹ Stop after this listing", key="ap-stop"):
            agent_post.stop() if on_pc else agent_post.request_stop(db)
            st.toast("Stopping after the listing it's on…")
    progress()


def agent_post_credits(mid, results, rows):
    """Universal Credits to post this listing: what AgentNet showed for its draft, else what it usually charges -
    2 for a rental, 6 for a sale."""
    known = (results.get(mid) or {}).get("credits")
    if known:
        return known
    return 2 if (rows.get(mid) or {}).get("listing_type") == "Rent" else 6


def agent_post_actions(ids, key, results, rows=None):
    """The action bar for ticked listings: save as drafts (preview, no credits) / post now - and post the ones that
    already have a draft. Posting needs a tick that says what it costs."""
    agent_post, on_pc, s, _ = agent_post_state()
    if agent_post_busy(s):
        agent_post_progress(agent_post, on_pc)
        return
    rows = rows or {}
    # posted, or Post now pressed but unconfirmed ("check") - never posted (paid) twice
    posted = [i for i in ids if (results.get(i) or {}).get("state") in ("posted", "check")]
    todo = [i for i in ids if i not in posted]
    no_draft = [i for i in todo if not (results.get(i) or {}).get("draft_id")]
    if posted:
        st.caption(f"{len(posted)} of the ticked listing(s) already posted (or to check in AgentNet) - left out.")
    if not todo:
        return

    def go(which, action):
        agent_post.start(which, action) if on_pc else agent_post.request(db, which, action)
        st.session_state.pop(f"{key}-sure", None)
        st.rerun()

    plural = lambda n: "s" * (n > 1)  # noqa: E731
    credits = sum(agent_post_credits(i, results, rows) for i in todo)
    c1, c2, c3 = st.columns([1.2, 1.5, 1.3])
    if c1.button(f"📝 Save {len(no_draft)} as draft{plural(len(no_draft))} (preview)" if no_draft else
                 "📝 Already saved as draft", key=f"{key}-draft", disabled=not no_draft,
                 help="Fills AgentNet's Create Listing for each with your usual choices and saves it as a draft - "
                      "nothing is posted, no credits used. Check them, then post. Listings that already have a "
                      "draft are left out."):
        go(no_draft, "draft")
    sure = c2.checkbox(f"I checked - post {len(todo)} listing{plural(len(todo))} ({credits} Universal Credits)",
                       key=f"{key}-sure", help="2 credits for a rental, 6 for a sale (what AgentNet charges)")
    if c3.button(f"🚀 Post {len(todo)} now", key=f"{key}-post", type="primary", disabled=not sure,
                 help="A listing with a draft posts that draft (what you checked); the others are filled in and "
                      "posted straight away"):
        go(todo, "post")


def agent_post_box(r, results):
    """This listing on PropertyGuru: its draft / post (made on the PropertyGuru posting page or with the ticks)."""
    done = results.get(r["message_id"]) or {}
    if not done:
        return
    with st.expander("📤 On PropertyGuru (AgentNet)", expanded=True):
        agent_post_detail(done)


def agent_post_steps(steps):
    """The robot's steps for one listing -> a checklist: ✅ done (what it filled in) / ⏳ working / ❌ stopped (why)."""
    icon = {"done": "✅", "doing": "⏳", "failed": "❌"}
    return "\n".join(f"{i}. {icon.get(x.get('state'), '•')} **{x['step']}**"
                     + (f" — {x['detail']}" if x.get("detail") and x.get("state") != "failed" else "")
                     + (f" — :red[{x['error']}]" if x.get("error") else "")
                     for i, x in enumerate(steps, 1))


def agent_post_fields(pl, done=None, credits=None):
    """What goes (or went) into AgentNet's form, as a two-column table you can check at a glance."""
    done = done or {}
    furnish = pl.get("furnishing_words") or ""
    items = {"AIRCO": "air-con", "BED": "bed", "FRI": "fridge", "HEATE": "water heater"}
    if pl.get("furnishing_items"):
        furnish += " (" + ", ".join(items.get(k, k) for k in pl["furnishing_items"]) + ")"
    rooms = f"{pl.get('beds')} bed, {pl.get('baths')} bath" if pl.get("beds") else "-"
    size = (f"{pl['sqft']:,} sf" + (" (estimated from PropertyGuru)" if pl.get("sqft_estimated") else "")
            if pl.get("sqft") else "-")
    price = f"RM {pl['price']:,}" + ("/month" if pl.get("rent") else "") if pl.get("price") else "-"
    facts = [
        ("For", f"{'Rent - entire unit' if pl.get('rent') else 'Sale'} · available immediately"),
        ("Building", (done.get("property") or pl.get("name") or "-").replace("  ", " · ")),
        ("Property type", done.get("types") or " / ".join(x for x in (pl.get("sub_type"), pl.get("unit_type")) if x)),
        ("Title", pl.get("title")),
        ("Rooms", rooms), ("Size", size), ("Furnishing", furnish or "-"), ("Price", price),
        ("Tenant pays", f"RM {pl['rental']:,}/month" if pl.get("rental") else None),
        ("Photos", f"{done['photos']} of the owner's photos (no videos, no screenshots)" if done.get("photos")
         else None),
        ("Cost to post", f"{credits} Universal Credits · PropertyGuru + iProperty · 60 days" if credits else None)]
    st.markdown("\n".join(f"- **{k}:** {v}" for k, v in facts if v))


def agent_post_photos(paths):
    """The owner's photos the robot put in, small."""
    urls = signed_urls([p for p in paths or [] if p])
    if urls:
        cols = st.columns(len(urls))
        for col, p in zip(cols, urls):
            col.image(urls[p], width="stretch")


def agent_post_detail(done):
    word = {"posted": "✅ Posted", "draft": "📝 Draft saved (not posted yet)", "check": "⚠️ Check in AgentNet",
            "error": "❌ Couldn't make it"}.get(done.get("state"), done.get("state"))
    when = datetime.fromtimestamp(done.get("posted_at") or done.get("at") or 0, MYT).strftime("%d %b %Y %H:%M")
    ident = f" · AgentNet ID {done['draft_id']}" if done.get("draft_id") else ""
    link = f" · [Open it in AgentNet ↗]({done['edit_url']})" if done.get("edit_url") else ""
    st.markdown(f"**{word}**{ident}{link} · {when}")
    if done.get("error"):
        st.error(f"❌ {done['error']}")
    if done.get("note"):
        st.warning(done["note"])
    if done.get("steps"):
        with st.expander("Step by step", expanded=bool(done.get("error"))):
            st.markdown(agent_post_steps(done["steps"]))
            if done.get("error_shot"):
                url = signed_urls([done["error_shot"]]).get(done["error_shot"])
                if url:
                    st.image(url, caption="What AgentNet showed when it stopped", width=520)
    if done.get("headline"):
        st.markdown(f"**Headline:** {done['headline']}")
        st.text(done.get("description", ""))
    if done.get("shot"):
        url = signed_urls([done["shot"]]).get(done["shot"])
        if url:
            st.image(url, caption="AgentNet preview", width=520)


def agent_post_photo_picker(mid, row, prefs, key):
    """All the owner's photos: tick the ones for the ad (at first the robot's usual 5 are ticked). -> the photos
    that go in, in the listing's order."""
    import agent_post
    every = [p for p in row.get("photo_paths") or [] if p.lower().endswith(".jpg")]
    mine = prefs.get(mid) or {}
    if mine.get("photos"):
        chosen = [p for p in every if p in set(mine["photos"])]
    else:  # nothing ticked yet: the usual first 5 (minus any left out before)
        chosen = [p for p in every if p not in set(mine.get("skip_photos") or [])][:5]
    head, clear = st.columns([4, 1])
    head.markdown(f"**Photos** - {len(every)} from the owner · tick ✔ the ones for the ad ({len(chosen)} ticked)")
    if clear.button("Clear ticks", key=f"{key}-clear", disabled=not chosen):
        agent_post.save_prefs(db, mid, photos=["-"], skip_photos=[])  # "-" = you cleared them all
        for p in every:
            st.session_state.pop(f"{key}-use-{p}", None)
        st.rerun()
    urls = signed_urls(every)
    ticked = []
    for start in range(0, len(every), 6):
        cols = st.columns(6)
        for col, p in zip(cols, every[start:start + 6]):
            with col:
                if urls.get(p):
                    st.image(urls[p], width="stretch")
                if st.checkbox("✔ use", value=p in chosen, key=f"{key}-use-{p}"):
                    ticked.append(p)
    if ticked != chosen:
        agent_post.save_prefs(db, mid, photos=ticked or ["-"], skip_photos=[])
        st.rerun()
    if len(chosen) < 5:
        st.error(f"{len(chosen)} photo(s) ticked - PropertyGuru needs at least 5.")
    return chosen


def agent_post_card(mid, done, row, results, rows, prefs):
    """One draft to approve: everything the robot filled in, your choice of photos, the headline / description (you
    can reword them), AgentNet's preview - then Approve & post (after a tick) or Redo with your changes."""
    import agent_post
    _, on_pc, s, _ = agent_post_state()
    credits = agent_post_credits(mid, results, rows)
    mine = prefs.get(mid) or {}
    changed = (row.get("scanned_at") and pd.to_datetime(row["scanned_at"], utc=True).timestamp()
               > (done.get("at") or 0) + 60)
    with st.container(border=True):
        st.markdown(f"### {row.get('place')} · {row.get('listing_type')} · "
                    f"RM {(row.get('price_rm') or 0):,}" + (f"  \n<small>Owner: {row.get('owner_name')}</small>"
                                                           if row.get("owner_name") else ""),
                    unsafe_allow_html=True)
        made = datetime.fromtimestamp(done.get("at") or 0, MYT).strftime("%d %b %H:%M")
        st.caption(f"Draft saved {made} · AgentNet ID {done.get('draft_id')}"
                   + (f" · [open it in AgentNet ↗]({done['edit_url']})" if done.get("edit_url") else ""))
        left, right = st.columns([1.1, 1])
        with left:
            agent_post_fields(done.get("plan") or {}, done, credits)
            head = st.text_input("Headline", mine.get("headline") or done.get("headline", ""), max_chars=70,
                                 key=f"ap-head-{mid}")
            desc = st.text_area("Description", mine.get("description") or done.get("description", ""), height=190,
                                key=f"ap-desc-{mid}", help="AgentNet wants at least 20 words")
            if (head, desc) != (mine.get("headline") or done.get("headline", ""),
                                mine.get("description") or done.get("description", "")):
                if st.button("💾 Keep this wording", key=f"ap-word-{mid}"):
                    agent_post.save_prefs(db, mid, headline=head.strip(), description=desc.strip())
                    st.rerun()
        with right:
            if done.get("shot"):
                url = signed_urls([done["shot"]]).get(done["shot"])
                if url:
                    st.image(url, caption="How AgentNet shows it now (preview)", width="stretch")
        used = agent_post_photo_picker(mid, row, prefs, f"card-{mid}")
        # what you changed here that the draft in AgentNet doesn't have yet
        in_draft = done.get("photo_paths") or []
        pending = [x for x, yes in (
            ("photos", bool(in_draft) and used != in_draft),
            ("headline", bool(mine.get("headline")) and mine["headline"] != done.get("headline")),
            ("description", bool(mine.get("description")) and mine["description"] != done.get("description")),
            ("listing details", changed)) if yes]
        if pending:
            st.info(f"✏️ Your new {', '.join(pending)} aren't in AgentNet's draft yet - tick below and **✅ Redo & "
                    "post** makes the ad again with them and posts it in one go (or **🔄 Redo draft** to see the "
                    "new draft first).")
        if done.get("steps"):
            with st.expander("Step by step (what the robot did)"):
                st.markdown(agent_post_steps(done["steps"]))
        if agent_post_busy(s):
            return
        c1, c2, c3 = st.columns([1.6, 1, 1])
        # changed something? one go: the ad made again with your changes (photos, wording, details) and posted
        sure = c1.checkbox(f"I checked it - {'redo with my changes and ' if pending else ''}post for {credits} "
                           "Universal Credits", key=f"ap-ok-{mid}")
        if c2.button("✅ Redo & post" if pending else "✅ Approve & post", key=f"ap-go-{mid}", type="primary",
                     disabled=not sure):
            action = "redo_post" if pending else "post"
            agent_post.start([mid], action) if on_pc else agent_post.request(db, [mid], action)
            st.rerun()
        if c3.button("🔄 Redo draft", key=f"ap-redo-{mid}", type="primary" if pending else "secondary",
                     help="Makes the draft again with the photos and wording above (the old draft stays in "
                          "AgentNet's Draft Listings - delete it there if you like)"):
            agent_post.start([mid], "redo") if on_pc else agent_post.request(db, [mid], "redo")
            st.rerun()


def agent_post_preview(row, market, prefs):
    """Before any draft: what the robot will fill in for this saved listing - and pick its photos."""
    import agent_post
    mid = row["message_id"]
    pl = agent_post.plan(row, market.get(mid))
    with st.container(border=True):
        st.markdown(f"**{row.get('place')}** · {row.get('owner_name') or ''}")
        agent_post_fields(pl, credits=2 if pl.get("rent") else 6)
        st.caption(f"Headline (roughly - the area comes from PropertyGuru's search): "
                   f"{(prefs.get(mid) or {}).get('headline') or agent_post.headline(pl, '')}")
        agent_post_photo_picker(mid, row, prefs, f"pre-{mid}")


# ---------- PropertyGuru posting ----------
def propertyguru_posting_page():
    """Everything on PropertyGuru through AgentNet: drafts to approve, posted, problems, and saved listings not on
    PropertyGuru yet (with what will be filled in)."""
    st.title("PropertyGuru posting")
    st.caption("Your saved listings on PropertyGuru through AgentNet (your agent account). Save listings as drafts "
               "first (no credits), check each draft below, then approve. Runs on your PC: a Chrome window fills "
               "AgentNet one listing after another (about 2 minutes each). Posting costs 2 Universal Credits for a "
               "rental, 6 for a sale - on PropertyGuru + iProperty for 60 days.")
    agent_post, on_pc, s, results = agent_post_state()
    if agent_post_busy(s):
        agent_post_progress(agent_post, on_pc)
    elif s.get("state") == "done" and s.get("total"):
        st.success(f"Last run: {s.get('ok', 0)} done, {s.get('failed', 0)} couldn't"
                   + (f" · {s['credits']} Universal Credits left" if s.get("credits") else "")
                   + (" · stopped early" if s.get("stopped") else ""))
    elif s.get("state") == "error":
        st.error(f"The last run stopped: {s.get('error')}")
    try:
        rows = {r["message_id"]: r for r in load_owner_listings(db)}
    except Exception as e:
        st.warning(f"Couldn't load saved listings ({str(e).splitlines()[0]}).")
        return
    import market_check
    market = load_market_check(db)
    prefs = agent_post.load_prefs(db)  # your photo / wording choices
    mine = {mid: d for mid, d in results.items() if mid in rows}
    drafts = sorted(((m, d) for m, d in mine.items() if d.get("state") == "draft"), key=lambda x: -(x[1].get("at") or 0))
    posted = sorted(((m, d) for m, d in mine.items() if d.get("state") in ("posted", "check")),
                    key=lambda x: -(x[1].get("posted_at") or x[1].get("at") or 0))
    problems = [(m, d) for m, d in mine.items() if d.get("state") == "error"]

    # ----- drafts to approve -----
    st.subheader(f"📝 Drafts waiting for your OK ({len(drafts)})")
    if not drafts:
        st.caption("No drafts - tick listings at the bottom and press **Save as drafts**.")
    for mid, d in drafts:
        agent_post_card(mid, d, rows[mid], results, rows, prefs)
    if len(drafts) > 1:
        with st.container(border=True):
            st.markdown("**All drafts at once**")
            agent_post_actions([m for m, _ in drafts], "ap-all-drafts", results, rows)

    # ----- posted -----
    st.subheader(f"✅ Posted ({len(posted)})")
    if posted:
        st.dataframe(pd.DataFrame([{
            "Status": "✅ Posted" if d.get("state") == "posted" else "⚠️ Check in AgentNet",
            "Place": rows[m].get("place"), "Rent / Sale": rows[m].get("listing_type"),
            "Price (RM)": rows[m].get("price_rm"), "Headline": d.get("headline"),
            "Posted": datetime.fromtimestamp(d.get("posted_at") or d.get("at") or 0, MYT).strftime("%d %b %H:%M"),
            "AgentNet": d.get("live_url") or d.get("edit_url")} for m, d in posted]),
            hide_index=True, width="stretch",
            column_config={"Price (RM)": st.column_config.NumberColumn(format="%,d"),
                           "AgentNet": st.column_config.LinkColumn(display_text="Open ↗")})
    else:
        st.caption("Nothing posted through the robot yet.")

    # ----- problems -----
    if problems:
        st.subheader(f"❌ Couldn't make ({len(problems)})")
        for mid, d in problems:
            with st.expander(f"{rows[mid].get('place')} - {d.get('error', '')[:90]}"):
                agent_post_detail(d)

    # ----- saved listings not on PropertyGuru yet -----
    st.subheader("🏠 Saved listings not on PropertyGuru yet")
    waiting = sorted((r for mid, r in rows.items() if mid not in mine or mine[mid].get("state") == "error"),
                     key=lambda r: r.get("posted_at") or "", reverse=True)
    if not waiting:
        st.caption("All saved listings have a draft or are posted.")
        return
    table = pd.DataFrame([{
        "Place": r.get("place"), "Owner": r.get("owner_name"), "Rent / Sale": r.get("listing_type"),
        "Price (RM)": r.get("price_rm"), "sf": r.get("sqft"), "Furnish": r.get("furnishing"),
        "Photos": sum(1 for p in r.get("photo_paths") or [] if p.lower().endswith(".jpg")),
        "Posted in chat": pd.to_datetime(r.get("posted_at"), utc=True).tz_convert(MYT).strftime("%d %b"),
    } for r in waiting])
    picked = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
                          key="ap-waiting", height=min(420, 38 + 35 * len(table)),
                          column_config={"Price (RM)": st.column_config.NumberColumn(format="%,d"),
                                         "Photos": st.column_config.NumberColumn(
                                             help="PropertyGuru needs at least 5 photos")})
    sel = [waiting[i]["message_id"] for i in picked.selection.rows if i < len(waiting)]
    if not sel:
        st.caption("☝️ Tick listings to see what will be filled in, then save them as drafts.")
        return
    st.markdown("**What the robot will fill in**")
    for mid in sel[:6]:
        agent_post_preview(rows[mid], market, prefs)
    if len(sel) > 6:
        st.caption(f"…and {len(sel) - 6} more.")
    agent_post_actions(sel, "ap-wait-act", results, rows)


def edit_listings_table(shown):
    """Saved listings, editable: Unit number, Type, Rent price, Sale price, Furnish (Fully furnished / Partial /
    Bare). Save writes the changed listings (listing_edits.py)."""
    import listing_edits
    base = pd.DataFrame([{
        "Date": r["_date"].strftime("%d %b"), "Owner": r.get("owner_name"), "Place": r.get("place"),
        "Unit number": r.get("unit_no") or "",
        "Type": r["_category"] if r["_category"] in listing_edits.TYPES else None,
        "Rent Price (RM)": r.get("price_rm") if r.get("listing_type") == "Rent" else r.get("rental_rm"),
        "Sale Price (RM)": r.get("price_rm") if r.get("listing_type") != "Rent" else None,
        "Furnish": listing_edits.furnish_choice(r.get("furnishing")),
    } for r in shown])
    st.caption("Change the white columns, then press **Save changes**. Sale price filled = for sale (the rent then "
               "means what the tenant pays); only a rent = for rent. A changed listing is compared with PropertyGuru "
               "again by itself.")
    edited = st.data_editor(
        base, hide_index=True, width="stretch", height=480, key="sl-editor",
        disabled=["Date", "Owner", "Place"],
        column_config={
            "Type": st.column_config.SelectboxColumn(options=listing_edits.TYPES),
            "Rent Price (RM)": st.column_config.NumberColumn(min_value=0, step=100, format="%,d"),
            "Sale Price (RM)": st.column_config.NumberColumn(min_value=0, step=1000, format="%,d"),
            "Furnish": st.column_config.SelectboxColumn(options=list(listing_edits.FURNISH))})
    changed = [i for i in range(len(base)) if not base.iloc[i].equals(edited.iloc[i])]
    if st.button(f"💾 Save changes ({len(changed)})", type="primary", disabled=not changed, key="sl-save"):
        saved = 0
        for i in changed:
            e = edited.iloc[i]
            num = lambda v: None if pd.isna(v) or not v else v  # noqa: E731
            saved += bool(listing_edits.save(db, shown[i], listing_edits.row_changes(
                shown[i], unit=(e["Unit number"] or "").strip(), ptype=e["Type"], rent=num(e["Rent Price (RM)"]),
                sale=num(e["Sale Price (RM)"]), furnish=e["Furnish"])))
        load_owner_listings.clear()
        st.session_state.pop("sl-editor", None)
        st.toast(f"Saved {saved} listing{'s' * (saved != 1)}")
        st.rerun()


def saved_listings_tab():
    from wa_export import describe, from_row, listing_zip

    st.caption("Everything saved from your WhatsApp scans (stored online, so this tab also works on the online "
               "app). Click a row to see the full description and download it with its pictures.")
    try:
        rows = load_owner_listings(db)
    except Exception as e:
        st.warning(f"Couldn't load owner listings ({str(e).splitlines()[0]}). If this is the first time, run "
                   "schema_owner_listings.sql once in the Supabase SQL Editor, then scan on the WhatsApp listing page.")
        return
    if not rows:
        st.info("Nothing saved yet - scan some chats on the WhatsApp listing page.")
        return

    # Simple property type (Condo, Shop, Land, ...) from the stated type / place / message + your Condo prices list
    from wa_parser import property_category
    try:
        condos = [c.get("condo") for c in load_condo_summary(db).get("rows") or []]
    except Exception:
        condos = []
    for r in rows:
        r["_category"] = property_category(r.get("property_type"), r.get("place"), r.get("message_text"), condos)

    for r in rows:  # the day it was posted in the chat (Malaysia time)
        r["_date"] = pd.to_datetime(r.get("posted_at"), utc=True).tz_convert(MYT).date()
    f1, f2, f3, f4, f5 = st.columns([1, 1.5, 1.8, 1.7, 1.5])
    kind = f1.radio("Rent / Sale", ["All", "Rent", "Sale"], horizontal=True)
    days = sorted(r["_date"] for r in rows)
    picked_days = f5.date_input("Date", (days[0], days[-1]), min_value=days[0], max_value=days[-1],
                                format="DD/MM/YYYY", key="sl-dates",
                                help="Posted in the chat between these days - pick one day twice for just that day")
    d_from, d_to = (tuple(picked_days) * 2)[:2] if picked_days else (days[0], days[-1])
    cats = sorted({r["_category"] for r in rows}, key=lambda c: (c == "Unknown", c))
    pick_cats = f2.multiselect("Property type", cats, placeholder="All types",
                               help="Condo, shop, office, factory, land… worked out from the listing's own type, its "
                                    "name and message, and the condo names in Condo prices.")
    text = f3.text_input("Search location / phone / message")
    prices = [r["price_rm"] for r in rows if r.get("price_rm")]
    lo, hi = (min(prices), max(prices)) if prices else (0, 1)
    pr = f4.slider("Price (RM)", lo, max(hi, lo + 1), (lo, max(hi, lo + 1)))
    shown = [r for r in rows
             if (kind == "All" or r.get("listing_type") == kind)
             and (not pick_cats or r["_category"] in pick_cats)
             and (not r.get("price_rm") or pr[0] <= r["price_rm"] <= pr[1])
             and d_from <= r["_date"] <= d_to
             and (not text or text.lower() in " ".join(str(r.get(k) or "") for k in
                                                       ("place", "phones", "sender_phone", "message_text")).lower())]
    shown.sort(key=lambda r: (r.get("posted_at") or "", r["message_id"]), reverse=True)  # newest on top
    st.caption(f"{len(shown):,} of {len(rows):,} listings · newest first")

    # ----- compare with PropertyGuru (runs on the PC: market_check.py) -----
    import market_check
    market = load_market_check(db)
    on_pc = (ROOT_DIR / "market_check.py").exists() and bool(os.environ.get("SUPABASE_SECRET_KEY"))
    mstat = market_check.status() if on_pc else (blast_remote_status(db).get("market_check") or {})
    m0, m1, m2 = st.columns([1, 1, 2])
    not_yet = [r for r in shown if r["message_id"] not in market]  # never compared

    def compare(which):
        ids = [r["message_id"] for r in which]
        if on_pc:
            market_check.start(ids)
        else:
            market_check.request(db, ids)
            st.session_state.market_asked = True
        st.rerun()

    if mstat.get("state") == "running" and datetime.now().timestamp() - mstat.get("at", 0) < 600:
        @st.fragment(run_every="5s")
        def market_progress():
            s = market_check.status() if on_pc else (blast_remote_status(db).get("market_check") or {})
            if s.get("state") != "running":
                load_market_check.clear()
                st.rerun(scope="app")
            st.info(f"🔍 Comparing with PropertyGuru & iProperty on your PC… {s.get('done', 0)} of {s.get('total', '?')}: "
                    f"{s.get('text', '')}")
        with m2:
            market_progress()
    else:
        how = ("For each listing: finds it on PropertyGuru and iProperty, reads the current rent / sale prices and "
               "sizes there, and says whether the owner's price is OK to post. Runs on your PC (a Chrome window "
               "opens) - about 20 seconds per property. New and changed listings are also compared by themselves "
               "every few minutes when the PC is on.")
        if m0.button(f"🔍 Compare new ones ({len(not_yet)})", disabled=not not_yet, type="primary",
                     help="Only the listings shown that were never compared. " + how):
            compare(not_yet)
        if m1.button(f"🔄 Compare all ({len(shown)})", disabled=not shown,
                     help="Every listing shown again, also ones compared before (prices on PropertyGuru change). "
                          + how):
            compare(shown)
    if st.session_state.get("market_asked") and not on_pc:
        m2.caption("⏳ Sent to your PC - results appear here within a few minutes (if the PC is on).")

    # what the posting robot made for each (agent_post.py)
    import agent_post
    ap_results = agent_post.load_results(db)
    ap_word = {"posted": "✅ Posted", "draft": "📝 Draft", "check": "⚠️ Check", "error": "❌ Problem"}
    # Left: the owner's listing | right: PropertyGuru & iProperty
    table = pd.DataFrame([{
        "Date": r["_date"],
        "Owner": r.get("owner_name"),
        "Number": (r.get("phones") or "").replace("; ", ", ") or r.get("sender_phone"),
        "Place": r.get("place"),
        "Unit number": r.get("unit_no"),
        "Type": r["_category"],
        # a sale that comes with a tenant also shows its rental
        "Rent Price (RM)": r.get("price_rm") if r.get("listing_type") == "Rent" else r.get("rental_rm"),
        "Sale Price (RM)": r.get("price_rm") if r.get("listing_type") != "Rent" else None,
        # the owner's size, else the usual size of such units there online (estimated, marked "est.")
        "sf": f"{r['sqft']:,}" if r.get("sqft") else (f"~{market[r['message_id']]['est_sqft']:,} est."
                                                     if (market.get(r["message_id"]) or {}).get("est_sqft") else None),
        "Rent / Sale": r.get("listing_type"),
        "Furnish": r.get("furnishing"),
        "PG + iProperty": (market.get(r["message_id"]) or {}).get("label", ""),
        "Why": (market.get(r["message_id"]) or {}).get("why") or (market.get(r["message_id"]) or {}).get("reason"),
        "Lowest sale": market_check.lowest_real(market.get(r["message_id"]) or {}, "sale"),
        "Lowest rent": market_check.lowest_real(market.get(r["message_id"]) or {}, "rent"),
        "Already listed?": (market.get(r["message_id"]) or {}).get("same_unit_url"),
        "On PropertyGuru": ap_word.get((ap_results.get(r["message_id"]) or {}).get("state"), ""),
    } for r in shown])
    # ----- edit mode: correct unit number, type, prices, furnishing right in the table -----
    if st.toggle("✏️ Edit", key="sl-edit", help="Correct unit number, type, rent / sale price and furnishing. "
                                                "Your corrections stay even when the chat is scanned again."):
        edit_listings_table(shown)
        return
    picked = st.dataframe(table, hide_index=True, width="stretch", height=480, on_select="rerun",
                          selection_mode="multi-row", key="saved_listings_table",
                          column_config={"Date": st.column_config.DateColumn(
                                             format="DD MMM YYYY", help="Posted in the chat"),
                                         "Place": st.column_config.TextColumn(width="medium"),
                                         "Rent Price (RM)": st.column_config.NumberColumn(
                                             format="%,d", help="Monthly rent - for a sale, the rental it comes "
                                                                "with (sold tenanted)"),
                                         "Sale Price (RM)": st.column_config.NumberColumn(format="%,d"),
                                         "sf": st.column_config.TextColumn(
                                             help="\"est.\" = the owner didn't say - the usual size of units with "
                                                  "the same rooms there on PropertyGuru / iProperty"),
                                         "PG + iProperty": st.column_config.TextColumn(
                                             help="Owner's price compared with PropertyGuru and iProperty now"),
                                         "Why": st.column_config.TextColumn(width="large"),
                                         "Lowest sale": st.column_config.NumberColumn(
                                             format="%,d", help="Cheapest real one for sale on PropertyGuru / "
                                                                "iProperty now - same size when listed (lelong / "
                                                                "bait left out)"),
                                         "Lowest rent": st.column_config.NumberColumn(
                                             format="%,d", help="Cheapest real one for rent on PropertyGuru / "
                                                                "iProperty now - same size when listed (lelong / "
                                                                "bait left out)"),
                                         "Already listed?": st.column_config.LinkColumn(
                                             display_text="👀 Open", help="Same price online - probably this unit, "
                                                                          "already advertised")})
    sel = [i for i in picked.selection.rows if i < len(shown)]
    if not sel:
        st.caption("👆 Tick a row for its description and pictures - tick several to save them as PropertyGuru "
                   "drafts or post them together.")
        return
    # ticked listings -> PropertyGuru: save as drafts (preview) / post (see also the PropertyGuru posting page)
    st.markdown(f"**{len(sel)} ticked** → PropertyGuru (AgentNet)")
    agent_post_actions([shown[i]["message_id"] for i in sel], "sl-ap", ap_results,
                       {r["message_id"]: r for r in shown})
    if len(sel) > 1:
        st.caption("Tick just one row to see its description and pictures.")
        return

    r = shown[sel[0]]
    listing = from_row(r)
    st.subheader(f"{r.get('listing_type') or ''} · {r['_category']} · {r.get('place') or 'Listing'} · "
                 f"{'RM ' + format(r['price_rm'], ',') if r.get('price_rm') else ''}")
    if r.get("property_type"):
        st.caption(f"Type as written: {r['property_type'][:200]}")
    mk = market.get(r["message_id"])
    if mk:  # the verdict, full width
        box = {"good": st.success, "ok": st.warning, "high": st.error}.get(mk.get("verdict"), st.info)
        when = datetime.fromtimestamp(mk.get("checked_at", 0), MYT).strftime("%d %b %Y")
        links = " · ".join(x for x in (
            f"[See {mk.get('property')} on PropertyGuru]({mk['property_url']})" if mk.get("property_url") else "",
            f"[on iProperty]({mk['ip_url']})" if mk.get("ip_url") else "",
            f"[👀 Already listed? (same price)]({mk['same_unit_url']})" if mk.get("same_unit_url") else "") if x)
        sites = "PropertyGuru & iProperty" if mk.get("sites") else "PropertyGuru (compare again to add iProperty)"
        box(f"**📊 Compared with {sites}: {mk.get('label')}**\n\n{mk.get('reason')}\n\n{links}  \n"
            f"_Checked {when}_")
    agent_post_box(r, ap_results)
    name = re.sub(r"[^A-Za-z0-9 ]+", "", r.get("place") or "listing").strip() or "listing"
    paths = r.get("photo_paths") or []
    left, right = st.columns(2, gap="large")  # half owner, half PropertyGuru & iProperty

    # ----- left: the owner's listing -----
    with left:
        st.markdown("### 🏠 Owner's listing")
        rent = r.get("listing_type") == "Rent"
        money = (f"RM {r['price_rm']:,}" + ("/mo" if rent else "")) if r.get("price_rm") else "-"
        est = (mk or {}).get("est_note")  # owner left out the size / rooms: what's usual there online
        rooms = " / ".join(x for x in (f"{r['bedrooms']} bed" if r.get("bedrooms") else "",
                                       f"{r['bathrooms']} bath" if r.get("bathrooms") else "") if x)
        facts = [("Rent / Sale", r.get("listing_type")), ("Price", money),
                 ("Size", f"{r['sqft']:,} sf" if r.get("sqft") else
                  f"~{mk['est_sqft']:,} sf (estimated: {est})" if (mk or {}).get("est_sqft") else None),
                 ("Property type", r["_category"] + (f" ({r['property_type'][:60]})" if r.get("property_type") else "")),
                 ("Rooms", rooms or (f"estimated: {est}" if (mk or {}).get("est_beds") is not None else "")),
                 ("Furnishing", r.get("furnishing")), ("Unit", r.get("unit_no")),
                 ("Rental (if sold tenanted)", f"RM {r['rental_rm']:,}/mo" if r.get("rental_rm") and not rent else None),
                 ("Owner", r.get("owner_name")),
                 ("Number", (r.get("phones") or "").replace("; ", ", ") or r.get("sender_phone")),
                 ("Posted", pd.to_datetime(r.get("posted_at"), utc=True).tz_convert(MYT).strftime("%d %b %Y"))]
        st.markdown("\n".join(f"- **{k}:** {v}" for k, v in facts if v))
        hide = st.checkbox("Without phone numbers (to share)", key="owner_hide_phones",
                           help="For sending to other agents / clients: no owner or sender number, and any phone "
                                "number in the message shows as [number hidden]")
        tag = " (no numbers)" if hide else ""
        b1, b2 = st.columns(2)
        b1.download_button(f"Download description{tag} (.txt)", describe(listing, hide).encode("utf-8-sig"),
                           f"{name}{tag}.txt", "text/plain", on_click="ignore")
        if paths and b2.button(f"Prepare ZIP{tag} ({len(paths)} files)", key=f"zip-{r['message_id']}"):
            with st.spinner("Collecting pictures…"):
                photos = []
                for p in paths:
                    try:
                        photos.append((p, db.storage.from_(PHOTO_BUCKET).download(p)))
                    except Exception:
                        pass
            st.session_state.owner_zip = (r["message_id"], listing_zip(listing, photos, hide), f"{name}{tag}.zip",
                                          hide)
        oz = st.session_state.get("owner_zip")
        if oz and oz[0] == r["message_id"] and oz[3] == hide:  # the ZIP ready matches the tick box
            b2.download_button(f"Download {oz[2]}", oz[1], oz[2], "application/zip", type="primary",
                               on_click="ignore")
        with st.expander("Owner's full description", expanded=True):
            st.text(describe(listing))

    # ----- right: what's on PropertyGuru & iProperty now - the 5 cheapest for sale and for rent, real or fake -----
    with right:
        st.markdown("### 🔎 PropertyGuru & iProperty now")
        if not mk:
            st.caption("Not compared yet - press **Compare with PropertyGuru & iProperty** above.")
        else:
            where = f"in **{mk.get('property')}** (area)" if mk.get("area") else \
                f"at **{mk.get('property')}**" if mk.get("property") else ""
            st.caption(f"Cheapest listings {where} on both sites (an ad on both counts once) - each one's own "
                       "description checked: ✅ real · ⚠️ check · ❌ fake (lelong, \"starting from\", a different "
                       "price in its description)")
            real_word = {"real": "✅ Real", "check": "⚠️ Check", "fake": "❌ Fake"}
            site_word = {"pg": "PropertyGuru", "ip": "iProperty", "both": "Both"}
            for k, title in (("sale", "🏷️ 5 cheapest for sale"), ("rent", "🔑 5 cheapest for rent")):
                units = mk.get(f"{k}_lowest")
                size_note = ("sizes vary" if not mk.get(f"{k}_same_size") else
                             f"same size as the owner's (±20% of {mk['owner_sqft']:,} sf)" if mk.get("owner_sqft")
                             else "same rooms as the owner's")
                st.markdown(f"**{title}** <small>· {size_note}</small>", unsafe_allow_html=True)
                if units is None:
                    st.caption("Compare again to see this.")
                    continue
                if not units:
                    st.caption("None listed on PropertyGuru or iProperty right now.")
                    continue
                st.dataframe(pd.DataFrame([{
                    "Real?": real_word.get((u.get("check") or {}).get("status"), ""),
                    "Price (RM)": u["price"], "Sqft": u.get("sqft"), "Beds": u.get("beds"),
                    "Site": site_word.get(u.get("site") or "pg", ""),
                    "Why": (u.get("check") or {}).get("why", ""),
                    "Listing": u.get("url")} for u in units]), hide_index=True, width="stretch",
                    column_config={"Price (RM)": st.column_config.NumberColumn(format="%,d"),
                                   "Sqft": st.column_config.NumberColumn(format="%,d"),
                                   "Why": st.column_config.TextColumn(width="large",
                                                                      help="Quoted from the listing's own description"),
                                   "Listing": st.column_config.LinkColumn("Listing", display_text="Open ↗")})
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

    try:
        import neonize  # noqa: F401 - only installed on the PC
    except ImportError:
        return scan_chats_remote()  # online dashboard: the PC scans, asked through Supabase
    import wa_client
    from phone_filter import extract_from_text
    from wa_export import make_zip

    st.caption("Link your WhatsApp by QR code, pick chats (individual or groups), and turn property posts into "
               "listings (type, price, sqft, place, furnishing, phone numbers) with their photos. Scanning is "
               "read-only: nothing is sent from your WhatsApp except blasts you start on the WhatsApp blast "
               "page. Works on your PC only.")
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
    days = scan_days("pc")
    read_pictures = st.checkbox("Read WhatsApp screenshots and PDFs", value=True,
                                help="Screenshots of WhatsApp chats and PDF files are read on this PC (about 1-3 "
                                     "seconds each); ordinary photos are skipped but still attached. In chats where "
                                     "you put a '.' between owners, everything between two dots counts as one "
                                     "owner (one number, possibly several properties).")

    if not picked:
        st.caption("☝️ Pick at least one chat to scan.")
    if st.button("Scan messages", type="primary", disabled=not picked):
        import wa_scan
        bar = st.progress(0.0, text="Starting…")
        st.session_state.wa_result = wa_scan.scan(
            db, picked, days, read_pictures, st.session_state.email,
            progress=lambda text, frac=None: bar.progress(min(1.0, frac or 0.0), text=text))
        bar.empty()
        load_owner_listings.clear()
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


def scan_days(key):
    """Which messages to scan: Today (since midnight, Malaysia time) / the last few days / any number of days.
    -> days back (a fraction for Today)."""
    choice = st.radio("Messages from", ["Today", "Last 3 days", "Last 7 days", "Last 30 days", "Last 60 days",
                                        "Custom"], index=0, horizontal=True, key=f"scan-days-{key}",
                      help="A scan replaces what earlier scans saved for the same chats and days, so scanning "
                           "Today again is safe")
    if choice == "Today":
        now = datetime.now(MYT)
        return max((now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds(), 60) / 86400
    if choice == "Custom":
        return st.number_input("Days back", 1, 365, 14, key=f"scan-days-n-{key}")
    return int(choice.split()[1])


def scan_chats_remote():
    """Scan chats from the online dashboard: the chat list comes from the PC (copied up every minute); pressing
    Scan asks the PC to do it (wa_scan.py) and the listings appear in Saved listings."""
    import time

    import wa_scan
    from phone_filter import extract_from_text

    st.caption("Pick chats (individual or groups) and turn property posts into listings with their photos. Your PC "
               "does the scanning (it has your WhatsApp and reads the screenshots) - results appear in the "
               "**Saved listings** tab.")
    try:
        pc = wa_scan.remote_chats(db)
    except Exception as e:
        st.error(f"Couldn't reach Supabase: {e}")
        return
    age = time.time() - (pc.get("at") or 0)
    if not pc:
        st.warning("🖥️ Your PC hasn't sent its WhatsApp chats yet - make sure it's on, then reload in a minute.")
        return
    if age > 300:
        st.warning(f"🖥️ Your PC was last seen {age / 60:.0f} minutes ago - scans wait until it's back on.")
    if pc.get("status") not in ("ready", "syncing"):
        st.warning("WhatsApp isn't connected on your PC - open the dashboard on the PC and link it by QR code "
                   "(WhatsApp listing page) once.")
        return
    me = pc.get("me") or {}
    st.success(f"Connected on your PC as **{me.get('name') or '?'}** (+{me.get('number') or '?'}) · chat list "
               f"updated {max(0, age) / 60:.0f} min ago")

    # ----- a scan in progress / just finished -----
    stt = wa_scan.status_remote(db)
    waiting = [t for t in wa_scan.pending(db) if t["kind"] == "wa_scan"]
    if waiting or (stt.get("state") == "running" and time.time() - stt.get("at", 0) < 900):
        @st.fragment(run_every="5s")
        def scan_progress():
            s = wa_scan.status_remote(db)
            if s.get("state") == "running" and time.time() - s.get("at", 0) < 900:
                st.progress(min(1.0, s.get("frac") or 0.0), text=f"🖥️ Your PC is scanning: {s.get('text')}")
            elif [t for t in wa_scan.pending(db) if t["kind"] == "wa_scan"]:
                st.info("⏳ Sent to your PC - the scan starts within about 15 seconds.")
            else:
                load_owner_listings.clear()
                st.rerun(scope="app")
        scan_progress()
        return
    if stt.get("state") == "done" and time.time() - stt.get("at", 0) < 3600:
        saved = stt.get("saved")
        st.success(f"✅ Last scan: {stt.get('messages', 0):,} messages read, {stt.get('listings', 0):,} listings, "
                   f"{stt.get('photos', 0):,} photos" + (" - see the **Saved listings** tab." if saved is True
                                                         else f". Note: {saved}"))
    elif stt.get("state") == "error":
        st.error(f"Last scan stopped: {stt.get('error')}")

    chats = pc.get("chats") or []
    if not chats:
        st.info("No chats on the PC yet.")
        return
    label = {c["jid"]: f"{'👥' if c['is_group'] else '👤'} {c['name'] or ('+' + c['phone'] if c['phone'] else c['jid'])}"
                       f"{' (+' + c['phone'] + ')' if c['phone'] and c['name'] else ''} · {c['messages']} msgs"
             for c in chats}
    groups = [c["jid"] for c in chats if c["is_group"]]
    settings = pc.get("settings") or {}
    settings.setdefault("my_numbers", [])
    settings.setdefault("listing_groups", [])
    with st.expander("⚙️ My numbers & listing groups", expanded=not settings["listing_groups"]):
        nums_text = st.text_area("My own numbers - never recorded as an owner's number (one per line)",
                                 "\n".join(settings["my_numbers"]), height=90)
        my_groups = st.multiselect("My listing groups (where owner listings are posted)", groups,
                                   [g for g in settings["listing_groups"] if g in groups], format_func=label.get)
        if st.button("Save settings"):
            nums = list(dict.fromkeys(n for n, _, _ in extract_from_text(nums_text) if n))
            wa_scan.request(db, "wa_settings", {"my_numbers": nums, "listing_groups": my_groups})
            st.success("Sent to your PC - it's applied within about 15 seconds.")

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
                            key=f"rpick-{kind}")
    days = scan_days("online")
    read_pictures = st.checkbox("Read WhatsApp screenshots and PDFs", value=True,
                                help="Done on your PC (about 1-3 seconds each); ordinary photos are attached only.")
    if not picked:
        st.caption("☝️ Pick at least one chat to scan.")
    if st.button("Scan messages", type="primary", disabled=not picked):
        wa_scan.request(db, "wa_scan", {"chats": picked, "days": days, "read_pictures": read_pictures,
                                        "by": st.session_state.get("email") or "online dashboard"})
        st.rerun()


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

    for c in ("sale_min", "sale_max", "rent_min", "rent_max", "sqft_min", "sqft_max", "sale_listings", "rent_listings"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    # "2 - 5", "Studio - 3" -> lowest / highest room count (studio = 0)
    beds = df.bedrooms.fillna("").astype(str).str.lower().str.replace("studio", "0").str.findall(r"\d+")
    df["bed_lo"] = beds.map(lambda b: int(b[0]) if b else None)
    df["bed_hi"] = beds.map(lambda b: int(b[-1]) if b else None)

    def range_slider(label, lo_col, hi_col, step, key):
        """Price / size range like on Listings; a condo stays if its range overlaps the chosen one."""
        lo, hi = df[lo_col].min(), df[hi_col].max()
        if pd.isna(lo) or pd.isna(hi) or lo >= hi:
            return None
        lo, hi = int(lo // step * step), int(-(-hi // step) * step)
        picked = st.slider(label, lo, hi, (lo, hi), step=step, key=key, format="%d")
        return None if picked == (lo, hi) else (lo_col, hi_col, picked)

    # ---------- filters (sidebar, like the Listings page) ----------
    with st.sidebar:
        st.header("Filters")
        areas = st.multiselect("Area", sorted(df.area.unique()), default=sorted(df.area.unique()))
        has = st.radio("Show", ["All", "For sale", "For rent"], horizontal=True)
        ranges = [range_slider("Sale price (RM)", "sale_min", "sale_max", 100_000, "condo-sale"),
                  range_slider("Monthly rent (RM)", "rent_min", "rent_max", 500, "condo-rent"),
                  range_slider("Size (sqft)", "sqft_min", "sqft_max", 100, "condo-sqft")]
        bed_opts = sorted({n for lo, hi in zip(df.bed_lo, df.bed_hi) if pd.notna(lo) and pd.notna(hi)
                           for n in range(int(lo), int(hi) + 1)})
        want_beds = st.multiselect("Bedrooms", bed_opts)
        types = st.multiselect("Property type", sorted(df.property_type.dropna().unique()))
        tenures = st.multiselect("Tenure", sorted(df.tenure.dropna().unique()))
        text = st.text_input("Search condo name")

    view = df[df.area.isin(areas)]
    if text:
        view = view[view.condo.str.contains(text, case=False, na=False)]
    if has == "For sale":
        view = view[view.sale_listings > 0]
    elif has == "For rent":
        view = view[view.rent_listings > 0]
    for r in filter(None, ranges):
        lo_col, hi_col, (lo, hi) = r
        view = view[(view[hi_col] >= lo) & (view[lo_col] <= hi)]
    if want_beds:
        view = view[[any(pd.notna(a) and pd.notna(b) and a <= n <= b for n in want_beds)
                     for a, b in zip(view.bed_lo, view.bed_hi)]]
    if types:
        view = view[view.property_type.isin(types)]
    if tenures:
        view = view[view.tenure.isin(tenures)]

    k1, k2, k3 = st.columns(3)
    k1.metric("Condos", f"{len(view):,}")
    k2.metric("Median lowest sale", f"RM {view.sale_min.median():,.0f}" if view.sale_min.notna().any() else "–")
    k3.metric("Median lowest rent", f"RM {view.rent_min.median():,.0f}" if view.rent_min.notna().any() else "–")

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


# ---------- WhatsApp chat bot (auto-replies on a separate number) ----------
def chat_bot_page():
    st.title("WhatsApp chat bot")
    mine, prop = st.tabs(["🙋 My assistant", "🏠 Property bot"])
    with mine:
        assistant_tab()
    with prop:
        property_bot_page()


def assistant_tab():
    """Your personal assistant on the bot's number - talks only to your own number (assistant.py)."""
    import assistant
    import bot_remote
    on_pc = (ROOT_DIR / "wa_bot.py").exists() and bool(os.environ.get("SUPABASE_SECRET_KEY"))
    try:
        import bot_client
        bot = (bot_client.status() or {}) if on_pc else bot_remote.state(db)
    except Exception:
        bot = {}
    a = assistant.load(db)
    st.caption("Texts **only your own number** from the bot's WhatsApp: one reminder at a time at random times in "
               "your hours, in Singlish / Chinese-English, worded differently every time. Answer it like a friend "
               "(\"not yet\", \"done\", \"bought, starts 5 Nov\") - and ask it things: \"give me whatsapp listing\", "
               "\"blast 30 done?\", \"resume the blast\", \"Residence 22 price?\", \"yesterday post what on "
               "PropertyGuru?\", \"scan the listing group\", \"remind me pay TNB every 15th\". Works even when the "
               "property bot is off. Runs on your PC.")
    me = (bot.get("me") or {}).get("number")
    status = bot.get("status") or "off"
    (st.success if status == "ready" else st.warning)(
        f"Bot WhatsApp: **{status}**" + (f" (+{me})" if me else "") + f" → texts **+{a.get('number')}**"
        + ("" if a.get("enabled") else " · ⏸ assistant switched off"))

    # ----- settings -----
    with st.expander("⚙️ Settings", expanded=False):
        c1, c2, c3, c4 = st.columns([1, 1.4, 2, 1.2])
        on = c1.toggle("On", a.get("enabled", True), key="as-on")
        number = c2.text_input("Your number", a.get("number"), key="as-num")
        hours = c3.slider("Texts you between", 0, 23, (int(a["start_hour"]), int(a["end_hour"])), format="%d:00",
                          key="as-hours")
        per_day = c4.number_input("Messages a day (max)", 1, 10, int(a["per_day"]), key="as-per-day")
        if st.button("💾 Save settings", key="as-save"):
            a.update(enabled=on, number=re.sub(r"\D", "", number) or a["number"], start_hour=hours[0],
                     end_hour=hours[1], per_day=int(per_day), plan={})
            assistant.save(a, db)
            st.toast("Saved")
            st.rerun()

    # ----- reminders -----
    st.markdown("**Your reminders** - change, add (last row) or delete (tick the row, then the bin), then **Save "
                "reminders**")
    kinds = {"todo": "To-do (daily until done)", "monthly": "Monthly (due date)", "checkin": "Check-in (every N days)"}
    back = {v: k for k, v in kinds.items()}
    table = pd.DataFrame([{
        "Reminder": t.get("title"), "Type": kinds.get(t.get("kind")),
        "Due date": pd.to_datetime(t["due"]).date() if t.get("due") else None,
        "Remind days before": t.get("remind_days_before"), "Ask every N days": t.get("every_days"),
        "Status": t.get("status") or "open", "Last answer": t.get("note") or "",
        "Last asked": datetime.fromtimestamp(t["last_asked"], MYT).strftime("%d %b %H:%M") if t.get("last_asked")
        else "", "_id": t.get("id")} for t in a["tasks"]])
    edited = st.data_editor(
        table, hide_index=True, width="stretch", num_rows="dynamic", key="as-tasks",
        disabled=["Last answer", "Last asked"],
        column_config={"Type": st.column_config.SelectboxColumn(options=list(kinds.values()), required=True),
                       "Due date": st.column_config.DateColumn(format="DD MMM YYYY"),
                       "Remind days before": st.column_config.NumberColumn(min_value=0, max_value=60),
                       "Ask every N days": st.column_config.NumberColumn(min_value=1, max_value=60),
                       "Status": st.column_config.SelectboxColumn(options=["open", "done", "cancelled"]),
                       "_id": None})
    if st.button("💾 Save reminders", key="as-save-tasks"):
        old = {t["id"]: t for t in a["tasks"]}
        tasks = []
        for _, r in edited.iterrows():
            if not (isinstance(r["Reminder"], str) and r["Reminder"].strip()):
                continue
            tid = r["_id"] if isinstance(r["_id"], str) and r["_id"] in old else \
                re.sub(r"[^a-z0-9]+", "-", r["Reminder"].lower()).strip("-")[:30] or f"t{len(tasks)}"
            t = {**old.get(tid, {}), "id": tid, "title": r["Reminder"].strip(),
                 "kind": back.get(r["Type"], "todo"), "status": r["Status"] or "open"}
            t["due"] = r["Due date"].isoformat() if not pd.isna(r["Due date"]) and r["Due date"] else None
            t["remind_days_before"] = int(r["Remind days before"]) if not pd.isna(r["Remind days before"]) else 10
            t["every_days"] = int(r["Ask every N days"]) if not pd.isna(r["Ask every N days"]) else 7
            tasks.append(t)
        a["tasks"] = tasks
        assistant.save(a, db)
        st.toast(f"Saved {len(tasks)} reminders")
        st.rerun()

    # ----- today -----
    plan = a.get("plan") or {}
    if plan.get("day") == datetime.now(MYT).date().isoformat():
        times = ", ".join(datetime.fromtimestamp(t, MYT).strftime("%I:%M %p").lstrip("0") for t in plan["times"])
        st.caption(f"Today's message times (random): {times} · {plan.get('sent', 0)} used")
    w = a.get("waiting")
    if w:
        st.caption(f"Last asked: \"{w.get('text')}\" - "
                   + ("answered ✅" if w.get("answered") else "waiting for your answer"))
    if on_pc and status == "ready" and st.button("📨 Send one now", key="as-nudge",
                                                 help="It texts you the next reminder right away (to try it)"):
        try:
            bot_client.assistant_nudge()
            st.toast("Sending - check your WhatsApp in a few seconds")
        except Exception as e:
            st.error(f"Couldn't reach the bot: {e}")

    # ----- your chat with it -----
    convs = bot_client.conversations() if on_pc else (bot.get("conversations") or [])
    chat = next((c for c in convs if c.get("chat") == assistant.HISTORY_KEY), None)
    with st.expander("💬 Your chat with it (latest)", expanded=bool(chat)):
        if not chat:
            st.caption("Nothing yet.")
        for role, text, ts in (chat or {}).get("messages", []):
            when = datetime.fromtimestamp(ts, MYT).strftime("%d %b %H:%M")
            st.markdown(f"{'🙋 **You**' if role == 'user' else '🤖 **Assistant**'} · <small>{when}</small>  \n"
                        f"{text}", unsafe_allow_html=True)


def property_bot_page():
    import base64

    import bot_brain
    import bot_client

    st.caption("Auto-replies on a SEPARATE WhatsApp number: finds listings, quotes condo prices, answers your FAQ and "
               "collects owner listings (saved to WhatsApp → Saved listings). Replies to private chats only - never "
               "groups, never messages anyone first. Runs on your PC: keep it on for instant replies.")
    try:
        import neonize  # noqa: F401 - only installed on the PC
    except ImportError:
        return chat_bot_remote()  # online dashboard: the bot runs on the PC, settings go through Supabase
    try:
        s = bot_client.ensure_running()
    except Exception as e:
        st.error(str(e))
        return

    # 1) link the bot's number
    if s["status"] not in ("ready", "connecting"):
        @st.fragment(run_every="3s")
        def link_box():
            try:
                cur = bot_client.ensure_running()
            except Exception as e:
                st.error(str(e))
                return
            if cur.get("status") in ("ready", "connecting"):
                st.rerun(scope="app")
            if cur.get("status") == "qr" and cur.get("qr"):
                st.image(base64.b64decode(cur["qr"].split(",", 1)[1]), width=300)
                st.markdown("On the **bot's phone** (the separate number): **WhatsApp → ⋮ / Settings → Linked devices "
                            "→ Link a device**, then scan. It shows as **KL Tracker Bot**.")
            elif cur.get("status") in ("error", "logged_out"):
                st.error(cur.get("error") or "Disconnected.")
                if st.button("Start again"):
                    bot_client.logout()
                    st.rerun(scope="app")
            else:
                st.info("Starting the bot… (about 30 seconds)")
        link_box()
        return

    me = s.get("me") or {}
    settings = bot_client.load_settings()
    c1, c2, c3 = st.columns([3, 1, 1])
    c1.success(f"Bot number connected: **+{me.get('number') or '?'}** · replies today: {s.get('replies_today', 0)}")
    on = c2.toggle("Bot ON", value=settings["enabled"])
    if on != settings["enabled"]:
        settings["enabled"] = on
        bot_client.save_settings(settings)
        st.rerun()
    if c3.button("Unlink number"):
        bot_client.logout()
        st.rerun()

    # 2) AI brain
    with st.expander("🧠 AI (free Groq key)", expanded=not os.environ.get("BOT_LLM_API_KEY")):
        has_key = bool(os.environ.get("BOT_LLM_API_KEY"))
        st.markdown(("✅ AI key saved - the bot replies with AI." if has_key else
                     "⚠️ No AI key yet - the bot only gives simple keyword replies.") +
                    "\n\nGet a free key: **console.groq.com → sign in → API Keys → Create API key**, paste it below. "
                    "It's stored only in `.env` on this PC.")
        key = st.text_input("Groq API key", type="password", placeholder="gsk_...")
        if st.button("Save key") and key.strip():
            bot_client.set_env("BOT_LLM_API_KEY", key.strip())
            os.environ["BOT_LLM_API_KEY"] = key.strip()
            st.success("Saved - new replies use AI straight away.")
        test = st.text_input("Try the bot here (nothing is sent on WhatsApp)",
                             placeholder="e.g. any 3 room Mont Kiara below 10k?")
        if test:
            with st.spinner("Thinking…"):
                st.info(bot_brain.reply([{"role": "user", "content": test}], lambda **k: {"saved": "test - not saved"}))

    # 3) the agent's real details - the bot never invents these
    prof = bot_brain.profile()
    with st.expander("👤 Your details (name card) - the bot only uses these, never makes them up",
                     expanded=not prof.get("agent_name")):
        new_prof = {k: st.text_input(label, prof.get(k, ""), key=f"prof-{k}")
                    for k, label in bot_brain.PROFILE_FIELDS.items()}
        st.caption("Leave a field empty if you don't want the bot to share it - it will say you'll send it yourself.")
        if st.button("Save details"):
            s2 = bot_client.load_settings()
            s2["profile"] = {k: v.strip() for k, v in new_prof.items()}
            bot_client.save_settings(s2)
            st.success("Saved - the bot uses these from the next message.")

    # 4) FAQ
    with st.expander("📝 Your agency info / FAQ (the bot answers from this)"):
        current = bot_brain.faq()
        new = st.text_area("FAQ", current, height=220, label_visibility="collapsed")
        if st.button("Save FAQ") and new != current:
            bot_brain.FAQ_FILE.parent.mkdir(exist_ok=True)
            bot_brain.FAQ_FILE.write_text(new, encoding="utf-8")
            st.success("Saved.")

    # 4) conversations
    st.subheader("Recent conversations")
    convs = bot_client.conversations()
    if not convs:
        st.caption("No messages yet - send the bot's number a WhatsApp from another phone to test it.")
    for cv in convs:
        when = datetime.fromtimestamp(cv["last_ts"], MYT).strftime("%d %b %H:%M")
        who = cv["name"] or cv["chat"].split("@")[0]
        with st.expander(f"{who} · {when}"):
            for role, text, ts in cv["messages"]:
                st.markdown(f"**{'🤖 Bot' if role == 'assistant' else '👤 ' + who}:** {text}")


def chat_bot_remote():
    """Chat bot page on the online dashboard: shows what the PC copied up (bot_remote.sync) and sends changes /
    test questions to the PC (pc_tasks), which applies them within about 15 seconds."""
    import time

    import bot_brain
    import bot_remote

    try:
        bot = bot_remote.state(db)
        waiting = bot_remote.pending(db)
    except Exception as e:
        st.error(f"Couldn't reach Supabase: {e}")
        return
    if not bot:
        st.warning("🖥️ Your PC hasn't sent the bot's status yet - make sure it's on, then reload in a minute.")
        return
    age = time.time() - (bot.get("at") or 0)
    if age > 300:
        st.warning(f"🖥️ Your PC was last seen {age / 60:.0f} minutes ago - the bot only replies while the PC is on, "
                   "and changes made here wait until it's back.")
    if bot.get("status") not in ("ready", "connecting"):
        st.warning("The bot's WhatsApp isn't connected - link it by QR code on the PC (Chat bot page) once.")
        return
    if [t for t in waiting if t["kind"] == "bot_settings"]:
        st.info("⏳ Your change was sent to the PC - it's applied within about 15 seconds. Reload to see it.")

    me = bot.get("me") or {}
    c1, c2 = st.columns([3, 1])
    c1.success(f"Bot number connected: **+{me.get('number') or '?'}** · replies today: {bot.get('replies_today', 0)}"
               f" · updated {max(0, age):.0f} s ago")
    on = c2.toggle("Bot ON", value=bool(bot.get("enabled", True)))
    if on != bool(bot.get("enabled", True)):
        bot_remote.request(db, "bot_settings", {"enabled": on})
        st.rerun()

    with st.expander("🧠 AI", expanded=False):
        st.markdown("✅ AI key saved on your PC - the bot replies with AI." if bot.get("has_key") else
                    "⚠️ No AI key on your PC yet - add it on the PC's Chat bot page (it's kept there only).")
        q = st.text_input("Try the bot here (nothing is sent on WhatsApp) - your PC answers in about 15-30 seconds",
                          placeholder="e.g. any 3 room Mont Kiara below 10k?")
        if st.button("Ask") and q.strip():
            bot_remote.request(db, "bot_test", {"question": q.strip()})
            st.session_state.bot_test_asked = q.strip()
        asked = st.session_state.get("bot_test_asked")
        if asked:
            @st.fragment(run_every="5s")
            def test_answer():
                t = (bot_remote.state(db) or {}).get("test") or {}
                if t.get("question") == asked:
                    st.info(t.get("answer") or "(no answer)")
                else:
                    st.caption("⏳ Waiting for your PC to answer…")
            test_answer()

    prof = bot.get("profile") or {}
    with st.expander("👤 Your details (name card) - the bot only uses these, never makes them up",
                     expanded=not prof.get("agent_name")):
        new_prof = {k: st.text_input(label, prof.get(k, ""), key=f"rprof-{k}")
                    for k, label in bot_brain.PROFILE_FIELDS.items()}
        st.caption("Leave a field empty if you don't want the bot to share it - it will say you'll send it yourself.")
        if st.button("Save details"):
            bot_remote.request(db, "bot_settings", {"profile": {k: v.strip() for k, v in new_prof.items()}})
            st.success("Sent to your PC - the bot uses these within about 15 seconds.")

    with st.expander("📝 Your agency info / FAQ (the bot answers from this)"):
        current = bot.get("faq") or ""
        new = st.text_area("FAQ", current, height=220, label_visibility="collapsed")
        if st.button("Save FAQ") and new != current:
            bot_remote.request(db, "bot_settings", {"faq": new})
            st.success("Sent to your PC - saved within about 15 seconds.")

    st.subheader("Recent conversations")
    st.caption("Copied from your PC every 30 seconds.")
    convs = bot.get("conversations") or []
    if not convs:
        st.caption("No messages yet - send the bot's number a WhatsApp from another phone to test it.")
    for cv in convs:
        when = datetime.fromtimestamp(cv["last_ts"], MYT).strftime("%d %b %H:%M")
        who = cv["name"] or cv["chat"].split("@")[0]
        with st.expander(f"{who} · {when}"):
            for role, text, ts in cv["messages"]:
                st.markdown(f"**{'🤖 Bot' if role == 'assistant' else '👤 ' + who}:** {text}")


# ---------- WhatsApp blast (one message to many numbers, sent slowly from this PC) ----------
def whatsapp_blast_page():
    from collections import Counter

    import importlib
    import time

    import wa_blast
    wa_blast = importlib.reload(wa_blast)  # always the latest version, even if the dashboard has run for days

    st.title("WhatsApp blast")
    st.caption("Send one message - with photos / videos / PDF and auto replies if you like - to a list of numbers, "
               "one person at a time with a random pause in between, now or at a time you choose. Your PC does the "
               "sending, so keep it on until a blast finishes (it won't go to sleep while a blast is waiting).")
    try:
        import neonize  # noqa: F401 - only installed on the PC
        on_pc = True
    except ImportError:
        on_pc = False

    senders, default_test = {}, ""
    if on_pc:  # here on the PC: blasts are files the sender reads directly
        import bot_client
        import wa_client
        store = wa_blast
        for key, mod in (("main", wa_client), ("bot", bot_client)):
            s = mod.status()
            if not s and key == "main":
                try:
                    s = wa_client.ensure_running()
                except Exception:
                    s = None
            if s and s.get("status") == "ready":
                senders[key] = f"{wa_blast.SENDERS[key]} (+{(s.get('me') or {}).get('number') or ''})"
        default_test = (wa_client.load_settings().get("my_numbers") or [""])[0]
    else:  # online: blasts go to Supabase and the PC picks them up (blast_remote.py)
        import blast_remote
        store = blast_remote.RemoteStore(db)
        try:
            pc = store.pc_status()
        except Exception:
            st.warning("Online blasting isn't set up yet - run **schema_blast_online.sql** once in Supabase "
                       "(SQL Editor), then reload this page.")
            return
        age = time.time() - (pc.get("at") or 0)
        for key in ("main", "bot"):
            info = pc.get(key) or {}
            if info.get("number") and (info.get("status") == "ready" or age > 120):
                senders[key] = f"{wa_blast.SENDERS[key]} (+{info['number']})"
        if not pc:
            st.warning("🖥️ Your PC hasn't checked in yet - make sure it's on (KL Tracker starts by itself when you "
                       "log in to Windows).")
        elif age < 120:
            st.success("🖥️ Your PC is on - blasts start sending within about 30 seconds.")
        else:
            ago = f"{age / 60:.0f} minutes" if age < 5400 else f"{age / 3600:.0f} hours"
            st.warning(f"🖥️ Your PC was last seen {ago} ago. You can still set up blasts - they wait and send "
                       "when the PC is back on (within your sending hours).")
        default_test = (pc.get("my_numbers") or [""])[0]

    new, mine = st.tabs(["New blast", "My blasts"])
    with new:
        if not senders:
            st.warning("No WhatsApp connected yet - link your number on the **WhatsApp listing** page on your PC first "
                       "(or the bot number on the **Chat bot** page), then come back.")
        else:
            new_blast_form(store, senders, default_test)
    with mine:
        @st.fragment(run_every="10s")
        def blast_list():
            all_jobs = store.jobs()
            if not all_jobs:
                st.info("No blasts yet.")
                return
            for i, j in enumerate(all_jobs):
                res = (j.get("progress") or {}).get("results") or {}
                count = Counter(r.get("status") for r in res.values())
                total, state = len(j["numbers"]), wa_blast.state_of(j)
                live = state not in ("Finished", "Cancelled")
                head = (f"{'🧪 Test' if j.get('test') else '📣'} {datetime.fromtimestamp(j['created'], MYT):%d %b %I:%M %p}"
                        f" · {total} {'person' if total == 1 else 'people'} · {state}")
                with st.expander(head, expanded=live and i < 3):
                    unable = count["not on WhatsApp"] + count["failed"]
                    st.progress(len(res) / max(total, 1),
                                text=f"✅ {count['sent']} sent · ❌ {unable} unable to message · "
                                     f"⏳ {total - len(res)} still to send")
                    if unable:
                        names = {n["phone"]: n.get("name") for n in j["numbers"]}
                        st.error(f"❌ **Unable to message {unable}:**\n\n" + "\n".join(
                            f"- {p}{' ' + names[p] if names.get(p) else ''} - {r.get('error') or r['status']}"
                            for p, r in res.items() if r.get("status") != "sent"))
                        retry = [n for n in j["numbers"] if (res.get(n["phone"]) or {}).get("status") == "failed"]
                        if retry and not live and st.button(f"🔁 Try the {len(retry)} failed again",
                                                            key=f"retry-{j['id']}",
                                                            help="Numbers not on WhatsApp are left out."):
                            store.create(j["sender"], retry, j["message"], store.attachments_of(j),
                                         triggers=j.get("triggers") or [], delay=j["delay"],
                                         daily_limit=j["daily_limit"], window=wa_blast.window_of(j),
                                         rest=((j.get("rest") or {}).get("every", 0),
                                               (j.get("rest") or {}).get("minutes", 0)))
                            st.rerun()
                    rest = j.get("rest") or {}
                    w = wa_blast.window_of(j)
                    st.caption("From: " + wa_blast.SENDERS.get(j["sender"], j["sender"])
                               + (f" · sends {wa_blast.clock(w[0])} - {wa_blast.clock(w[1])}" if w else "")
                               + (f" · rests {rest['minutes'] // 60} h {rest['minutes'] % 60} min after every "
                                  f"{rest['every']} messages" if rest.get("every") and rest.get("minutes") else "")
                               + (f" · attachments: {', '.join(j['files'])}" if j.get("files") else ""))
                    st.text(wa_blast.full_text(j, "{name}")[:600])
                    if live:
                        b = st.columns(4)
                        if j["control"] == "pause":
                            if b[0].button("▶️ Resume", key=f"res-{j['id']}"):
                                store.set_control(j["id"], "run")
                                st.rerun()
                        elif b[0].button("⏸️ Pause", key=f"pau-{j['id']}"):
                            store.set_control(j["id"], "pause")
                            st.rerun()
                        if b[1].button("✖️ Cancel", key=f"can-{j['id']}"):
                            store.set_control(j["id"], "cancel")
                            st.rerun()
                    label = {"sent": "✅ Sent", "not on WhatsApp": "❌ Unable to message",
                             "failed": "❌ Unable to message"}
                    rows = [{"Number": n["phone"], "Name": n.get("name") or "",
                             "Result": label.get((res.get(n["phone"]) or {}).get("status"), "⏳ Waiting"),
                             "Time": (datetime.fromtimestamp(res[n["phone"]]["at"], MYT).strftime("%d %b %I:%M %p")
                                      if n["phone"] in res else ""),
                             "Problem": (res.get(n["phone"]) or {}).get("error", "")} for n in j["numbers"]]
                    table = pd.DataFrame(rows)
                    st.dataframe(table, hide_index=True, width="stretch", height=min(400, 38 + 35 * len(rows)))
                    st.download_button("Download results (CSV)", table.to_csv(index=False).encode("utf-8-sig"),
                                       f"blast {j['id']}.csv", "text/csv", key=f"dl-{j['id']}", on_click="ignore")
        blast_list()


def new_blast_form(wa_blast, senders, default_test=""):
    """wa_blast = where blasts are kept: the wa_blast module on the PC, blast_remote.RemoteStore online."""
    import time
    from datetime import time as dtime

    from phone_filter import extract_from_text

    v = st.session_state.setdefault("blast_v", 0)  # bumped after a blast starts -> widgets come back empty
    sender = st.radio("Send from", list(senders), format_func=senders.get, horizontal=True)

    st.subheader("1. Who")
    c1, c2 = st.columns(2)
    pasted = c1.text_area("Paste numbers - one per line. Add a name after the number if you like "
                          "(e.g. `0123456789 Ali`)", height=170, key=f"blast-numbers-{v}")
    ups = c2.file_uploader("…or upload .txt / Excel / CSV files", type=["txt", "csv", "xlsx", "xls"],
                           accept_multiple_files=True, key=f"blast-files-{v}")
    lines = pasted.splitlines()
    for u in ups or []:
        try:
            lines += wa_blast.lines_from_file(u.name, u.getvalue())
        except Exception as e:
            st.warning(f"Couldn't read {u.name}: {e}")
    numbers, rejected, dupes = wa_blast.parse_numbers(lines)
    skip_days = c2.number_input("Skip people I already blasted in the last … days (0 = don't skip)", 0, 365, 7)
    recent = wa_blast.recently_sent(skip_days) if skip_days else set()
    skipped = [n for n in numbers if n["phone"] in recent]
    numbers = [n for n in numbers if n["phone"] not in recent]
    if lines:
        bits = [f"**{len(numbers)}** to send"]
        bits += [f"{dupes} duplicate{'s' * (dupes != 1)} removed"] if dupes else []
        bits += [f"{len(skipped)} already blasted in the last {skip_days} days"] if skipped else []
        bits += [f"{len(rejected)} not a Malaysian mobile"] if rejected else []
        st.markdown(" · ".join(bits))
        with st.expander("See the list"):
            st.dataframe(pd.DataFrame(numbers or [{"phone": "", "name": ""}]).rename(
                columns={"phone": "Number", "name": "Name"}), hide_index=True, width="stretch")
            if rejected:
                st.caption("Left out: " + ", ".join(f"{raw} ({why})" for raw, why in rejected[:30]))

    st.subheader("2. What")
    msg = st.text_area("Message", height=170, key=f"blast-msg-{v}",
                       value="Hi I'm Kelvin Low property agent here focus on KLCC & Mont Kiara property, you have "
                             "property want to rent or sell? or any other properties are welcome.",
                       placeholder="{Hi|Hello} {name}, this is Mr Low from Homex Property 😊\n"
                                   "Got a few new units at KLCC for rent, interested to view?")
    st.caption("`{name}` = the person's name (left out when you didn't give one). `{Hi|Hello|Hey}` = picks one "
               "at random for each person, so the messages aren't all identical - WhatsApp is less likely to "
               "flag you.")
    atts = st.file_uploader("Photos / videos / PDF (optional - the message goes with the first one)",
                            accept_multiple_files=True, key=f"blast-atts-{v}",
                            type=["jpg", "jpeg", "png", "webp", "mp4", "mov", "pdf", "doc", "docx", "xls", "xlsx"])
    st.markdown("**Auto replies** - when someone answers this blast, the AI reads their reply and sends the "
                "matching auto reply below.")
    triggers = []
    if st.toggle("Auto reply to people who answer", value=True, key=f"blast-trig-on-{v}"):
        for i, (when0, reply0) in enumerate(wa_blast.DEFAULT_TRIGGERS):
            c1, c2 = st.columns([1, 1])
            when = c1.text_input(f"{i + 1}. When they…", when0, key=f"blast-tw{i}-{v}",
                                 placeholder="(leave empty for no rule)")
            reply = c2.text_area("…auto reply", reply0, key=f"blast-tr{i}-{v}", height=68, disabled=not when.strip())
            if when.strip() and reply.strip():
                triggers.append({"when": when.strip(), "reply": reply.strip()})
        st.caption(f"Each person gets **one** auto reply - the first time their message fits a rule - only for "
                   f"people from this blast, for {wa_blast.REPLY_DAYS} days, and not after you've replied to them "
                   "yourself. Messages that fit no rule are left for you. Your own test numbers (\"my numbers\" on "
                   "the WhatsApp listing page) get an auto reply every time, for testing.")
    spec = {"message": msg}
    if msg.strip():
        sample = numbers[0] if numbers else {"name": "Ali"}
        st.markdown("**Preview**" + (f" (for {sample.get('name') or 'someone without a name'})"))
        st.info(wa_blast.full_text(spec, sample.get("name")) or "(no text)")

    st.subheader("3. When")

    def mins_secs(s):
        return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"

    t1, t2 = st.columns(2)
    delay = t1.slider("Pause between people (seconds, random)", 15, 900, (45, 120), step=5)
    t1.caption(f"Waits {mins_secs(delay[0])} to {mins_secs(delay[1])} before the next person.")
    limit = t2.number_input("Max people per day", 1, 300, 40,
                            help="The rest carry on the next day. Keeping it low protects your number.")

    st.markdown("**Rest break**")
    r1, r2, r3 = st.columns(3)
    every = r1.number_input("Rest after every … messages (0 = no break)", 0, 500, 10)
    rest_h = r2.number_input("Rest for … hours", 0, 23, 0, disabled=not every)
    rest_m = r3.number_input("and … minutes", 0, 59, 15, disabled=not every)
    rest_min = (rest_h * 60 + rest_m) if every else 0
    if every and not rest_min:
        st.caption("Rest time is 0, so there's no break.")

    st.markdown("**Sending hours**")
    w1, w2 = st.columns(2)
    from_t = w1.time_input("Only send from", dtime(9, 0), step=60)
    until_t = w2.time_input("until", dtime(21, 0), step=60)
    window = (from_t.hour * 60 + from_t.minute, until_t.hour * 60 + until_t.minute)
    if window[0] == window[1]:
        w1.caption("Same time = sends any time of day.")

    when = st.radio("Start", ["Now", "At a set time"], horizontal=True)
    start_at = time.time()
    if when == "At a set time":
        d1, d2 = st.columns(2)
        now = datetime.now(MYT)  # Malaysia time, also on the online server (which runs on UTC)
        day = d1.date_input("Date", now.date(), min_value=now.date())
        at = d2.time_input("Time (hour and minute)", (now + timedelta(hours=1)).replace(minute=0).time(), step=60)
        start_at = datetime.combine(day, at, tzinfo=MYT).timestamp()
        if start_at < time.time():
            st.warning("That time has already passed - it would start right away.")
    if numbers:
        n = len(numbers)
        secs = n * (sum(delay) / 2 + 8) + ((n - 1) // every * rest_min * 60 if every and rest_min else 0)
        days = -(-n // limit)
        st.caption(f"About {secs // 3600:.0f} h {secs % 3600 // 60:.0f} min of sending (including breaks)"
                   + (f", spread over {days} days (max {limit} a day)" if days > 1 else "") + ".")

    files = [(a.name, a.getvalue()) for a in atts or []]
    if not (msg.strip() or files):
        return
    look = dict(triggers=triggers)
    st.divider()
    t_col, go_col = st.columns(2)
    test_to = t_col.text_input("Test number", default_test if sender == "main" else "",
                               placeholder="your own other number",
                               help="Sends the message once right now, so you can see how it looks.")
    if t_col.button("🧪 Send a test"):
        tn = next((n for n, _, _ in extract_from_text(test_to) if n), None)
        if not tn:
            st.error("Put a Malaysian mobile number in Test number.")
        else:
            wa_blast.create(sender, [{"phone": tn, "name": (numbers[0].get("name") if numbers else None)}],
                            msg, files, test=True, **look)
            st.success(f"Test sending to {tn} now - check that phone in a few seconds.")
    if not numbers:
        go_col.info("Add some numbers in step 1.")
        return
    ok = go_col.checkbox(f"Yes, send this to {len(numbers)} {'person' if len(numbers) == 1 else 'people'}")
    if go_col.button("📣 Start blast", type="primary", disabled=not ok):
        wa_blast.create(sender, numbers, msg, files, start_at=start_at, delay=delay, **look,
                        daily_limit=limit, window=window, rest=(every, rest_min))
        st.session_state.blast_v = v + 1  # fresh, empty form next time - no accidental second send
        st.success("Blast saved" + (f" - starts {datetime.fromtimestamp(start_at, MYT):%d %b %I:%M %p}"
                                    if when == "At a set time" else " - sending has started")
                   + ". Follow it on the **My blasts** tab.")


# ---------- AI call (ipline.cn 呼出记录, grade A -> who really wants it) ----------
def ai_call_page():
    import importlib
    import subprocess
    import sys

    st.title("AI call")
    st.caption("Checks your ipline (获客通) 呼出记录 with 意向等级 A: listens to every recording, reads the text, and "
               "keeps the people who said yes / ok / can or asked where / which property. Confused, hostile or "
               "unclear ones are marked fake. Tick or untick to confirm, then download the ticked numbers.")
    import ai_call
    import ai_call_remote
    import blast_remote
    ai_call, ai_call_remote = importlib.reload(ai_call), importlib.reload(ai_call_remote)
    # On the PC the check runs here; online the PC is asked through Supabase (ai_call_remote.py)
    on_pc = (ROOT_DIR / "ai_call.py").exists() and bool(os.environ.get("IPLINE_USER"))

    def pc_check_status():
        if on_pc:
            return ai_call.status()
        return (blast_remote.RemoteStore(db).pc_status() or {}).get("ai_call") or {}

    try:
        stt = pc_check_status()
        waiting = [] if on_pc else ai_call_remote.pending_requests(db)
    except Exception:
        st.warning("AI call online isn't set up yet - run **schema_ai_call_online.sql** once in Supabase "
                   "(SQL Editor), then reload this page.")
        return

    # ----- run a check in the background -----
    running = stt.get("state") == "running" and time_now() - stt.get("at", 0) < 600
    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 1, 2])
        start = c1.number_input("Start from page", 1, 999, 1, help="Page 1 = the newest calls")
        pages = c2.number_input("How many pages (30 calls each)", 1, 200, 1)
        quick = c1.radio("Check", ["Full", "Quick"], horizontal=True, help=(
            "Full: listens to the whole call twice (most accurate, ~250-300 calls a day on the free AI). "
            "Quick: only listens to what they said from 'Do you want to sell or rent your property?' on - about "
            "2x faster and ~500 calls a day, but misses about 1 in 4 real leads (ones only the whole recording "
            "hears clearly). Good for the old backlog; run Full on those pages later to check them properly.")) \
            == "Quick"
        c3.caption(f"About {pages * (2 if quick else 4)} minutes. Calls already checked are skipped, so you can run "
                   "it again any time to pick up new calls." + ("" if on_pc else " Your PC does the checking - keep it on."))
        if running or waiting:
            @st.fragment(run_every="5s")
            def progress_box():
                s = pc_check_status()
                if s.get("state") == "running" and time_now() - s.get("at", 0) < 600:
                    st.info(f"⏳ Checking page {s.get('page')} (of {s.get('of_pages')}) - call {s.get('call')} of "
                            f"{s.get('calls')}: {s.get('phone')}. You can leave this page; it carries on.")
                elif not on_pc and ai_call_remote.pending_requests(db):
                    st.info("⏳ Sent to your PC - it starts within about 15 seconds (if the PC is on).")
                else:
                    load_ai_rows.clear()
                    st.rerun(scope="app")
            with c3:
                progress_box()
        elif c3.button("▶️ Check calls", type="primary"):
            if on_pc:
                (ROOT_DIR / "logs").mkdir(exist_ok=True)
                ai_call.OUT.mkdir(parents=True, exist_ok=True)
                ai_call._save(ai_call.STATUS, {"state": "running", "at": time_now(), "page": start, "call": 0,
                                               "calls": "?", "of_pages": "?", "phone": "logging in…"})
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                subprocess.Popen([sys.executable, "-u", str(ROOT_DIR / "ai_call.py"), "--pages", str(pages),
                                  "--start", str(start)] + (["--quick"] if quick else []), cwd=ROOT_DIR,
                                 creationflags=flags,
                                 stdout=open(ROOT_DIR / "logs" / "ai_call.log", "a", encoding="utf-8"),
                                 stderr=subprocess.STDOUT)
            else:
                ai_call_remote.request_check(db, start, pages, quick)
            st.rerun()
        if stt.get("state") == "error":
            c3.error(f"Last check stopped: {stt.get('error')}")
        elif stt.get("state") == "done" and stt.get("errors"):
            c3.warning(f"Last check: {stt['errors']} call(s) couldn't be checked - run it again to retry them.")

    rows = ai_call.results() if on_pc else load_ai_rows(db)
    if not rows:
        st.info("No calls checked yet - press **Check calls**." if on_pc else
                "No calls here yet - press **Check calls**, or wait a minute for your PC to copy its results up.")
        return
    remote_ticks = load_ai_ticks(db)  # kept for 30 s: a click that only opens a call doesn't fetch them again
    ticks = {**ai_call.load_ticks(), **remote_ticks} if on_pc else remote_ticks

    def save_ticks(changed):
        """Ticks go to Supabase (shared with the online page); the PC keeps its own copy as well."""
        ai_call_remote.save_ticks(db, changed)
        if on_pc:
            ai_call.save_ticks({**ai_call.load_ticks(), **changed})
        load_ai_ticks.clear()
    df = pd.DataFrame(rows)
    for c in ("said", "reason", "heard", "ipline", "full", "called", "verdict"):
        df[c] = df[c].fillna("") if c in df else ""
    # Only what the person said after "Do you want to sell or rent your property?" - no hello / bye
    df["ipline_said"] = [ai_call.after_q1(turns) if isinstance(turns, list) else ""
                         for turns in (df["turns"] if "turns" in df else [None] * len(df))]
    # The same answer as each hearing got it (after the question, no hello / bye): their own voice clips, the clips
    # heard as Chinese (Chinese answers come out as English-sounding nonsense in the first), and ipline's text
    turns_col = df["turns"] if "turns" in df else [None] * len(df)
    df["english"] = [ai_call.after_q1(t, "heard") if isinstance(t, list) else "" for t in turns_col]
    zh_turns = [ai_call.after_q1(t, "heard_zh") if isinstance(t, list) else "" for t in turns_col]
    df["chinese"] = [c if isinstance(c, str) and c else z
                     for c, z in zip(df["chinese"] if "chinese" in df else [""] * len(df), zh_turns)]
    df["said"] = df.said.map(ai_call.clean_said)
    df["ai_want"] = df.verdict == "interested"
    df["keep"] = [ticks.get(i, w) for i, w in zip(df.call_id, df.ai_want)]
    df["reviewed"] = df.call_id.isin(ticks.keys())
    df["day"] = df.called.str[:10]

    # ----- filters -----
    f1, f2, f3 = st.columns([2, 2, 1])
    show = f1.radio("Show", ["All", "AI says want", "AI says fake", "Not confirmed by me yet"], horizontal=True)
    days = sorted(df.day.dropna().unique(), reverse=True)
    pick_days = f2.multiselect("Call date", days, placeholder="All dates")
    search = f3.text_input("Search number")
    view = df
    if show == "AI says want":
        view = view[view.ai_want]
    elif show == "AI says fake":
        view = view[~view.ai_want]
    elif show == "Not confirmed by me yet":
        view = view[~view.reviewed]
    if pick_days:
        view = view[view.day.isin(pick_days)]
    if search:
        view = view[view.phone.str.contains(re.sub(r"\D", "", search), na=False)]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Calls shown", f"{len(view):,}")
    m2.metric("AI says want", f"{int(view.ai_want.sum()):,}")
    m3.metric("Ticked ✔", f"{int(view.keep.sum()):,}")
    m4.metric("Confirmed by you", f"{int(view.reviewed.sum()):,}")

    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("✔ Tick all shown"):
        save_ticks({i: True for i in view.call_id})
        st.rerun()
    if b2.button("✖ Untick all shown"):
        save_ticks({i: False for i in view.call_id})
        st.rerun()

    table = pd.DataFrame({
        "✔ Keep": view.keep.map({True: "✅", False: "⬜"}).values, "Phone": view.phone.values,
        "AI says": view.ai_want.map({True: "✅ Want", False: "❌ Fake"}).values,
        "English": view.english.values, "Chinese": view.chinese.values, "ipline": view.ipline_said.values,
        "Why": view.reason.values,
        "Called": view.called.values, "Sec": view.seconds.values,
        "Checked by me": view.reviewed.map({True: "✔", False: ""}).values})
    st.caption("👆 Click a **number** (or anywhere on its row) to listen to that call · click the **✔ Keep** box "
               "to tick / untick it")
    # Clicking an already-selected cell makes the table pop up a "cell viewer" box over it - not wanted here
    st.markdown("<style>#portal .gdg-style, #portal .gdg-unstyle {display: none !important;}</style>",
                unsafe_allow_html=True)
    # The table keeps the same key, so a sort you made by clicking a heading survives clicks. A click is acted on
    # when the selected cell changes (the table itself remembers the last selected cell).
    tbl_key = f"aicall-{show}-{'|'.join(pick_days)}-{search}"
    ids_now, keep_now = list(view.call_id), [bool(k) for k in view.keep]

    def on_click():
        cells = [tuple(c) for c in st.session_state[tbl_key]["selection"]["cells"]]
        last = st.session_state.get("aicall_last_cell")
        st.session_state.aicall_last_cell = cells
        if not cells or cells == last:
            return
        row, col = int(cells[0][0]), cells[0][1]  # row = position in the original (unsorted) table
        if row >= len(ids_now):
            return
        cid = ids_now[row]
        if col == "✔ Keep":  # tick / untick
            save_ticks({cid: not keep_now[row]})
        else:  # open this call in "Listen to a call" and jump down to it
            st.session_state.aicall_box = cid
            st.session_state.aicall_scroll = True

    st.dataframe(
        table, hide_index=True, width="stretch", height=min(650, 38 + 35 * len(table)),
        on_select=on_click, selection_mode="single-cell",
        column_config={"✔ Keep": st.column_config.TextColumn(width="small"),
                       "English": st.column_config.TextColumn(
                           width="large", help="Their own voice clips heard again (English / Malay)"),
                       "Chinese": st.column_config.TextColumn(
                           width="large", help="The same clips heard as Chinese - right when they spoke Chinese"),
                       "ipline": st.column_config.TextColumn(width="large", help="ipline's own text of their words"),
                       "Why": st.column_config.TextColumn(width="medium")},
        key=tbl_key)

    keep = list(dict.fromkeys(view[view.keep].phone))

    def to_blast(phones):
        """Open the WhatsApp blast page with these numbers already in its number box."""
        st.session_state.page = "WhatsApp blast"
        st.session_state[f"blast-numbers-{st.session_state.get('blast_v', 0)}"] = "\n".join(phones)

    d1, d2, d3 = st.columns(3)
    d1.download_button(f"⬇️ Download ticked numbers ({len(keep)}) .txt", "\n".join(keep) + "\n",
                       f"AI call interested {datetime.now():%Y-%m-%d}.txt", "text/plain", type="primary",
                       disabled=not keep, on_click="ignore")
    d2.button(f"📣 Send to WhatsApp blast ({len(keep)})", disabled=not keep, on_click=to_blast, args=(keep,),
              help="Opens WhatsApp blast with these numbers filled in - just write the message and start")
    shown_table = view.drop(columns=["ai_want", "reviewed", "day", "turns"], errors="ignore")
    d3.download_button("Download the table (Excel / CSV)",  # made only when pressed (1 MB+ on every click otherwise)
                       lambda: shown_table.to_csv(index=False).encode("utf-8-sig"),
                       f"AI call {datetime.now():%Y-%m-%d}.csv", "text/csv", on_click="ignore")
    st.caption("The download uses the calls shown with the filters above.")

    # ----- listen to one call -----
    st.markdown('<div id="aicall-listen"></div>', unsafe_allow_html=True)
    st.subheader("🎧 Listen to a call")
    ids = list(view.call_id)
    if st.session_state.get("aicall_box") not in ids:
        st.session_state.aicall_box = ids[0] if ids else None
    labels = {i: f"{p} · {c} · {'✅ Want' if w else '❌ Fake'} · {s[:60]}"  # all at once (one lookup per call was slow)
              for i, p, c, w, s in zip(view.call_id, view.phone, view.called, view.ai_want,
                                       [e or z or ip for e, z, ip in zip(view.english, view.chinese, view.ipline_said)])}
    pick = st.selectbox("Call", ids, key="aicall_box", format_func=labels.get)
    jump = st.session_state.pop("aicall_scroll", False)
    if pick:
        r = view[view.call_id == pick].iloc[0]
        st.markdown(f"### 📞 {r.phone}  ·  {'✅ Want' if r.ai_want else '❌ Fake'}")
        st.caption(f"{r.called} · {r.reason}")
        f = ai_call.audio_file(pick) if on_pc else ai_call_remote.audio_url(db, pick)
        if f:
            st.audio(str(f), format="audio/mpeg", autoplay=jump)
        else:
            st.caption("Recording not here yet." + ("" if on_pc else " Your PC copies it up within a minute."))
        keep_this = bool(ticks.get(pick, r.ai_want))  # own name: keep_now is the table's list (used by on_click)
        if st.toggle("✔ Keep this number", value=keep_this, key=f"aicall-keep-{pick}") != keep_this:
            save_ticks({pick: not keep_this})
            st.rerun()
        st.caption("Three hearings of the same call - phone lines blur similar words, so they can differ. "
                   "The AI goes by what they agree on; the recording above is the real thing.")
        l1, l2, l3, l4 = st.columns(4)
        l1.markdown("**Person's voice clips, re-heard**")
        l1.text(r.heard or "(nothing)")
        l2.markdown("**Heard as Chinese**")
        l2.text(r.chinese.replace(" / ", "\n") if r.chinese else "(not heard as Chinese)")
        l3.markdown("**Whole recording, re-heard**")
        l3.text(r.full or "(nothing)")
        l4.markdown("**ipline's own text**")
        l4.text(r.ipline or "(nothing)")
    if jump:  # scroll the page down to this section
        st.iframe("<script>setTimeout(() => {const el = window.parent.document.getElementById('aicall-listen');"
                  "if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});}, 300);</script>", height=1)


def time_now():
    import time
    return time.time()


@st.cache_data(ttl=30, show_spinner=False)
def load_ai_ticks(_db):
    """Your ticks from Supabase (shared by the PC and the online page) - cleared whenever you tick."""
    import ai_call_remote
    return ai_call_remote.load_ticks(_db)


@st.cache_data(ttl=60, show_spinner="Loading checked calls…")
def load_ai_rows(_db):
    """Checked calls from Supabase (online page) - refreshed every minute, or when a check finishes."""
    import ai_call_remote
    return ai_call_remote.load_rows(_db)


# ---------- Google Map Extractor ----------
def gmaps_page():
    """Search Google Maps and list the businesses found (gmaps.py - runs on the PC, also when asked online)."""
    import importlib
    import io
    import gmaps
    importlib.reload(gmaps)
    st.title("Google Map Extractor")
    st.caption("Search Google Maps like on your phone (\"property agency Mont Kiara\", \"interior design Cheras\") "
               "and get each business found: company name, address, postcode, state, website, email and phone "
               "number. Runs on your PC in a hidden Chrome window; emails come from each company's own website. "
               "Google shows about 120 places per search at most - for more, search area by area.")
    on_pc = (ROOT_DIR / "gmaps.py").exists() and bool(os.environ.get("SUPABASE_SECRET_KEY"))

    def now_status():
        return gmaps.status() if on_pc else (blast_remote_status(db).get("gmaps") or {})

    s = now_status()
    if s.get("state") == "running" and datetime.now().timestamp() - s.get("at", 0) < 300:
        @st.fragment(run_every="4s")
        def progress():
            s = now_status()
            if s.get("state") != "running":
                st.session_state.gmaps_pick = s.get("id")
                st.rerun(scope="app")
            st.info(f"🔍 Extracting \"{s.get('query')}\" on your PC… {s.get('done', 0)} of {s.get('total', '?')}"
                    f" · {s.get('text', '')}")
            if st.button("⏹ Stop", help="Stops after the place it's on - what's found so far is kept"):
                gmaps.stop() if on_pc else gmaps.request_stop(db)
                st.toast("Stopping…")
        progress()
    else:
        # Google Maps stops at about 120 places a search - "split by area" runs one search per area and joins them
        split = st.toggle("Split by area (more than 120 places)", key="gmaps_split",
                          help="Google Maps gives about 100-120 places per search at most. This searches each area "
                               "on its own (\"property agency Bangsar\", \"property agency Mont Kiara\", …) and "
                               "joins the results - each company once.")
        areas = []
        if split:
            regions = st.multiselect("Areas of", list(gmaps.AREAS), default=["Kuala Lumpur"], key="gmaps_regions")
            preset = [a for r in regions for a in gmaps.AREAS[r]]
            # a new text box when the regions change, so the list follows them (your edits stay until then)
            areas_text = st.text_area("Areas to search (one per line - add, remove or change any)", "\n".join(preset),
                                      height=150, key=f"gmaps_areas-{'|'.join(regions)}")
            areas = list(dict.fromkeys(a.strip() for a in areas_text.splitlines() if a.strip()))
        with st.form("gmaps_form"):
            c1, c2 = st.columns([3, 1])
            q = c1.text_input("Search Google Maps for", placeholder="property agency" if split else
                              "property agency Mont Kiara",
                              help="With Split by area, leave the area out - it's added for each area" if split
                              else None)
            if split:
                n = c2.number_input("How many places in all", 1, 5000, 1000, step=100,
                                    help="Stops when it has this many (or when the areas run out)")
            else:
                n = c2.number_input("How many places", 1, 120, 50, step=10,
                                    help="Google Maps gives about 100-120 places per search at most - for more, "
                                         "turn on Split by area")
            em = st.checkbox("Find emails on their websites (slower - a few seconds more per place)", value=True)
            if split:
                st.caption(f"{len(areas)} areas: {', '.join(areas[:6])}{'…' if len(areas) > 6 else ''} · roughly "
                           f"{max(1, round(min(int(n), len(areas) * 60) * (8 if em else 4) / 3600 + len(areas) / 60))}"
                           " hour(s) for the whole list - you can stop any time and keep what's found")
            go = st.form_submit_button("🔍 Extract", type="primary")
        if go and q.strip() and not (split and not areas):
            if on_pc:
                st.session_state.gmaps_pick = gmaps.start(q.strip(), int(n), em, areas or None)
            else:
                gmaps.request(db, q.strip(), int(n), em, areas or None)
                st.session_state.gmaps_asked = True
            st.rerun()
        if go and split and not areas:
            st.warning("Add at least one area.")
        if go and not q.strip():
            st.warning("Type what to search for first.")
        if st.session_state.get("gmaps_asked") and not on_pc and s.get("state") != "running":
            st.caption("⏳ Sent to your PC - it starts within a minute (if the PC is on).")
    if s.get("state") == "error":
        st.error(f"The last search stopped with a problem: {s.get('error')}")

    # ----- past searches -----
    index = sorted(gmaps.load_index(db), key=lambda j: -(j.get("at") or 0))
    if not index:
        st.info("No searches yet - type one above and press Extract.")
        return
    by_id = {j["id"]: j for j in index}
    if st.session_state.get("gmaps_pick") not in by_id:
        st.session_state.gmaps_pick = index[0]["id"]
    st.subheader("Results")
    pick = st.selectbox("Search", list(by_id), key="gmaps_pick", format_func=lambda i: (
        by_id[i]["query"] + (f" · {by_id[i]['areas']} areas" if by_id[i].get("areas") else "")
        + f" · {by_id[i]['count']} places · "
        f"{datetime.fromtimestamp(by_id[i]['at'], MYT):%d %b %Y %H:%M}"
        + (" · still running" if by_id[i].get("state") == "running" else "")
        + (" · stopped" if by_id[i].get("state") == "stopped" else "")))
    job = gmaps.load_job(db, pick)
    if not job:
        st.warning("Couldn't load this search.")
        return
    rows = job["rows"]
    f1, f2, f3, f4 = st.columns([1, 1.3, 2, 2])
    only_email = f1.checkbox("Only with email")
    only_mobile = f2.checkbox("Only with mobile (WhatsApp)")
    states = f3.multiselect("State", sorted({r["state"] for r in rows if r.get("state")}), placeholder="All states")
    text = f4.text_input("Search name / address / category")
    # Google's own category for each place ("Investment company", "Real estate agency", ...), most common first
    cat_count = pd.Series([r.get("category") or "(no category)" for r in rows]).value_counts()
    split = any(r.get("area") for r in rows)
    g1, g2 = st.columns(2) if split else (st.container(), None)
    cats = g1.multiselect("Category", list(cat_count.index), placeholder="All categories",
                          format_func=lambda c: f"{c} ({cat_count[c]})",
                          help="Google Maps' own category for each place - e.g. tick \"Investment company\" only")
    found_in = g2.multiselect("Found in area", list(dict.fromkeys(r["area"] for r in rows if r.get("area"))),
                              placeholder="All areas") if split else []
    view = [r for r in rows if (not only_email or r.get("email")) and (not only_mobile or r.get("whatsapp"))
            and (not states or r.get("state") in states) and (not found_in or r.get("area") in found_in)
            and (not cats or (r.get("category") or "(no category)") in cats)
            and (not text or text.lower() in f"{r.get('name')} {r.get('address')} {r.get('category')}".lower())]
    st.caption(f"{len(view)} of {len(rows)} places · {sum(1 for r in view if r.get('email'))} with email · "
               f"{sum(1 for r in view if r.get('whatsapp'))} mobile numbers (WhatsApp)")
    table = pd.DataFrame([{
        "Company name": r.get("name"), "Category": r.get("category"), "Address": r.get("address"),
        "Postcode": r.get("postcode"), "State": r.get("state"), "Website": r.get("website") or None,
        "Email": r.get("email"), "Phone number": r.get("phone"), "Rating": r.get("rating"),
        "Google Maps": r.get("maps_url"), "Area": r.get("area")} for r in view],
        columns=["Company name", "Category", "Address", "Postcode", "State", "Website", "Email", "Phone number",
                 "Rating", "Google Maps", "Area"])
    if not split:
        table = table.drop(columns=["Area"])
    st.dataframe(table, hide_index=True, width="stretch", height=min(650, 38 + 35 * max(len(table), 1)),
                 column_config={"Address": st.column_config.TextColumn(width="large"),
                                "Website": st.column_config.LinkColumn(display_text=r"https?://(?:www\.)?([^/?]+)"),
                                "Google Maps": st.column_config.LinkColumn(display_text="Open ↗")})

    mobiles = list(dict.fromkeys(r["whatsapp"] for r in view if r.get("whatsapp")))

    def to_blast(phones):
        """Open the WhatsApp blast page with these numbers already in its number box."""
        st.session_state.page = "WhatsApp blast"
        st.session_state[f"blast-numbers-{st.session_state.get('blast_v', 0)}"] = "\n".join(phones)

    name = re.sub(r"[^A-Za-z0-9 ]+", "", job["query"]).strip() or "Google Maps"
    xlsx = io.BytesIO()
    table.drop(columns=["Rating"]).to_excel(xlsx, index=False, sheet_name="Google Maps")
    d1, d2, d3, d4 = st.columns(4)
    d1.download_button("⬇️ Download Excel", xlsx.getvalue(), f"{name}.xlsx", type="primary", on_click="ignore",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", disabled=not view)
    d2.download_button("⬇️ Download CSV", table.to_csv(index=False).encode("utf-8-sig"), f"{name}.csv", "text/csv",
                       on_click="ignore", disabled=not view)
    d3.button(f"📣 Send mobiles to WhatsApp blast ({len(mobiles)})", disabled=not mobiles, on_click=to_blast,
              args=(mobiles,), help="Opens WhatsApp blast with the mobile numbers shown filled in (landlines "
                                    "can't get WhatsApp)")
    if d4.button("🗑 Delete this search", disabled=job.get("state") == "running"):
        gmaps.delete_job(db, pick)
        st.session_state.pop("gmaps_pick", None)
        st.rerun()


if tool == "PropertyGuru posting":
    propertyguru_posting_page()
    st.stop()
if tool == "Google Map Extractor":
    gmaps_page()
    st.stop()
if tool == "AI call":
    ai_call_page()
    st.stop()
if tool == "WhatsApp blast":
    whatsapp_blast_page()
    st.stop()
if tool == "Chat bot":
    chat_bot_page()
    st.stop()
if tool == "Condo prices":
    condo_prices_page()
    st.stop()
if tool == "WhatsApp listing":
    whatsapp_page()
    st.stop()
if tool == "Phone filter":
    phone_filter_page()
    st.stop()
if tool == "Price filter":
    price_filter_page()
    st.stop()


# ---------- data ----------
# Only what the page shows / downloads - the long description, photos and amenity lists made loading slow
LISTING_COLS = ("listing_id,search_key,title,headline,url,address,postcode,district,area,region,nearest_station,"
                "price_rm,initial_price_rm,previous_price_rm,price_changed_at,psf_rm,floor_area_sqft,bedrooms,"
                "bathrooms,property_type,furnishing,tenure,title_type,build_year,completed_year,developer,"
                "agent_name,agency_id,posted_on,first_seen,last_seen,details_scraped_at,latitude,longitude,status")


@st.cache_data(ttl=600, show_spinner="Loading listings…")
def load_listings(_db, search_key):
    """One category's listings. Supabase gives max 1000 rows per request, so the batches are fetched side by side."""
    from concurrent.futures import ThreadPoolExecutor

    def query(cols, **kw):
        q = _db.table("listings").select(cols, **kw)
        if search_key == "kl-rent-8k":  # older rows have no search_key - they came from this first search
            return q.or_(f"search_key.eq.{search_key},search_key.is.null")
        return q.eq("search_key", search_key)

    size = 1000
    total = query("listing_id", count="exact").limit(1).execute().count or 0
    with ThreadPoolExecutor(8) as pool:
        batches = pool.map(lambda start: query(LISTING_COLS).order("listing_id").range(start, start + size - 1)
                           .execute().data, range(0, total, size))
        rows = [r for b in batches for r in b]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ["first_seen", "last_seen", "price_changed_at"]:
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    for c in ["price_rm", "initial_price_rm", "previous_price_rm", "psf_rm", "floor_area_sqft", "bedrooms",
              "bathrooms", "latitude", "longitude"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Furnishing etc. only come from the listing's own page; make "not opened yet" explicit
    df["search_key"] = df["search_key"].fillna("kl-rent-8k")
    pending = df.details_scraped_at.isna() & (df.search_key == "kl-rent-8k")  # only this search opens listing pages
    df.loc[pending, "furnishing"] = "Pending (details not scraped yet)"
    df.loc[~pending, "furnishing"] = df.loc[~pending, "furnishing"].fillna("Not specified")
    return df


# Listing categories (scraper.py SEARCHES keys)
CATEGORIES = {
    "Homes for rent (RM 8k+)": ("kl-rent-8k", "rent"),
    "Commercial for rent (RM 8k+)": ("kl-commercial-rent-8k", "rent"),
    "Homes for sale (RM 3M+)": ("kl-sale-3m", "sale"),
    "Commercial for sale (RM 3M+)": ("kl-commercial-sale-3m", "sale"),
}


with st.sidebar:
    if st.button("Refresh data"):
        load_listings.clear()
        st.rerun()

st.title("KL Tracker")

# ---------- filters (across the top of the page) ----------
category = st.radio("Category", list(CATEGORIES), horizontal=True)
cat_key, cat_kind = CATEGORIES[category]
df = load_listings(db, cat_key)
if df.empty:
    st.info("No listings in this category yet - they appear after the next scrape.")
    st.stop()
with st.container(border=True):
    r1 = st.columns([1, 2, 2])
    active_days = r1[0].slider("Seen in the last N days", 1, 60, 3,
                               help="Listings not seen recently were probably taken down.")
    pmin, pmax = int(df.price_rm.min()), int(df.price_rm.max())
    if cat_kind == "rent":
        price = r1[1].slider("Monthly rent (RM)", pmin, max(pmax, pmin + 1),
                             (pmin, min(max(pmax, pmin + 1), 50000)), step=500, key=f"price-{cat_key}")
    else:
        price = r1[1].slider("Price (RM)", pmin, max(pmax, pmin + 1), (pmin, max(pmax, pmin + 1)), step=100_000,
                             key=f"price-{cat_key}", format="%d")
    text = r1[2].text_input("Search name / address")
    r2 = st.columns(4)
    districts = r2[0].multiselect("District", sorted(df.district.dropna().unique()), key=f"dist-{cat_key}")
    beds = r2[1].multiselect("Bedrooms", sorted(df.bedrooms.dropna().astype(int).unique()), key=f"beds-{cat_key}")
    types = r2[2].multiselect("Property type", sorted(df.property_type.dropna().unique()), key=f"type-{cat_key}")
    furnish = r2[3].multiselect("Furnishing", sorted(df.furnishing.dropna().unique()), key=f"furn-{cat_key}")

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
price_word = "rent" if cat_kind == "rent" else "price"
k1, k2, k3, k4 = st.columns(4)
k1.metric("Listings", f"{len(f):,}")
k2.metric(f"Median {price_word}", f"RM {f.price_rm.median():,.0f}" if len(f) else "–")
k3.metric("Median RM psf", f"{f.psf_rm.median():.2f}" if f.psf_rm.notna().any() else "–")
k4.metric("New in last 7 days", f"{(f.first_seen >= datetime.now(timezone.utc) - timedelta(days=7)).sum():,}")

# Clickable property name: the name rides in the URL fragment and display_text shows it
f = f.assign(property_link=f.url.fillna("") + "#" + f.title.fillna("Listing"))
COLS = {"property_link": "Property", "price_rm": "Rent (RM)" if cat_kind == "rent" else "Price (RM)",
        "psf_rm": "RM psf", "floor_area_sqft": "Sqft",
        "bedrooms": "Beds", "bathrooms": "Baths", "property_type": "Type", "furnishing": "Furnishing",
        "district": "District", "area": "Area", "nearest_station": "Nearest station",
        "posted_on": "Posted", "agent_name": "Agent"}
LINK = {"Property": st.column_config.LinkColumn("Property", display_text=r"^[^#]*#(.*)$")}

tab_list, tab_map, tab_drops, tab_district = st.tabs(["Listings", "Map", "Price changes", "By district"])

with tab_list:
    PER_PAGE = 10
    pages_n = max(1, -(-len(f) // PER_PAGE))
    # back to page 1 whenever the filters change
    sig = (cat_key, active_days, price, tuple(districts), tuple(beds), tuple(types), tuple(furnish), text)
    if st.session_state.get("lst-sig") != sig:
        st.session_state["lst-sig"], st.session_state["lst-page"] = sig, 1
    st.session_state["lst-page"] = min(st.session_state.get("lst-page", 1), pages_n)

    def turn(step):
        st.session_state["lst-page"] = min(max(1, st.session_state["lst-page"] + step), pages_n)

    page_no = st.session_state["lst-page"]
    first = (page_no - 1) * PER_PAGE
    st.dataframe(f.sort_values("price_rm").iloc[first:first + PER_PAGE][list(COLS)].rename(columns=COLS),
                 column_config=LINK, hide_index=True, width="stretch")
    p1, p2, p3, p4, p5 = st.columns([1, 1, 2, 1, 1], vertical_alignment="center")
    p1.button("⏮ First", on_click=turn, args=(-pages_n,), disabled=page_no == 1, width="stretch")
    p2.button("◀ Back", on_click=turn, args=(-1,), disabled=page_no == 1, width="stretch")
    p3.markdown(f"<div style='text-align:center'>Page <b>{page_no}</b> of {pages_n:,} · listings "
                f"{first + 1 if len(f) else 0:,}–{min(first + PER_PAGE, len(f)):,} of {len(f):,}</div>",
                unsafe_allow_html=True)
    p4.button("Next ▶", on_click=turn, args=(1,), disabled=page_no == pages_n, width="stretch")
    p5.button("Last ⏭", on_click=turn, args=(pages_n,), disabled=page_no == pages_n, width="stretch")
    st.download_button("Download CSV (all filtered)",
                       lambda: f.drop(columns="property_link").to_csv(index=False).encode("utf-8-sig"),
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
