"""
BMS Avengers Doomsday Theatre Tracker
Checks BookMyShow every N minutes for new Bengaluru theatres going live
for Avengers: Doomsday on December 17, 2026.
Sends email alert when a new theatre is detected.
"""

import os
import json
import time
import smtplib
import logging
import re
import hashlib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TARGET_DATE   = "20261217"          # Dec 17 2026
CITY          = "bengaluru"
MOVIE_KEYWORD = "avengers-doomsday" # used to filter showtimes

# How often to poll (seconds). 600 = 10 minutes.
POLL_INTERVAL = 600

# State file — persists known theatres between runs
STATE_FILE = Path(__file__).parent / "known_theatres.json"
LOG_FILE   = Path(__file__).parent / "tracker.log"

# ─── EMAIL CONFIG  (edit these or set as env vars) ────────────────────────────
EMAIL_SENDER   = os.getenv("BMS_EMAIL_SENDER",   "your_gmail@gmail.com")
EMAIL_PASSWORD = os.getenv("BMS_EMAIL_PASSWORD", "your_app_password")
EMAIL_TO       = os.getenv("BMS_EMAIL_TO",       "your_email@gmail.com")
SMTP_HOST      = os.getenv("BMS_SMTP_HOST",      "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("BMS_SMTP_PORT",  "587"))

# ─── REQUEST HEADERS ──────────────────────────────────────────────────────────
# BMS sits behind Cloudflare. A realistic browser UA + headers avoids 403s.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://in.bookmyshow.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bms_tracker")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def extract_initial_state(html: str) -> dict:
    """
    BMS inlines all data as window.__INITIAL_STATE__ = {...};
    Parse it out and return as a Python dict.
    """
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*(?:window|</script>)",
        html,
        re.DOTALL,
    )
    if not match:
        # Fallback: try grabbing up to the closing script tag
        match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*(\{.+)",
            html,
            re.DOTALL,
        )
        if not match:
            return {}
        raw = match.group(1)
        # Trim at last closing brace that balances
        depth, end = 0, 0
        for i, ch in enumerate(raw):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        raw = raw[:end]
    else:
        raw = match.group(1)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def fetch_html(url: str, retries: int = 3, backoff: float = 5.0) -> str | None:
    """GET a URL, return HTML text or None on failure."""
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            log.warning("HTTP %s for %s (attempt %d)", resp.status_code, url, attempt)
        except requests.RequestException as exc:
            log.warning("Request error for %s: %s (attempt %d)", url, exc, attempt)
        time.sleep(backoff * attempt)
    return None


def get_all_venues() -> list[dict]:
    """
    Fetch the Bengaluru cinemas listing page and extract venue list.
    Returns list of dicts: {VenueCode, VenueName, VenueAddress}
    """
    url  = f"https://in.bookmyshow.com/{CITY}/cinemas"
    html = fetch_html(url)
    if not html:
        log.error("Could not fetch cinemas listing page.")
        return []

    state = extract_initial_state(html)

    # Navigate into the nested state structure
    try:
        queries = (
            state
            .get("fetchVenuesListingApi", {})
            .get("queries", {})
        )
        # There's usually one key in queries
        venues_raw = []
        for key, val in queries.items():
            venues_raw = val.get("data", {}).get("venues", [])
            if venues_raw:
                break
    except (AttributeError, KeyError):
        venues_raw = []

    # Fallback: parse <a> tags directly from HTML if state parse fails
    if not venues_raw:
        log.warning("State parse failed for venues — falling back to HTML parse.")
        soup = BeautifulSoup(html, "html.parser")
        venues_raw = []
        for a in soup.select("a[href*='/buytickets/']"):
            href = a.get("href", "")
            # extract venue code from URL like /cinemas/bengaluru/pvr.../buytickets/PVFF/...
            m = re.search(r"/buytickets/([A-Z0-9]+)/", href)
            if m:
                venues_raw.append({
                    "VenueCode": m.group(1),
                    "VenueName": a.get_text(strip=True),
                    "VenueAddress": "",
                })

    venues = []
    seen   = set()
    for v in venues_raw:
        code = v.get("VenueCode") or v.get("venueCode", "")
        name = v.get("VenueName") or v.get("venueName", "")
        addr = v.get("VenueAddress") or v.get("venueAddress", "")
        if code and code not in seen:
            seen.add(code)
            venues.append({"code": code, "name": name, "address": addr})

    log.info("Found %d total venues in Bengaluru.", len(venues))
    return venues


