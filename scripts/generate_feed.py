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


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "mtg-spoiler-rss/1.0 (github-actions)",
            "Accept": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_known_cards() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known_cards(known: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(known, f, indent=2, ensure_ascii=False)


def card_image_url(card: dict) -> str | None:
    images = card.get("image_uris", {})
    if images:
        return images.get("normal") or images.get("large") or images.get("small")
    faces = card.get("card_faces", [])
    if faces:
        face_images = faces[0].get("image_uris", {})
        return face_images.get("normal") or face_images.get("large")
    return None


def card_oracle_text(card: dict) -> str:
    text = card.get("oracle_text")
    if text:
        return text
    faces = card.get("card_faces", [])
    if faces:
        parts = [f.get("oracle_text", "") for f in faces if f.get("oracle_text")]
        return "\n//\n".join(parts)
    return ""


def card_sort_date(card: dict) -> str:
    return card.get("released_at") or "1970-01-01"


def build_rss_item(card: dict) -> Element:
    item = Element("item")

    name        = card.get("name", "Unknown Card")
    scryfall_uri = card.get("scryfall_uri", "https://scryfall.com")
    set_name    = card.get("set_name", "")
    mana_cost   = card.get("mana_cost", "")
    type_line   = card.get("type_line", "")
    oracle_text = card_oracle_text(card)
    rarity      = card.get("rarity", "").capitalize()
    image_url   = card_image_url(card)

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
    SubElement(item, "guid", isPermaLink="false").text = oracle_id
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
        desc_parts.append(f"<p>{oracle_text.replace(chr(10), '<br/>')}</p>")
    desc_parts.append(f'<p><a href="{scryfall_uri}">Auf Scryfall ansehen</a></p>')

    SubElement(item, "description").text = "\n".join(desc_parts)
    if image_url:
        SubElement(item, "enclosure", url=image_url, type="image/jpeg", length="0")

    return item


def build_rss_feed(items: list[Element], build_time: datetime) -> str:
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


def fetch_recent_cards(since_date: datetime) -> list[dict]:
    """
    Fetch cards using documented Scryfall search syntax:
    - date>=yyyy-mm-dd  filters by official release date
    - order:spoiled     sorts by spoiler/preview date (newest first)
    - unique:cards      one result per unique oracle card
    - include:extras    include tokens, variants etc.
    """
    date_str = since_date.strftime("%Y-%m-%d")
    query = f"date>={date_str}"

    params = urllib.parse.urlencode({
        "q": query,
        "order": "spoiled",
        "dir": "desc",
        "unique": "cards",
    })
    url = f"{SCRYFALL_SEARCH_URL}?{params}"

    all_cards: list[dict] = []
    page = 1

    while url:
        print(f"  Fetching page {page}...")
        try:
            data = fetch_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("  No results found (404).")
            else:
                print(f"  HTTP error {e.code}: {e.reason}", file=sys.stderr)
            break
        except Exception as e:
            print(f"  Error on page {page}: {e}", file=sys.stderr)
            break

        batch = data.get("data", [])
        all_cards.extend(batch)
        print(f"  Page {page}: {len(batch)} cards (total so far: {len(all_cards)})")

        url = data.get("next_page")
        page += 1
        if url:
            time.sleep(0.12)

    return all_cards


def set_github_output(key: str, value: str) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"{key}={value}\n")


def main() -> int:
    print("=== MTG Spoiler RSS Feed Generator ===")

    since_date = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"Lookback window: {since_date.strftime('%Y-%m-%d')} – today")

    known_cards = load_known_cards()
    print(f"Known cards in database: {len(known_cards)}")

    print("Fetching cards from Scryfall...")
    fetched_cards = fetch_recent_cards(since_date)
    print(f"Total cards fetched: {len(fetched_cards)}")

    if not fetched_cards:
        print("No cards fetched – aborting without changes.")
        set_github_output("new_cards", "false")
        return 0

    # Determine which are new (not yet tracked)
    now_iso = datetime.now(timezone.utc).isoformat()
    new_cards = []
    for card in fetched_cards:
        oracle_id = card.get("oracle_id") or card.get("id")
        if oracle_id and oracle_id not in known_cards:
            new_cards.append(card)
            known_cards[oracle_id] = now_iso

    print(f"New cards (not yet tracked): {len(new_cards)}")

    # Always generate feed on first run (known_cards was empty at start)
    is_first_run = not OUTPUT_FILE.exists()
    if not new_cards and not is_first_run:
        print("No new cards and feed already exists – skipping.")
        set_github_output("new_cards", "false")
        return 0

    # Build and write feed
    feed_cards = sorted(fetched_cards, key=card_sort_date, reverse=True)[:MAX_FEED_ENTRIES]
    print(f"Building RSS feed with {len(feed_cards)} entries...")

    rss_items = [build_rss_item(card) for card in feed_cards]
    rss_xml = build_rss_feed(rss_items, datetime.now(timezone.utc))

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print(f"Feed written: {OUTPUT_FILE}")

    save_known_cards(known_cards)
    print(f"Known cards saved: {len(known_cards)} entries")

    set_github_output("new_cards", "true")
    print(f"Done – {len(new_cards)} new card(s) added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
