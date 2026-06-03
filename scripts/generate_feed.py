#!/usr/bin/env python3
"""
Magic: The Gathering Spoiler RSS Feed Generator
Fetches new cards from Scryfall API and generates an RSS 2.0 feed.
Duplicates are tracked via oracle_id.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
import urllib.request
import urllib.parse
import urllib.error

# --- Configuration ---
SCRYFALL_SEARCH_URL = "https://api.scryfall.com/cards/search"
LOOKBACK_DAYS = 14
MAX_FEED_ENTRIES = 250
DATA_FILE = Path(__file__).parent.parent / "data" / "known_cards.json"
OUTPUT_FILE = Path(__file__).parent.parent / "docs" / "feed.xml"
FEED_TITLE = "Magic: The Gathering – Neue Karten & Spoiler"
FEED_DESCRIPTION = (
    "Automatisch generierter RSS Feed für neu gespoilerte und "
    "veröffentlichte Magic: The Gathering Karten via Scryfall."
)
FEED_LINK = "https://scryfall.com"
FEED_LANGUAGE = "de-de"


# --- Helpers ---

def fetch_json(url: str) -> dict:
    """Fetch JSON from a URL with a polite delay and User-Agent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "mtg-spoiler-rss/1.0 (github-actions; https://github.com)",
            "Accept": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_known_cards() -> dict:
    """Load oracle_id -> first_seen mapping from disk."""
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known_cards(known: dict) -> None:
    """Persist oracle_id tracking data to disk."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(known, f, indent=2, ensure_ascii=False)


def card_image_url(card: dict) -> str | None:
    """Extract the best available image URL from a card object."""
    images = card.get("image_uris", {})
    if images:
        return images.get("normal") or images.get("large") or images.get("small")
    faces = card.get("card_faces", [])
    if faces:
        face_images = faces[0].get("image_uris", {})
        return face_images.get("normal") or face_images.get("large")
    return None


def card_oracle_text(card: dict) -> str:
    """Get oracle text, handling double-faced cards."""
    text = card.get("oracle_text")
    if text:
        return text
    faces = card.get("card_faces", [])
    if faces:
        parts = [f.get("oracle_text", "") for f in faces if f.get("oracle_text")]
        return "\n//\n".join(parts)
    return ""


def card_sort_date(card: dict) -> str:
    """Return the best date string for sorting."""
    return (
        card.get("preview", {}).get("previewed_at")
        or card.get("released_at")
        or "1970-01-01"
    )


def build_rss_item(card: dict) -> Element:
    """Build a single <item> element for a card."""
    item = Element("item")

    name = card.get("name", "Unknown Card")
    scryfall_uri = card.get("scryfall_uri", "https://scryfall.com")
    set_name = card.get("set_name", "")
    mana_cost = card.get("mana_cost", "")
    type_line = card.get("type_line", "")
    oracle_text = card_oracle_text(card)
    rarity = card.get("rarity", "").capitalize()
    image_url = card_image_url(card)

    pub_date_str = card_sort_date(card)
    try:
        pub_dt = datetime.fromisoformat(pub_date_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pub_dt = datetime.now(timezone.utc)

    title_text = name
    if set_name:
        title_text += f" [{set_name}]"
    SubElement(item, "title").text = title_text
    SubElement(item, "link").text = scryfall_uri

    oracle_id = card.get("oracle_id") or card.get("id", name)
    guid = SubElement(item, "guid", isPermaLink="false")
    guid.text = oracle_id

    SubElement(item, "pubDate").text = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    desc_parts = []
    if image_url:
        desc_parts.append(f'<img src="{image_url}" alt="{name}" style="max-width:300px"/>')
    desc_parts.append(f"<p><strong>{name}</strong></p>")
    if mana_cost:
        desc_parts.append(f"<p>Manakosten: {mana_cost}</p>")
    if type_line:
        desc_parts.append(f"<p>Typ: {type_line}</p>")
    if rarity:
        desc_parts.append(f"<p>Seltenheit: {rarity}</p>")
    if set_name:
        desc_parts.append(f"<p>Set: {set_name}</p>")
    if oracle_text:
        formatted = oracle_text.replace("\n", "<br/>")
        desc_parts.append(f"<p>{formatted}</p>")
    desc_parts.append(f'<p><a href="{scryfall_uri}">Auf Scryfall ansehen</a></p>')

    SubElement(item, "description").text = "\n".join(desc_parts)

    if image_url:
        SubElement(item, "enclosure", url=image_url, type="image/jpeg", length="0")

    return item


def build_rss_feed(items: list[Element], build_time: datetime) -> str:
    """Assemble the full RSS 2.0 document."""
    rss = Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")

    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = FEED_TITLE
    SubElement(channel, "link").text = FEED_LINK
    SubElement(channel, "description").text = FEED_DESCRIPTION
    SubElement(channel, "language").text = FEED_LANGUAGE
    SubElement(channel, "lastBuildDate").text = build_time.strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    SubElement(channel, "ttl").text = "120"

    atom_link = SubElement(channel, "atom:link")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    atom_link.set("href", "https://wombart.github.io/mtg-spoiler-rss/feed.xml")

    for item in items:
        channel.append(item)

    raw = tostring(rss, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


def fetch_all_pages(query: str) -> list[dict]:
    """Fetch all pages for a single Scryfall search query."""
    params = urllib.parse.urlencode({
        "q": query,
        "order": "released",
        "dir": "desc",
        "unique": "cards",
    })
    url = f"{SCRYFALL_SEARCH_URL}?{params}"

    cards = []
    page = 1
    while url:
        print(f"  Page {page}: {url[:80]}...")
        try:
            data = fetch_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  No results (404), skipping.")
            else:
                print(f"  HTTP {e.code}: {e.reason}", file=sys.stderr)
            break
        except Exception as e:
            print(f"  Error on page {page}: {e}", file=sys.stderr)
            break

        cards.extend(data.get("data", []))
        url = data.get("next_page")
        page += 1
        if url:
            time.sleep(0.12)  # stay well under 10 req/s

    return cards


def fetch_recent_cards(since_date: datetime) -> list[dict]:
    """
    Fetch cards released or previewed within the lookback window.
    Uses two reliable Scryfall date filters and merges results.
    """
    date_str = since_date.strftime("%Y-%m-%d")

    # date: filter = official release date
    # new: filter = cards added to Scryfall database recently (catches spoilers)
    queries = [
        f"date>={date_str}",
        f"new:art date>={date_str}",
        f"is:new",
    ]

    seen_ids: set[str] = set()
    all_cards: list[dict] = []

    for query in queries:
        print(f"  Query: {query}")
        fetched = fetch_all_pages(query)
        print(f"  -> {len(fetched)} cards fetched")
        for card in fetched:
            card_id = card.get("oracle_id") or card.get("id")
            if card_id and card_id not in seen_ids:
                seen_ids.add(card_id)
                all_cards.append(card)

    return all_cards


def set_github_output(key: str, value: str) -> None:
    """Write a key=value pair to GITHUB_OUTPUT if available."""
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"{key}={value}\n")


def main() -> int:
    print("=== MTG Spoiler RSS Feed Generator ===")

    since_date = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"Lookback window: {since_date.strftime('%Y-%m-%d')} until today")

    known_cards: dict = load_known_cards()
    print(f"Known cards in database: {len(known_cards)}")

    print("Fetching cards from Scryfall...")
    fetched_cards = fetch_recent_cards(since_date)
    print(f"Total unique cards fetched: {len(fetched_cards)}")

    # Determine which cards are genuinely new (not yet in known_cards)
    new_cards = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for card in fetched_cards:
        oracle_id = card.get("oracle_id") or card.get("id")
        if oracle_id and oracle_id not in known_cards:
            new_cards.append(card)
            known_cards[oracle_id] = now_iso

    print(f"New cards (not yet tracked): {len(new_cards)}")

    # Always write the feed if we have any cards at all (important for first run)
    # and always update known_cards when there are new entries
    feed_cards = fetched_cards if fetched_cards else []

    if not feed_cards:
        print("No cards found at all – skipping feed generation.")
        set_github_output("new_cards", "false")
        return 0

    # Sort descending by preview/release date
    feed_cards.sort(key=card_sort_date, reverse=True)
    feed_cards = feed_cards[:MAX_FEED_ENTRIES]

    print(f"Building RSS feed with {len(feed_cards)} entries...")
    rss_items = [build_rss_item(card) for card in feed_cards]

    build_time = datetime.now(timezone.utc)
    rss_xml = build_rss_feed(rss_items, build_time)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print(f"Feed written: {OUTPUT_FILE} ({len(rss_items)} items)")

    # Persist tracking data
    save_known_cards(known_cards)
    print(f"Known cards saved: {len(known_cards)} entries")

    # Signal result to GitHub Actions
    if new_cards or not Path(OUTPUT_FILE).exists():
        set_github_output("new_cards", "true")
        print(f"Result: {len(new_cards)} new card(s) – will commit.")
    else:
        set_github_output("new_cards", "false")
        print("Result: no new cards – skipping commit.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
