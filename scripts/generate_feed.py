#!/usr/bin/env python3
"""
Magic: The Gathering Spoiler RSS Feed Generator

Detects new cards by diffing the full Scryfall /cards/manifest catalog
against the previous run's snapshot (data/known_print_ids.json), instead of
relying on a rolling date-window search. New prints are tracked per
(oracle_id, set) via data/known_cards.json, so a reprint in a new set is
treated as a new feed entry, while duplicate variants within the same set
are not.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
import urllib.request
import urllib.error

# --- Configuration ---
SCRYFALL_MANIFEST_URL = "https://api.scryfall.com/cards/manifest"
SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
# Hard rate limits per https://scryfall.com/docs/api/rate-limits
MANIFEST_MIN_INTERVAL = 6.0   # /cards/manifest: 10/minute
COLLECTION_MIN_INTERVAL = 0.5  # /cards/collection: 2/second
COLLECTION_BATCH_SIZE = 75    # max identifiers per /cards/collection request
# Sets that only contain reprints and are not interesting as "new cards"
EXCLUDED_SET_CODES = {}  # "The List"
MAX_FEED_ENTRIES = 500
DATA_FILE = Path(__file__).parent.parent / "data" / "known_cards.json"
KNOWN_PRINT_IDS_FILE = Path(__file__).parent.parent / "data" / "known_print_ids.json"
FEED_ITEMS_FILE = Path(__file__).parent.parent / "data" / "feed_items.json"
OUTPUT_FILE = Path(__file__).parent.parent / "docs" / "feed.xml"
FEED_TITLE = "Magic: The Gathering – Neue Karten & Spoiler"
FEED_DESCRIPTION = (
    "Automatisch generierter RSS Feed für neu gespoilerte und "
    "veröffentlichte Magic: The Gathering Karten via Scryfall."
)
FEED_LINK = "https://scryfall.com"
FEED_LANGUAGE = "de-de"


def fetch_json(url: str, *, method: str = "GET", body: dict | None = None, max_retries: int = 3) -> dict:
    """Fetch JSON from the Scryfall API with retries for transient errors and 429s."""
    data = None
    headers = {
        "User-Agent": "mtg-spoiler-rss/1.0 (github-actions)",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 30
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = max(wait, int(retry_after))
                    except ValueError:
                        pass
                print(f"  Rate limited (429). Waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                if attempt >= max_retries:
                    raise
                continue
            if 500 <= e.code < 600 and attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Server error {e.code}, retrying in {wait}s (attempt {attempt}/{max_retries})...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Network error: {e}. Retrying in {wait}s (attempt {attempt}/{max_retries})...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise


def load_known_cards() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known_cards(known: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(known, f, indent=2, ensure_ascii=False)


def load_known_print_ids() -> set[str] | None:
    """Returns None if no baseline exists yet (i.e. this is the first run)."""
    if not KNOWN_PRINT_IDS_FILE.exists():
        return None
    with open(KNOWN_PRINT_IDS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_known_print_ids(ids: set[str]) -> None:
    KNOWN_PRINT_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KNOWN_PRINT_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def load_feed_items() -> list[dict]:
    if FEED_ITEMS_FILE.exists():
        with open(FEED_ITEMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_feed_items(items: list[dict]) -> None:
    FEED_ITEMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FEED_ITEMS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


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


def fetch_manifest_entries() -> dict[str, dict]:
    """
    Fetch the complete Scryfall card manifest (/cards/manifest).

    This is a full, index-independent enumeration of every card print Scryfall
    currently offers (id, name, set_code, collector_number, lang, ...). Comparing
    this full ID set against the previous run's snapshot is far more reliable
    than a rolling "date>=" search window, since it cannot silently miss cards
    due to preview-date quirks or a lookback window that expired between runs.

    Returns an dict of id -> manifest entry, in newest-release-first order
    (dicts preserve insertion order, and pages are requested in that order).
    """
    entries: dict[str, dict] = {}
    url = f"{SCRYFALL_MANIFEST_URL}?order=released"
    page = 1
    while url:
        print(f"  Manifest page {page}...")
        data = fetch_json(url)
        batch = data.get("data", [])
        for entry in batch:
            entries[entry["id"]] = entry
        total = data.get("total_cards")
        print(f"  Page {page}: {len(batch)} entries (total so far: {len(entries)} / {total})")
        url = data.get("next_page")
        page += 1
        if url:
            time.sleep(MANIFEST_MIN_INTERVAL)
    return entries


def hydrate_cards(ids: list[str]) -> list[dict]:
    """Fetch full Card objects for the given Scryfall IDs via POST /cards/collection."""
    cards: list[dict] = []
    for i in range(0, len(ids), COLLECTION_BATCH_SIZE):
        batch = ids[i:i + COLLECTION_BATCH_SIZE]
        body = {"identifiers": [{"id": card_id} for card_id in batch]}
        print(f"  Hydrating {i + len(batch)}/{len(ids)} card(s)...")
        data = fetch_json(SCRYFALL_COLLECTION_URL, method="POST", body=body)
        cards.extend(data.get("data", []))
        not_found = data.get("not_found", [])
        if not_found:
            print(f"  Warning: {len(not_found)} identifier(s) not found.", file=sys.stderr)
        if i + COLLECTION_BATCH_SIZE < len(ids):
            time.sleep(COLLECTION_MIN_INTERVAL)
    return cards


def is_feed_eligible(card: dict) -> bool:
    """Filters out cards that aren't interesting "new card" spoilers."""
    #if card.get("set") in EXCLUDED_SET_CODES:
    #    return False
    #if "paper" not in card.get("games", []):
    #    return False
    return True


