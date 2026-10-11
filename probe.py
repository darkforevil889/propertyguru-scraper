"""Open the search page in real Chrome and dump the HTML + embedded JSON for inspection."""
import json
from pathlib import Path

from patchright.sync_api import sync_playwright

URL = ("https://www.propertyguru.com.my/property-for-rent?listingType=rent&page=1"
       "&isCommercial=false&regionCode=58jok&_freetextDisplay=Kuala+Lumpur&minPrice=8000")
OUT = Path(__file__).parent / "debug"
OUT.mkdir(exist_ok=True)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(Path(__file__).parent / ".chrome-profile"),
        channel="chrome",
        headless=False,
        no_viewport=True,
    )
    page = ctx.new_page()
    resp = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    print("status:", resp.status if resp else None)
    # Give Cloudflare's check time to pass if it shows up
    for _ in range(30):
        if page.locator("#__NEXT_DATA__").count():
            break
        page.wait_for_timeout(1000)
    print("title:", page.title())
    html = page.content()
    (OUT / "search.html").write_text(html, encoding="utf-8")
    nd = page.locator("#__NEXT_DATA__")
    if nd.count():
        data = json.loads(nd.inner_text())
        (OUT / "search_next_data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("__NEXT_DATA__ saved")
    else:
        print("no __NEXT_DATA__ found")
    page.screenshot(path=str(OUT / "search.png"))
    ctx.close()
