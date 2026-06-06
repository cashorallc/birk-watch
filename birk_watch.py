#!/usr/bin/env python3
"""
birk_watch.py — Birkenstock bulk-deal monitor (v2)

Polls liquidation/wholesale sites for Birkenstock listings and pings your phone
the moment a NEW one shows up. Remembers what it has already seen so you only
get alerted on fresh listings, not the same ones over and over.

WHAT CHANGED IN v2
------------------
- All sites now load through a real headless browser (gets past 403 blocks &
  JavaScript pages). Requires Playwright (see QUICK START).
- Listing detection is now per-site: instead of hunting for the word
  "birkenstock" in link text (which missed real listings), it finds the actual
  product/auction cards on each site by their link pattern, and reads the title
  from the link text, the product image's alt text, or aria-label — whichever
  exists. This is why v1 returned 0s and v2 should not.

QUICK START
-----------
1) pip install requests beautifulsoup4 playwright
2) playwright install chromium
3) Set NTFY_TOPIC below to the topic you subscribed to in the ntfy phone app.
4) Record what's already there (no alerts):  python birk_watch.py --baseline
5) Run forever:                              python birk_watch.py
   (or schedule `python birk_watch.py --once` with Task Scheduler / cron)

NOTIFICATIONS: set NOTIFY to "ntfy" (easiest), "telegram", or "email" below.
"""

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================================
# CONFIG — edit this block
# ============================================================================

CHECK_MINUTES = 20            # how often to poll when running in loop mode

# Settings can come from environment variables (used by the cloud / GitHub
# Actions setup) and fall back to the values written here for local use.

# --- Notifier: set NOTIFY to "ntfy", "telegram", or "email" ---
NOTIFY = os.environ.get("NOTIFY", "ntfy")