def feed_dedup_key(card: dict) -> str:
    """Cards are tracked per (oracle card, set) so a reprint in a new set is a new entry."""
    return f"{card.get('oracle_id') or card.get('id')}:{card.get('set')}"


def trim_card_for_storage(card: dict) -> dict:
    """Keep only the fields build_rss_item() needs, to keep feed_items.json small."""
    keep_keys = (
        "id", "oracle_id", "name", "set", "set_name", "mana_cost", "type_line",
        "oracle_text", "rarity", "released_at", "scryfall_uri", "image_uris",
    )
    trimmed = {k: card[k] for k in keep_keys if k in card}
    if "card_faces" in card:
        face_keys = ("name", "oracle_text", "mana_cost", "image_uris")
        trimmed["card_faces"] = [
            {k: face[k] for k in face_keys if k in face} for face in card["card_faces"]
        ]
    return trimmed


def write_feed(feed_items: list[dict]) -> None:
    rss_items = [build_rss_item(card) for card in feed_items]
    rss_xml = build_rss_feed(rss_items, datetime.now(timezone.utc))
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print(f"Feed written: {OUTPUT_FILE} ({len(feed_items)} entries)")


def set_github_output(key: str, value: str) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def select_new_cards_for_feed(hydrated_cards: list[dict], known_cards: dict) -> list[dict]:
    """Filters hydrated cards down to genuinely new (oracle, set) combos, updating known_cards in place."""
    now_iso = datetime.now(timezone.utc).isoformat()
    new_cards = []
    for card in hydrated_cards:
        if not is_feed_eligible(card):
            continue
        key = feed_dedup_key(card)
        if key in known_cards:
            continue
        known_cards[key] = now_iso
        new_cards.append(card)
    return new_cards


def main() -> int:
    print("=== MTG Spoiler RSS Feed Generator ===")

    known_print_ids = load_known_print_ids()
    is_bootstrap = known_print_ids is None
    known_cards = load_known_cards()
    feed_items = load_feed_items()
    print(f"Known cards in database: {len(known_cards)}")
    print(f"Known print IDs: {len(known_print_ids) if known_print_ids else 0}")

    print("Fetching full card manifest from Scryfall (/cards/manifest)...")
    try:
        manifest_entries = fetch_manifest_entries()
    except (urllib.error.URLError, OSError) as e:
        print(f"Fatal: could not fetch manifest: {e}", file=sys.stderr)
        set_github_output("new_cards", "false")
        return 1

    current_print_ids = set(manifest_entries.keys())
    print(f"Manifest contains {len(current_print_ids)} card print(s).")

    if is_bootstrap:
        print("No known_print_ids baseline found – performing initial bootstrap.")
        print("Recording the full catalog as the baseline and seeding an initial feed")
        print("from the most recently released cards (no historical replay).")
        # manifest_entries preserves newest-release-first order, so the first
        # MAX_FEED_ENTRIES ids are the most recent cards on Scryfall.
        seed_ids = list(manifest_entries.keys())[:MAX_FEED_ENTRIES]
        hydrated = hydrate_cards(seed_ids)
        new_cards = select_new_cards_for_feed(hydrated, known_cards)

        feed_items = [trim_card_for_storage(c) for c in new_cards]
        feed_items.sort(key=card_sort_date, reverse=True)

        save_known_print_ids(current_print_ids)
        save_known_cards(known_cards)
        save_feed_items(feed_items)
        write_feed(feed_items)

        set_github_output("new_cards", "true" if feed_items else "false")
        print(f"Bootstrap complete: {len(current_print_ids)} print IDs recorded, {len(feed_items)} feed entries seeded.")
        return 0

    new_print_ids = current_print_ids - known_print_ids
    print(f"New print ID(s) since last run: {len(new_print_ids)}")

    if not new_print_ids:
        save_known_print_ids(current_print_ids)
        print("No new cards – skipping feed rebuild.")
        set_github_output("new_cards", "false")
        return 0

    print("Hydrating new card print(s) via /cards/collection...")
    hydrated = hydrate_cards(sorted(new_print_ids))
    new_cards = select_new_cards_for_feed(hydrated, known_cards)
    print(f"New feed-worthy card(s): {len(new_cards)} of {len(hydrated)} hydrated print(s).")

    # Persist the wider catalog state regardless of feed-worthiness, so the
    # next run's diff stays correct even if nothing ends up in the feed.
    save_known_print_ids(current_print_ids)
    save_known_cards(known_cards)

    if not new_cards:
        print("No feed-worthy new cards (filtered out or duplicates) – skipping feed rebuild.")
        set_github_output("new_cards", "false")
        return 0

    feed_items = [trim_card_for_storage(c) for c in new_cards] + feed_items
    feed_items.sort(key=card_sort_date, reverse=True)
    feed_items = feed_items[:MAX_FEED_ENTRIES]

    save_feed_items(feed_items)
    write_feed(feed_items)

    set_github_output("new_cards", "true")
    print(f"Done – {len(new_cards)} new card(s) added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
