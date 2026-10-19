"""
fetch_registry.py
Downloads the Rinascente Lista Nozze page after logging in.
Reads credentials from env vars RINASCENTE_USER and RINASCENTE_PASS.
Saves the result as "Rinascente Lista Nozze.html" in the current directory.

Run locally:
    RINASCENTE_USER=451019980 RINASCENTE_PASS="Maria&Giuseppe27" python fetch_registry.py

In the GitHub Action it reads from secrets automatically.
"""

import os
import sys
from playwright.sync_api import sync_playwright

USER = os.environ.get("RINASCENTE_USER", "").strip()
PASS = os.environ.get("RINASCENTE_PASS", "").strip()
OUTPUT = "Rinascente Lista Nozze.html"

REGISTRY_URL = "https://www.rinascentelistanozze.it/ListaNozze/it/AreaDonatori/5697/Regali"

if not USER or not PASS:
    print("ERROR: set RINASCENTE_USER and RINASCENTE_PASS env vars")
    sys.exit(1)

print(f"Logging in as {USER} …")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )
    page = ctx.new_page()

    # Step 1: go to login page directly for the donor area
    LOGIN_URL = "https://www.rinascentelistanozze.it/ListaNozze/it/AreaDonatori"
    print(f"  Navigating to donor login page …")
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    print(f"  After goto: {page.url}")

    # Step 2: if we see a login form, fill it
    if "login" in page.url.lower() or "accedi" in page.url.lower() or page.query_selector('input[type="password"]'):
        print("  Filling login form …")
        page.fill('input[name="username"], input[type="email"], #username, #email', USER)
        page.fill('input[name="password"], input[type="password"], #password', PASS)
        page.click('button[type="submit"], input[type="submit"], .btn-login, .submit')
        page.wait_for_load_state("networkidle", timeout=20000)
        print(f"  After login: {page.url}")

        if "login" in page.url.lower() or "accedi" in page.url.lower():
            print("ERROR: login failed — check credentials or form selectors")
            browser.close()
            sys.exit(1)

    # Step 3: always navigate explicitly to the gift list page
    print(f"  Navigating to registry page …")
    page.goto(REGISTRY_URL, wait_until="networkidle", timeout=30000)
    print(f"  Page loaded: {page.url}")

    # Save full HTML
    content = page.content()
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(content)

    browser.close()

print(f"Saved → {OUTPUT}  ({len(content):,} bytes)")
