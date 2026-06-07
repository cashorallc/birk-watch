#!/usr/bin/env python3
"""
birk_watch.py — Bulk-deal monitor (v3)

Polls liquidation/wholesale sites for branded bulk lots and pings your phone
the moment a NEW one shows up. Remembers what it has already seen so you only
get alerted on fresh listings, not the same ones over and over.

WHAT'S NEW IN v3
----------------
- Watches MANY brands now, not just Birkenstock (see BRANDS below — edit freely).
- Strict NO-MIX filter (all sites): skips grab-bag lots. A listing is dropped if
  its title contains mix words ("& more", "assorted", "mixed", etc.) OR names 2+
  different brands (e.g. "Skechers, adidas, New Balance" = mixed pallet → skip).
- Minimum-units filter on 888 Lots only: skips lots under MIN_UNITS_888 units.
  (Other sites already sell true bulk, so no minimum is applied there.)

QUICK START
-----------
1) pip install requests beautifulsoup4 playwright
2) playwright install chromium
3) Set NTFY_TOPIC below to the topic you subscribed to in the ntfy phone app.
4) Record what's already there (no alerts):  python birk_watch.py --baseline
5) Run forever:                              python birk_watch.py
   (or schedule `python birk_watch.py --once` with the cloud / Task Scheduler)

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

MIN_UNITS_888 = 10            # 888 Lots only: ignore lots smaller than this

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
# BRANDS — what to hunt for. Each brand has its search aliases (used to match a
# listing title) and the per-site search term used in the URL. Add/remove freely.
# The "aliases" are also how we count distinct brands in a title for the no-mix
# filter, so keep each brand's aliases grouped under that one brand.
# ============================================================================

BRANDS = {
    "Birkenstock": ["birkenstock", "birk"],
    "Nike":        ["nike", "jordan", "air max", "air force"],
    "Adidas":      ["adidas", "ultraboost", "yeezy", "samba"],
    "Hoka":        ["hoka"],
    "On":          ["on cloud", "oncloud", "on running", "cloudmonster", "cloudnova"],
    "New Balance": ["new balance"],
    "UGG":         ["ugg"],
    "Champion":    ["champion"],
    "North Face":  ["north face", "the north face", "tnf"],
    "Carhartt":    ["carhartt"],
    "Michael Kors":["michael kors"],
    "Coach":       ["coach"],
    "Fanatics":    ["fanatics"],
}

# Mix / grab-bag indicator words. If a title contains any of these, skip it.
MIX_WORDS = [
    "& more", "and more", "assorted", "mixed", "variety", "unsorted",
    "misc", "multi-brand", "multi brand", "assorted brands", "various",
]

# ============================================================================
# SITES
# link_contains : substrings that mark a real listing link
# exclude       : substrings that mark junk/nav links to ignore
# is_888        : flag so the min-units filter applies only to 888 Lots
# ============================================================================

SITES = [
    {
        "name": "eBay lots",
        "url": "https://www.ebay.com/sch/i.html?_nkw=shoe+lot+wholesale&_sop=10",
        "render": True,
        "link_contains": ["/itm/"],
        "exclude": [],
        "is_888": False,
    },
    {
        "name": "888 Lots",
        "url": "https://888lots.com/items",
        "render": True,
        "link_contains": ["/item/", "/items/"],
        "exclude": ["?brand=", "/items?", "/category"],
        "is_888": True,
    },
    {
        "name": "Liquidation.com",
        "url": "https://www.liquidation.com/wholesale-clothing/shoes.html",
        "render": True,
        "link_contains": ["/auction/view", "/auctiondetails", "/auction/details"],
        "exclude": ["/auction/search"],
        "is_888": False,
    },
    {
        "name": "B-Stock Shoes",
        "url": "https://bstock.com/auctions/footwear-auctions/shoes/",
        "render": True,
        "link_contains": ["/auction/", "/listing/"],
        "exclude": ["/search", "/faq", "/help"],
        "is_888": False,
    },
]

JUNK_TITLES = {
    "shop on ebay", "see all", "view all", "sign in", "register", "help",
    "advanced", "watch", "add to cart", "buy it now", "details", "view details",
    "see details", "bid now", "place bid", "", "more",
}

# Precompile word-boundary regexes for each alias so short aliases like "on"
# don't match inside other words (e.g. "on running" must NOT match "cliftON RUNNING").
ALL_ALIASES = []
for _brand, _aliases in BRANDS.items():
    for _alias in _aliases:
        _pat = re.compile(r"\b" + re.escape(_alias) + r"\b", re.IGNORECASE)
        ALL_ALIASES.append((_brand, _pat))

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
        page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
    return html


# ============================================================================
# Title analysis — brand match, mix detection, unit count
# ============================================================================

def brands_in_title(title):
    """Return the set of distinct brands whose aliases appear in the title,
    matched on whole-word boundaries (so 'on' won't match inside 'clifton')."""
    found = set()
    for brand, pat in ALL_ALIASES:
        if pat.search(title):
            found.add(brand)
    return found


def is_mix(title, matched_brands):
    """A listing is a 'mix' (grab-bag) if it contains a mix word OR names 2+
    distinct brands we track."""
    low = title.lower()
    if any(w in low for w in MIX_WORDS):
        return True
    if len(matched_brands) >= 2:
        return True
    return False


def unit_count(title):
    """Best-effort pull of a unit/pair/piece count from a title.
    Looks for patterns like '40 units', '19 pairs', '124 pcs', 'lot of 30'.
    Returns an int, or None if no count is found."""
    low = title.lower().replace(",", "")
    patterns = [
        r"(\d+)\s*(?:units?|pairs?|pcs?|pieces?|ct\b|count)",
        r"lot of\s*(\d+)",
        r"(\d+)\s*-?\s*pack",
    ]
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


# ============================================================================
# Parsing
# ============================================================================

def _best_title(anchor):
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
    is_888 = site.get("is_888", False)

    soup = BeautifulSoup(html, "html.parser")
    found = {}

    candidates = []
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
        link = href
        if link.startswith("/"):
            m = re.match(r"(https?://[^/]+)", base)
            if m:
                link = m.group(1) + link
        candidates.append((link, title))

    # Fallback if the per-site link pattern matched nothing: scan all anchors
    # whose text mentions any tracked brand.
    if not candidates:
        for a in soup.find_all("a", href=True):
            title = " ".join(a.get_text(" ", strip=True).split())
            if len(title) < 8:
                continue
            if not brands_in_title(title):
                continue
            link = a["href"]
            if link.startswith("/"):
                m = re.match(r"(https?://[^/]+)", base)
                if m:
                    link = m.group(1) + link
            candidates.append((link, title[:200]))

    # Now apply the brand / mix / units filters.
    for link, title in candidates:
        matched = brands_in_title(title)
        if not matched:
            continue                      # no tracked brand → skip
        if is_mix(title, matched):
            continue                      # grab-bag / multi-brand → skip
        if is_888:
            n = unit_count(title)
            if n is not None and n < MIN_UNITS_888:
                continue                  # too small a lot on 888 Lots → skip
        found[link] = title

    return [{"title": t, "link": l} for l, t in found.items()]


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
        print(f"[{name}] found {len(items)} matching listing(s)")

        for item in items:
            iid = listing_id(name, item)
            if iid in seen:
                continue
            seen.add(iid)
            new_count += 1
            if first_run_silent:
                continue
            # Tag the alert with which brand(s) matched, for a cleaner heads-up.
            matched = ", ".join(sorted(brands_in_title(item["title"]))) or "Deal"
            print(f"  NEW [{matched}] -> {item['title']}")
            notify(
                title=f"{matched} bulk lot on {name}",
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
    ap = argparse.ArgumentParser(description="Bulk-deal monitor")
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

    print(f"Watching {len(SITES)} sites for {len(BRANDS)} brands every "
          f"{CHECK_MINUTES} min. Ctrl-C to stop.")
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