def get_showtimes_for_venue(venue_code: str) -> list[dict]:
    """
    Fetch showtimes page for a specific venue on TARGET_DATE.
    Returns list of show dicts that match the movie keyword.
    """
    url  = (
        f"https://in.bookmyshow.com/cinemas/{CITY}/x"
        f"/buytickets/{venue_code}/{TARGET_DATE}"
    )
    html = fetch_html(url)
    if not html:
        return []

    state = extract_initial_state(html)

    # Key pattern: "getShowtimesByVenue-{VENUE_CODE}-{DATE}"
    query_key = f"getShowtimesByVenue-{venue_code}-{TARGET_DATE}"
    try:
        data = (
            state
            .get("venueShowtimesFunctionalApi", {})
            .get("queries", {})
            .get(query_key, {})
            .get("data", {})
        )
    except AttributeError:
        data = {}

    if not data:
        # Also try without the date — some builds use a slightly different key
        for k, v in (
            state.get("venueShowtimesFunctionalApi", {})
            .get("queries", {})
            .items()
        ):
            if venue_code in k:
                data = v.get("data", {})
                break

    shows = []

    # data.ShowDatesArray contains the date list; we care about data.ShowDetails
    # or data.shows depending on BMS version.
    show_details = (
        data.get("ShowDetails")
        or data.get("shows")
        or data.get("showDetails")
        or []
    )

    for show in show_details:
        event_code  = show.get("EventCode", "")
        event_title = show.get("EventTitle") or show.get("eventTitle") or ""
        # Match by keyword in title or event code
        if (
            MOVIE_KEYWORD.replace("-", " ").lower() in event_title.lower()
            or "avengers" in event_title.lower()
            or "doomsday" in event_title.lower()
        ):
            # Collect showtime info
            show_times = show.get("ShowTimes") or show.get("showTimes") or []
            shows.append({
                "event_code":  event_code,
                "event_title": event_title,
                "show_times":  show_times,
            })

    return shows


def check_via_search_url() -> dict[str, dict]:
    """
    Alternative: hit BMS's movie-specific listing for Bengaluru directly.
    Returns dict keyed by venue_code → {name, address, shows}
    """
    # BMS search / movie page for the specific movie in bengaluru
    # We try multiple URL patterns as BMS may assign different slugs
    candidate_urls = [
        f"https://in.bookmyshow.com/{CITY}/movies/avengers-doomsday/ET00",
        f"https://in.bookmyshow.com/{CITY}/movies/avengers-doomsday",
        # Broad movie search page
        f"https://in.bookmyshow.com/{CITY}/movies?category=MV",
    ]

    theatres = {}

    for url in candidate_urls:
        html = fetch_html(url)
        if not html:
            continue

        state = extract_initial_state(html)
        if not state:
            continue

        # Look for venue/showtime combos anywhere in the state tree
        raw_json = json.dumps(state)
        # Find all VenueCode patterns
        codes = re.findall(r'"VenueCode"\s*:\s*"([A-Z0-9]{3,6})"', raw_json)
        names = re.findall(r'"VenueName"\s*:\s*"([^"]+)"', raw_json)

        for i, code in enumerate(codes):
            name = names[i] if i < len(names) else code
            if code not in theatres:
                theatres[code] = {"name": name, "url": url, "shows": []}

        if theatres:
            log.info("Search URL yielded %d venues: %s", len(theatres), url)
            break

    return theatres


# ─── STATE MANAGEMENT ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"known_venues": {}, "last_check": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ─── EMAIL ────────────────────────────────────────────────────────────────────

