import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.wg-gesucht.de"
SEARCH_URL = os.getenv(
    "WG_SEARCH_URL",
    "https://www.wg-gesucht.de/wg-zimmer/muenster",
)
SEEN_FILE = Path(os.getenv("SEEN_FILE", "seen_ads.json"))

# Pause between detail-page fetches so a batch of several new ads doesn't
# hit WG-Gesucht with back-to-back requests and trip the bot/captcha check.
DETAIL_FETCH_DELAY_SECONDS = 2

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Retries only cover idempotent methods (GET by default), so a lost/garbled
# response to the Telegram POST never gets silently resent.
SESSION = requests.Session()
_retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://", HTTPAdapter(max_retries=_retry))

AD_ID_RE = re.compile(r"\.(\d{6,12})\.html(?:$|[?#])")
# Matching the plain words "captcha" or "cloudflare" is unreliable on this
# site: normal WG-Gesucht pages embed a reCAPTCHA widget in the contact
# form, a hidden validation placeholder for it, and reference recaptcha.net
# in a cookie-consent script — all three contain "captcha" without any bot
# check being shown. The German confirmation phrase is what actually only
# appears on the real "are you human" interstitial.
BOT_CHECK_RE = re.compile(r"bitte bestätigen sie, dass sie ein mensch sind", re.I)
PRICE_RE = re.compile(r"(\d{2,5})\s*€")
SIZE_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*m²", re.I)
DATE_RE = re.compile(
    r"(?:ab|frei ab|verfügbar ab)\s*[:\-]?\s*"
    r"(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)",
    re.I,
)