# ntfy settings  (install the free "ntfy" app, subscribe to this exact topic)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "birk-CHANGEME-picksomethingrandom")  # <-- change this!
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# telegram settings (only used if NOTIFY == "telegram")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# email settings (only used if NOTIFY == "email")
EMAIL_SMTP = os.environ.get("EMAIL_SMTP", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER", "you@gmail.com")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "your-app-password")
EMAIL_TO = os.environ.get("EMAIL_TO", "you@gmail.com")

STATE_FILE = Path(__file__).parent / "birk_seen.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ============================================================================
# SITES
# Each site's search URL is already filtered to Birkenstock, so EVERY product
# card on the page is relevant. We identify product cards by a URL fragment that
# only appears on listing/detail links ("link_contains"), and skip the search
# page's own URL plus obvious navigation ("exclude").
#
#   link_contains : list of substrings that mark a real listing link
#   exclude       : list of substrings that mark junk/nav links to ignore
#   require_kw    : if True, the title must also contain a keyword (use only for
#                   un-filtered pages); pre-filtered pages use False.
# ============================================================================

KEYWORDS = ["birkenstock", "birk"]

SITES = [
    {
        "name": "eBay lots",
        "url": "https://www.ebay.com/sch/i.html?_nkw=birkenstock+lot+wholesale&_sop=10",
        "render": True,
        "link_contains": ["/itm/"],
        "exclude": [],
        "require_kw": False,
    },
    {
        "name": "888 Lots",
        "url": "https://888lots.com/items?brand=birkenstock",
        "render": True,
        "link_contains": ["/item/", "/items/"],
        "exclude": ["?brand=", "/items?", "/category"],
        "require_kw": False,
    },
    {
        "name": "Liquidation.com",
        "url": "https://www.liquidation.com/auction/search?searchparam_words=birkenstock",
        "render": True,
        "link_contains": ["/auction/view", "/auctiondetails", "/auction/details"],
        "exclude": ["/auction/search"],
        "require_kw": False,
    },
    {
        "name": "B-Stock HSN",
        "url": "https://bstock.com/hsn/search/?q=birkenstock",
        "render": True,
        "link_contains": ["/auction/", "/listing/"],
        "exclude": ["/search", "/faq", "/help"],
        "require_kw": True,   # B-Stock search can show mixed brands
    },
]

JUNK_TITLES = {
    "shop on ebay", "see all", "view all", "sign in", "register", "help",
    "advanced", "watch", "add to cart", "buy it now", "details", "view details",
    "see details", "bid now", "place bid", "", "more",
}

# ============================================================================
# Fetchers
# ============================================================================

def fetch_plain(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_rendered(url):
    """Headless-browser fetch for JS-rendered / bot-protected pages."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)   # let listings render
        html = page.content()
        browser.close()
    return html


# ============================================================================
# Parsing
# ============================================================================

def _best_title(anchor):
    """Most descriptive text for a listing: the anchor text, else its image's
    alt text, else an aria-label / title attribute."""
    txt = " ".join(anchor.get_text(" ", strip=True).split())
    if txt and len(txt) >= 6:
        return txt[:200]
    img = anchor.find("img")
    if img:
        for attr in ("alt", "title"):
            v = (img.get(attr) or "").strip()
            if len(v) >= 6:
                return v[:200]
    for attr in ("aria-label", "title"):
        v = (anchor.get(attr) or "").strip()
        if len(v) >= 6:
            return v[:200]
    return ""


def extract_listings(html, site):
    base = site["url"]
    link_contains = site.get("link_contains", [])
    exclude = site.get("exclude", [])
    require_kw = site.get("require_kw", False)

    soup = BeautifulSoup(html, "html.parser")
    found = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        low_href = href.lower()

        if link_contains and not any(p in low_href for p in link_contains):
            continue
        if any(x in low_href for x in exclude):
            continue

        title = _best_title(a)
        if not title or title.lower() in JUNK_TITLES:
            continue
        if require_kw and not any(k in title.lower() for k in KEYWORDS):
            continue

        link = href
        if link.startswith("/"):
            m = re.match(r"(https?://[^/]+)", base)
            if m:
                link = m.group(1) + link
        found[link] = title

    items = [{"title": t, "link": l} for l, t in found.items()]

    # Fallback: if the per-site pattern matched nothing, try the broad
    # keyword-in-text method so we degrade gracefully instead of returning 0.
    if not items:
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text(" ", strip=True).split())
            if len(text) >= 8 and any(k in text.lower() for k in KEYWORDS):
                link = a["href"]
                if link.startswith("/"):
                    m = re.match(r"(https?://[^/]+)", base)
                    if m:
                        link = m.group(1) + link
                found[link] = text[:200]
        items = [{"title": t, "link": l} for l, t in found.items()]

    return items


def listing_id(site_name, item):
    raw = f"{site_name}|{item['link']}|{item['title']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ============================================================================
# Notifications
# ============================================================================

def notify(title, message, link=None):
    if NOTIFY == "ntfy":
        _notify_ntfy(title, message, link)
    elif NOTIFY == "telegram":
        _notify_telegram(title, message, link)
    elif NOTIFY == "email":
        _notify_email(title, message, link)
    else:
        print(f"[notify-disabled] {title}: {message} {link or ''}")


def _notify_ntfy(title, message, link):
    headers = {"Title": title}
    if link:
        headers["Click"] = link
        headers["Actions"] = f"view, Open listing, {link}"
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    except Exception as e:
        print(f"  ! ntfy failed: {e}")


def _notify_telegram(title, message, link):
    text = f"*{title}*\n{message}"
    if link:
        text += f"\n{link}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:
        print(f"  ! telegram failed: {e}")


def _notify_email(title, message, link):
    import smtplib
    from email.mime.text import MIMEText
    body = message + (f"\n\n{link}" if link else "")
    msg = MIMEText(body)
    msg["Subject"] = title
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT) as s:
            s.starttls()
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
    except Exception as e:
        print(f"  ! email failed: {e}")


# ============================================================================
# State
# ============================================================================

def load_seen():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


# ============================================================================
# Main check
# ============================================================================

def check_all(first_run_silent=False):
    seen = load_seen()
    new_count = 0

    for site in SITES:
        name = site["name"]
        try:
            html = fetch_rendered(site["url"]) if site.get("render") else fetch_plain(site["url"])
        except Exception as e:
            print(f"[{name}] fetch error: {e}")
            continue

        items = extract_listings(html, site)
        print(f"[{name}] found {len(items)} listing(s)")

        for item in items:
            iid = listing_id(name, item)
            if iid in seen:
                continue
            seen.add(iid)
            new_count += 1
            if first_run_silent:
                continue
            print(f"  NEW -> {item['title']}")
            notify(
                title=f"New Birks on {name}",
                message=item["title"],
                link=item["link"],
            )

    save_seen(seen)
    if first_run_silent:
        print(f"Baseline saved ({len(seen)} listings remembered). "
              f"Future runs will only alert on new ones.")
    else:
        print(f"Done. {new_count} new listing(s) this check.")
    return new_count


def main():
    ap = argparse.ArgumentParser(description="Birkenstock bulk-deal monitor")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--baseline", action="store_true",
                    help="record current listings WITHOUT alerting (run first)")
    args = ap.parse_args()

    if args.baseline:
        check_all(first_run_silent=True)
        return
    if args.once:
        check_all()
        return

    print(f"Watching {len(SITES)} sites every {CHECK_MINUTES} min. Ctrl-C to stop.")
    while True:
        try:
            check_all()
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"loop error: {e}")
        time.sleep(CHECK_MINUTES * 60)


if __name__ == "__main__":
    main()
