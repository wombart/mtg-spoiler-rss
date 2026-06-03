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
        headers={"User-Agent": "mtg-spoiler-rss/1.0 (github-actions)"}
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
    # Double-faced cards
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
    released_at = card.get("released_at", "")
    preview_date = card.get("preview", {}).get("previewed_at", released_at)
    image_url = card_image_url(card)

    # Title
    title_text = name
    if set_name:
        title_text += f" [{set_name}]"
    SubElement(item, "title").text = title_text

    # Link
    SubElement(item, "link").text = scryfall_uri

    # GUID – stable, unique per card face combination
    oracle_id = card.get("oracle_id", card.get("id", name))
    guid = SubElement(item, "guid", isPermaLink="false")
    guid.text = oracle_id

    # PubDate – use preview date if available, else released_at
    pub_date_str = preview_date or released_at
    try:
        pub_dt = datetime.fromisoformat(pub_date_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pub_dt = datetime.now(timezone.utc)
    SubElement(item, "pubDate").text = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Description – HTML content for feed readers
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

    # Enclosure for image (for feed readers that support media)
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

    # Atom self-link (good practice)
    atom_link = SubElement(channel, "atom:link")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    # Will be replaced by actual URL after deploy – placeholder is fine
    atom_link.set("href", "https://example.github.io/mtg-spoiler-rss/feed.xml")

    for item in items:
        channel.append(item)

    raw = tostring(rss, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


def fetch_new_cards(since_date: datetime) -> list[dict]:
    """
    Fetch all cards from Scryfall that were previewed or released
    within the lookback window.
    """
    date_str = since_date.strftime("%Y-%m-%d")
    # Query: cards with a preview date >= since_date OR released >= since_date
    # We use two queries and merge results.
    queries = [
        f"previewed>={date_str}",
        f"date>={date_str}",
    ]

    seen_ids: set[str] = set()
    all_cards: list[dict] = []

    for query in queries:
        params = urllib.parse.urlencode({
            "q": query,
            "order": "previewed",
            "dir": "desc",
            "unique": "cards",
        })
        url = f"{SCRYFALL_SEARCH_URL}?{params}"

        page = 1
        while url:
            print(f"  Fetching page {page} for query: {query[:60]}...")
            try:
                data = fetch_json(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # No results for this query – perfectly normal
                    print(f"  No results for query (404), skipping.")
                    break
                print(f"  HTTP error {e.code} on page {page}: {e.reason}", file=sys.stderr)
                break
            except Exception as e:
                print(f"  Warning: failed to fetch page {page}: {e}", file=sys.stderr)
                break

            for card in data.get("data", []):
                card_id = card.get("oracle_id") or card.get("id")
                if card_id and card_id not in seen_ids:
                    seen_ids.add(card_id)
                    all_cards.append(card)

            url = data.get("next_page")
            page += 1
            if url:
                time.sleep(0.1)  # Respect Scryfall rate limit (10 req/s)

    return all_cards


def main() -> int:
    print("=== MTG Spoiler RSS Feed Generator ===")

    since_date = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"Looking back to: {since_date.strftime('%Y-%m-%d')}")

    # Load existing tracking data
    known_cards: dict = load_known_cards()
    print(f"Known cards in database: {len(known_cards)}")

    # Fetch cards from Scryfall
    print("Fetching cards from Scryfall API...")
    fetched_cards = fetch_new_cards(since_date)
    print(f"Cards fetched from API: {len(fetched_cards)}")

    # Filter to only truly new cards (not in known_cards)
    new_cards = []
    for card in fetched_cards:
        oracle_id = card.get("oracle_id") or card.get("id")
        if oracle_id and oracle_id not in known_cards:
            new_cards.append(card)
            known_cards[oracle_id] = datetime.now(timezone.utc).isoformat()

    print(f"New cards not yet in feed: {len(new_cards)}")

    if not new_cards:
        print("No new cards found. Exiting without changes.")
        # Signal to GitHub Actions: no commit needed
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("new_cards=false\n")
        return 0

    # Build RSS items for ALL known cards within lookback window
    # (re-render existing feed entries + new ones, trimmed to MAX_FEED_ENTRIES)
    print("Building RSS feed items...")
    all_feed_cards = fetched_cards  # Already deduplicated by oracle_id above

    # Sort by preview/release date descending
    def sort_key(card):
        preview = card.get("preview", {}).get("previewed_at") or card.get("released_at", "")
        return preview

    all_feed_cards.sort(key=sort_key, reverse=True)

    # Trim to max entries
    trimmed = all_feed_cards[:MAX_FEED_ENTRIES]
    print(f"Feed entries after trimming to {MAX_FEED_ENTRIES}: {len(trimmed)}")

    rss_items = [build_rss_item(card) for card in trimmed]

    # Generate XML
    build_time = datetime.now(timezone.utc)
    rss_xml = build_rss_feed(rss_items, build_time)

    # Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print(f"Feed written to: {OUTPUT_FILE}")

    # Save updated known_cards
    save_known_cards(known_cards)
    print(f"Known cards database updated: {len(known_cards)} entries")

    # Signal to GitHub Actions: commit needed
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("new_cards=true\n")

    print(f"Done! {len(new_cards)} new card(s) added to feed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