MUNSTER_DISTRICTS = [
    "Centrum", "Altstadt", "Mauritz", "Kreuzviertel", "Hansaviertel",
    "Hafen", "Gievenbeck", "Sentrup", "Geist", "Mecklenbeck",
    "Roxel", "Wolbeck", "Hiltrup", "Kinderhaus", "Coerde",
    "Handorf", "Nienberge", "Aaseestadt", "Berg Fidel",
]


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return {str(x) for x in data}
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen: set[str]) -> None:
    ids = sorted(seen, key=int, reverse=True)[:5000]
    SEEN_FILE.write_text(
        json.dumps(ids, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = SESSION.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    response.raise_for_status()


def looks_like_offer(href: str) -> bool:
    # Check the path only: the domain itself is "wg-gesucht.de", so running
    # this against the full URL would match "-gesucht." on every single
    # link and exclude everything.
    low = urlparse(href).path.lower()

    if "-gesucht." in low or "/gesuche" in low:
        return False

    return "muenster" in low or "münster" in low


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def extract_district(text: str, url: str) -> str | None:
    # The URL slug is the most reliable source, e.g.
    # ...Muenster-Kreuzviertel.12345678.html
    m = re.search(r"Muenster-([A-Za-zÄÖÜäöüß\-]+)\.\d+\.html", url)
    if m:
        return m.group(1).replace("-", " ")

    # Fallback: match a known district as a whole word/phrase in the free
    # text. A plain substring check would also fire on ordinary German
    # words that happen to share a district's name (e.g. "Geist", "Hafen").
    for district in MUNSTER_DISTRICTS:
        pattern = r"\b" + re.escape(district) + r"\b"
        if re.search(pattern, text, re.I):
            return district

    return None


def extract_summary_fields(text: str, url: str) -> dict[str, str | None]:
    price = None
    size = None
    move_in = None

    price_match = PRICE_RE.search(text)
    if price_match:
        price = f"{price_match.group(1)} €"

    size_match = SIZE_RE.search(text)
    if size_match:
        size = f"{size_match.group(1).replace(',', '.')} m²"

    date_match = DATE_RE.search(text)
    if date_match:
        move_in = date_match.group(1)

    district = extract_district(text, url)

    return {
        "price": price,
        "size": size,
        "district": district,
        "move_in": move_in,
    }


def looks_like_bot_check(html: str) -> bool:
    return bool(BOT_CHECK_RE.search(html))


def fetch_ad_details(url: str) -> dict[str, str | None]:
    """
    Fetch the individual ad page and extract additional details.
    This is only called for newly discovered ads, so it adds very little load.
    """
    response = SESSION.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    html = response.text

    if looks_like_bot_check(html):
        return {}

    soup = BeautifulSoup(html, "html.parser")

    # Main visible text gives us a resilient fallback if WG-Gesucht
    # changes class names.
    page_text = normalize_text(soup.get_text(" ", strip=True))
    fields = extract_summary_fields(page_text, url)

    # Try to get a cleaner title from the page.
    title = None
    h1 = soup.find("h1")
    if h1:
        title = normalize_text(h1.get_text(" ", strip=True))

    if not title and soup.title:
        title = normalize_text(soup.title.get_text(" ", strip=True))
        title = re.sub(r"\s*\|\s*WG-Gesucht.*$", "", title, flags=re.I)

    fields["title"] = title

    return fields


def fetch_ads() -> list[dict[str, str | None]]:
    response = SESSION.get(
        SEARCH_URL,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    html = response.text

    if looks_like_bot_check(html):
        raise RuntimeError(
            "WG-Gesucht liefert momentan eine Bot-/Captcha-Prüfung."
        )

    soup = BeautifulSoup(html, "html.parser")
    ads: dict[str, dict[str, str | None]] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absolute_url = urljoin(BASE_URL, href)

        if urlparse(absolute_url).netloc not in {
            "wg-gesucht.de",
            "www.wg-gesucht.de",
        }:
            continue

        match = AD_ID_RE.search(absolute_url)
        if not match or not looks_like_offer(absolute_url):
            continue

        ad_id = match.group(1)

        # Use surrounding listing card text if possible.
        container = a
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent

        card_text = normalize_text(container.get_text(" ", strip=True))
        link_text = normalize_text(a.get_text(" ", strip=True))

        title = link_text or f"WG-Gesucht Anzeige {ad_id}"
        fields = extract_summary_fields(card_text, absolute_url)

        candidate = {
            "id": ad_id,
            "title": title,
            "url": absolute_url,
            **fields,
        }

        existing = ads.get(ad_id)
        if existing is None:
            ads[ad_id] = candidate
        else:
            # Prefer the version with more extracted information.
            current_score = sum(bool(existing.get(k)) for k in (
                "title", "price", "size", "district", "move_in"
            ))
            new_score = sum(bool(candidate.get(k)) for k in (
                "title", "price", "size", "district", "move_in"
            ))
            if new_score > current_score:
                ads[ad_id] = candidate

    if not ads:
        raise RuntimeError(
            "Keine Anzeigenlinks gefunden. "
            "Die Seitenstruktur oder Such-URL könnte sich geändert haben."
        )

    return list(ads.values())


def enrich_ad(ad: dict[str, str | None]) -> dict[str, str | None]:
    try:
        details = fetch_ad_details(str(ad["url"]))
    except Exception as exc:
        print(f"Details konnten nicht geladen werden: {exc}")
        return ad

    for key in ("title", "price", "size", "district", "move_in"):
        if details.get(key):
            ad[key] = details[key]

    return ad


def format_telegram_message(ad: dict[str, str | None]) -> str:
    lines = ["🚨 NEUE WG IN MÜNSTER", ""]

    if ad.get("district"):
        lines.append(f"📍 {ad['district']}")

    if ad.get("price"):
        lines.append(f"💰 {ad['price']}")

    if ad.get("size"):
        lines.append(f"📐 {ad['size']}")

    if ad.get("move_in"):
        lines.append(f"📅 frei ab {ad['move_in']}")

    if any(ad.get(k) for k in ("district", "price", "size", "move_in")):
        lines.append("")

    title = str(ad.get("title") or "WG-Gesucht Anzeige")
    lines.append(title)
    lines.append("")
    lines.append(f"👉 {ad['url']}")

    return "\n".join(lines)


def main() -> int:
    seen = load_seen()
    first_run = not SEEN_FILE.exists()

    ads = fetch_ads()
    current_ids = {str(ad["id"]) for ad in ads}

    print(f"{len(ads)} Anzeigen auf der Ergebnisseite gefunden.")

    if first_run:
        save_seen(current_ids)
        print(
            f"Erster Lauf: {len(current_ids)} bestehende Anzeigen gespeichert."
        )
        send_telegram(
            "🏠 WG-Watcher für Münster ist aktiv.\n"
            f"{len(current_ids)} bestehende Anzeigen wurden als bekannt gespeichert."
        )
        return 0

    new_ads = [
        ad for ad in ads
        if str(ad["id"]) not in seen
    ]
    new_ads.sort(key=lambda ad: int(str(ad["id"])))

    failed_ids: set[str] = set()

    for index, ad in enumerate(new_ads):
        ad_id = str(ad["id"])
        if index > 0:
            time.sleep(DETAIL_FETCH_DELAY_SECONDS)
        try:
            ad = enrich_ad(ad)
            message = format_telegram_message(ad)
            send_telegram(message)
            print(f"Telegram gesendet: {ad_id} | {ad.get('title')}")
        except Exception as exc:
            # Mark as failed instead of aborting, so ads that were already
            # sent successfully in this run aren't resent next time.
            print(f"FEHLER beim Senden von Anzeige {ad_id}: {exc}", file=sys.stderr)
            failed_ids.add(ad_id)
            continue

        seen.add(ad_id)
        save_seen(seen)

    # Everything visible now counts as seen, except ads we failed to send —
    # those stay unseen so they're retried on the next run.
    seen.update(current_ids - failed_ids)
    save_seen(seen)

    sent_count = len(new_ads) - len(failed_ids)
    print(f"{sent_count} neue Anzeige(n) gesendet, {len(failed_ids)} fehlgeschlagen.")

    return 1 if failed_ids else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        raise