def send_email(new_venues: list[dict]) -> None:
    if not new_venues:
        return

    subject = (
        f"🎬 BMS Alert: {len(new_venues)} new theatre(s) live for "
        f"Avengers: Doomsday Dec 17 — Bengaluru"
    )

    # Build HTML body
    rows = ""
    for v in new_venues:
        name    = v.get("name", "Unknown")
        code    = v.get("code", "")
        addr    = v.get("address", "")
        bms_url = (
            f"https://in.bookmyshow.com/cinemas/{CITY}/x"
            f"/buytickets/{code}/{TARGET_DATE}"
        )
        rows += f"""
        <tr>
          <td style="padding:8px 12px; font-weight:bold; color:#e50914;">{name}</td>
          <td style="padding:8px 12px; color:#555;">{addr}</td>
          <td style="padding:8px 12px;">
            <a href="{bms_url}"
               style="background:#e50914;color:#fff;padding:6px 14px;
                      border-radius:4px;text-decoration:none;font-size:13px;">
              Book Now
            </a>
          </td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;">
      <div style="background:#0d0d0d;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h1 style="color:#e50914;margin:0;font-size:22px;">
          🎬 Avengers: Doomsday — New Theatres Live!
        </h1>
        <p style="color:#aaa;margin:6px 0 0;">
          Bengaluru &nbsp;·&nbsp; December 17, 2026
        </p>
      </div>
      <div style="border:1px solid #ddd;border-top:none;padding:20px 24px;">
        <p style="color:#333;">
          <strong>{len(new_venues)}</strong> theatre(s) just opened bookings for
          <strong>Avengers: Doomsday</strong> on Dec 17 in Bengaluru.
          Grab your seats before they fill up!
        </p>
        <table style="width:100%;border-collapse:collapse;margin-top:12px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;color:#333;">Theatre</th>
              <th style="padding:8px 12px;text-align:left;color:#333;">Location</th>
              <th style="padding:8px 12px;text-align:left;color:#333;">Book</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="margin-top:20px;font-size:12px;color:#999;">
          Detected at {datetime.now().strftime("%d %b %Y, %I:%M %p IST")} by BMS Tracker.
        </p>
      </div>
    </html></body>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, [EMAIL_TO], msg.as_string())
        log.info("Email sent for %d new venue(s).", len(new_venues))
    except smtplib.SMTPException as exc:
        log.error("Failed to send email: %s", exc)


# ─── CORE SCAN ────────────────────────────────────────────────────────────────

def scan_for_doomsday_theatres() -> dict[str, dict]:
    """
    Returns dict of venue_code → venue_info for every theatre
    that currently has Avengers: Doomsday showing on Dec 17.
    """
    live_venues: dict[str, dict] = {}

    # Step 1: Get all Bengaluru venues
    all_venues = get_all_venues()

    if not all_venues:
        # Fallback: try movie-specific search page
        log.info("Venue list empty — trying movie search URL fallback.")
        search_venues = check_via_search_url()
        for code, info in search_venues.items():
            live_venues[code] = info
        return live_venues

    # Step 2: Check each venue for Dec 17 Doomsday shows
    log.info("Checking %d venues for Dec 17 showtimes...", len(all_venues))

    for idx, v in enumerate(all_venues, 1):
        code = v["code"]
        log.debug("[%d/%d] Checking %s (%s)", idx, len(all_venues), v["name"], code)

        shows = get_showtimes_for_venue(code)
        if shows:
            log.info("  ✓ LIVE: %s — %d show(s) found.", v["name"], len(shows))
            live_venues[code] = {
                "code":    code,
                "name":    v["name"],
                "address": v["address"],
                "shows":   shows,
            }

        # Polite delay between requests — avoid rate limiting
        time.sleep(1.5)

    return live_venues


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def main(once: bool = False) -> None:
    log.info("=" * 60)
    log.info("BMS Avengers Doomsday Tracker started.")
    log.info("Target: Bengaluru / Dec 17 2026")
    log.info("Poll interval: %ds", POLL_INTERVAL)
    log.info("Alert email → %s", EMAIL_TO)
    log.info("=" * 60)

    state = load_state()

    while True:
        log.info("── Scan starting at %s ──", datetime.now().strftime("%H:%M:%S"))

        try:
            live = scan_for_doomsday_theatres()
        except Exception as exc:
            log.error("Unexpected error during scan: %s", exc, exc_info=True)
            live = {}

        known = state.get("known_venues", {})

        # Find genuinely new theatres
        new_venues = []
        for code, info in live.items():
            if code not in known:
                new_venues.append(info)
                log.info("NEW THEATRE: %s (%s)", info["name"], code)

        if new_venues:
            send_email(new_venues)
            # Update state
            for v in new_venues:
                known[v["code"]] = {
                    "name":        v["name"],
                    "address":     v["address"],
                    "detected_at": datetime.now().isoformat(),
                }
        else:
            log.info("No new theatres. Currently live: %d", len(live))

        state["known_venues"] = known
        state["last_check"]   = datetime.now().isoformat()
        save_state(state)

        if once:
            log.info("--once flag set, exiting after single scan.")
            break
        log.info("Next check in %d seconds.\n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan then exit (used by CI/GitHub Actions).",
    )
    args = parser.parse_args()
    main(once=args.once)
