#!/usr/bin/env python3
"""
Woonstad Rotterdam - vrije sector huurwoning watcher.

Checks https://www.woonstadrotterdam.nl/aanbod/vrije-sector-huurwoning
for new listings and emails you when one appears.

Run it on a schedule (cron / Task Scheduler) - see README.md.
"""

import json
import logging
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://www.woonstadrotterdam.nl/aanbod/vrije-sector-huurwoning"
STATE_FILE = Path(__file__).parent / "seen_listings.json"
LOG_FILE = Path(__file__).parent / "watcher.log"

# Log to a file directly (not just the console) so runs via pythonw.exe -
# which has no console at all - are still fully visible afterward.
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("woonstad")

# --- Email settings: fill these in, or set as environment variables ---
SMTP_HOST = os.environ.get("WOONSTAD_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("WOONSTAD_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("WOONSTAD_SMTP_USER", "your.email@gmail.com")
SMTP_PASS = os.environ.get("WOONSTAD_SMTP_PASS", "your-app-password")
NOTIFY_TO = os.environ.get("WOONSTAD_NOTIFY_TO", "your.email@gmail.com")


def fetch_listings():
    """Load the page in a headless browser and return {url: title} for each listing."""
    listings = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # Accept cookie banner if present. Consent banners often live inside
        # an iframe (OneTrust, Cookiebot, etc.), so check the main page AND
        # every iframe, and match by accessible role rather than exact text.
        import re as _re
        cookie_pattern = _re.compile(
            r"accepteren|akkoord|alles toestaan|sta (alle )?cookies toe|"
            r"accept all|alleen noodzakelijke cookies",
            _re.I,
        )
        dismissed = False
        for frame in page.frames:
            if dismissed:
                break
            try:
                btn = frame.get_by_role("button", name=cookie_pattern).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=2000)
                    dismissed = True
                    page.wait_for_timeout(1000)
            except Exception:
                pass
        if not dismissed:
            # Fall back to plain text search on the main page.
            for text in ["Accepteren", "Alles accepteren", "Akkoord", "Alleen noodzakelijke cookies"]:
                try:
                    page.get_by_text(text, exact=False).first.click(timeout=3000)
                    break
                except Exception:
                    pass

        # Give client-side rendering a moment to finish populating listings.
        page.wait_for_timeout(3000)

        # The site paginates via a "Toon meer" button. Scroll down and click
        # it repeatedly until it's no longer there (i.e. all listings loaded).
        selector = 'a[href*="/aanbod/vrije-sector-huurwoning/"]'
        stable_rounds = 0
        for _ in range(40):  # safety cap so this can't loop forever
            before_count = len(page.query_selector_all(selector))

            # Scroll to the bottom first so the button actually comes into view.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)

            clicked = False
            for text in ["Toon meer", "Meer tonen", "Laad meer", "Meer laden"]:
                try:
                    btn = page.get_by_text(text, exact=False).first
                    btn.scroll_into_view_if_needed(timeout=2000)
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        clicked = True
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            after_count = len(page.query_selector_all(selector))

            if not clicked and after_count == before_count:
                # Nothing new loaded and no button to click - give it one more
                # chance (some sites need two scrolls) before concluding we're done.
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0

        # Save a screenshot every run so you can visually sanity-check what
        # the script actually saw if the listing count ever looks wrong.
        page.screenshot(path=str(Path(__file__).parent / "debug.png"), full_page=True)

        # Listing links live under /aanbod/vrije-sector-huurwoning/<slug>.
        # This is the part most likely to need tweaking if the site changes.
        anchors = page.query_selector_all(selector)
        NON_LISTING_SLUGS = {"inschrijven"}
        for a in anchors:
            href = a.get_attribute("href") or ""
            if href.rstrip("/") == "/aanbod/vrije-sector-huurwoning":
                continue  # skip the overview link itself
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            if slug in NON_LISTING_SLUGS:
                continue  # skip known non-listing links (e.g. "apply now" CTA)
            full_url = href if href.startswith("http") else f"https://www.woonstadrotterdam.nl{href}"
            title = (a.inner_text() or "").strip().replace("\n", " ")
            if full_url not in listings or title:
                listings[full_url] = title or listings.get(full_url, "(no title found)")

        browser.close()
    return listings


def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(listings):
    STATE_FILE.write_text(
        json.dumps(listings, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def send_email(new_listings: dict):
    lines = ["New listing(s) found on Woonstad Rotterdam:\n"]
    for url, title in new_listings.items():
        lines.append(f"- {title}\n  {url}\n")
    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Woonstad Rotterdam: {len(new_listings)} new listing(s)"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())


def main():
    log.info("Run started")
    current = fetch_listings()

    if not current:
        log.warning("No listings found at all - the page structure may have "
                    "changed, or the site blocked the request. Skipping this run.")
        return

    seen = load_seen()

    if not seen:
        # First ever run: just record the baseline, don't notify.
        save_seen(current)
        log.info(f"Baseline saved with {len(current)} listing(s). "
                 f"Future runs will notify you of anything new.")
        return

    new_urls = set(current) - set(seen)
    if new_urls:
        new_listings = {u: current[u] for u in new_urls}
        log.info(f"Found {len(new_listings)} new listing(s):")
        for u, t in new_listings.items():
            log.info(f" - {t} ({u})")
        try:
            send_email(new_listings)
            log.info("Email sent.")
        except Exception as e:
            log.error(f"Failed to send email: {e}")
        # Save the full current set either way, so we don't re-notify next time.
        save_seen(current)
    else:
        log.info("No new listings.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Unhandled error during run")
