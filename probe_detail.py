"""Open one listing detail page and dump its embedded JSON for inspection."""
import json
import sys
from pathlib import Path

from patchright.sync_api import sync_playwright

URL = sys.argv[1]
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
    for _ in range(30):
        if page.locator("#__NEXT_DATA__").count():
            break
        page.wait_for_timeout(1000)
    print("title:", page.title())
    nd = page.locator("#__NEXT_DATA__")
    if nd.count():
        data = json.loads(nd.inner_text())
        (OUT / "detail_next_data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("__NEXT_DATA__ saved")
    ctx.close()
