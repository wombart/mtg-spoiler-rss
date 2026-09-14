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

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring, ParseError
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
EXCLUDED_SET_CODES = {"plst"}  # "The List"
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


def write_text_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


def save_known_cards(known: dict) -> bool:
    return write_text_if_changed(DATA_FILE, json.dumps(known, indent=2, ensure_ascii=False))


def load_known_print_ids() -> set[str] | None:
    """Returns None if no baseline exists yet (i.e. this is the first run)."""
    if not KNOWN_PRINT_IDS_FILE.exists():
        return None
    with open(KNOWN_PRINT_IDS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_known_print_ids(ids: set[str]) -> bool:
    return write_text_if_changed(KNOWN_PRINT_IDS_FILE, json.dumps(sorted(ids)))


def load_feed_items() -> list[dict]:
    if FEED_ITEMS_FILE.exists():
        with open(FEED_ITEMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_feed_items(items: list[dict]) -> bool:
    return write_text_if_changed(FEED_ITEMS_FILE, json.dumps(items, indent=2, ensure_ascii=False))


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
    return card.get("discovered_at") or "1970-01-01T00:00:00+00:00"


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
        pub_dt = datetime.fromisoformat(pub_date_str)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        pub_dt = pub_dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pub_dt = datetime.now(timezone.utc)

    title_text = name
    if set_name:
        title_text += f" [{set_name}]"
    SubElement(item, "title").text = title_text
    SubElement(item, "link").text = scryfall_uri

    SubElement(item, "guid", isPermaLink="false").text = feed_dedup_key(card)
    SubElement(item, "pubDate").text = format_datetime(pub_dt)

    desc_parts = []
    if image_url:
        desc_parts.append(f'<img src="{escape(image_url)}" alt="{escape(name)}" style="max-width:300px"/>')
    desc_parts.append(f"<p><strong>{escape(name)}</strong></p>")
    if mana_cost:
        desc_parts.append(f"<p>Manakosten: {escape(mana_cost)}</p>")
    if type_line:
        desc_parts.append(f"<p>Typ: {escape(type_line)}</p>")
    if rarity:
        desc_parts.append(f"<p>Seltenheit: {escape(rarity)}</p>")
    if set_name:
        desc_parts.append(f"<p>Set: {escape(set_name)}</p>")
    if oracle_text:
        desc_parts.append(f"<p>{escape(oracle_text).replace(chr(10), '<br/>')}</p>")
    desc_parts.append(f'<p><a href="{escape(scryfall_uri)}">Auf Scryfall ansehen</a></p>')

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
    SubElement(channel, "lastBuildDate").text = format_datetime(build_time)
    SubElement(channel, "ttl").text = "120"

    feed_url = os.environ.get("FEED_URL", "").strip()
    if feed_url:
        atom_link = SubElement(channel, "atom:link")
        atom_link.set("rel", "self")
        atom_link.set("type", "application/rss+xml")
        atom_link.set("href", feed_url)

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
    if card.get("set") in EXCLUDED_SET_CODES:
        return False
    return True


def feed_dedup_key(card: dict) -> str:
    """Cards are tracked per (oracle card, set) so a reprint in a new set is a new entry."""
    return f"{card.get('oracle_id') or card.get('id')}:{card.get('set')}"


def trim_card_for_storage(card: dict) -> dict:
    """Keep only the fields build_rss_item() needs, to keep feed_items.json small."""
    keep_keys = (
        "id", "oracle_id", "name", "set", "set_name", "mana_cost", "type_line",
        "oracle_text", "rarity", "released_at", "discovered_at", "scryfall_uri", "image_uris",
    )
    trimmed = {k: card[k] for k in keep_keys if k in card}
    if "card_faces" in card:
        face_keys = ("name", "oracle_text", "mana_cost", "image_uris")
        trimmed["card_faces"] = [
            {k: face[k] for k in face_keys if k in face} for face in card["card_faces"]
        ]
    return trimmed


def write_feed(feed_items: list[dict]) -> bool:
    rss_items = [build_rss_item(card) for card in feed_items]
    if OUTPUT_FILE.exists():
        previous_xml = OUTPUT_FILE.read_text(encoding="utf-8")
        try:
            previous_time = parsedate_to_datetime(fromstring(previous_xml).findtext("./channel/lastBuildDate"))
            if build_rss_feed(rss_items, previous_time) == previous_xml:
                return False
        except (ParseError, ValueError, TypeError, IndexError):
            pass
    rss_xml = build_rss_feed(rss_items, datetime.now(timezone.utc))
    changed = write_text_if_changed(OUTPUT_FILE, rss_xml)
    print(f"Feed written: {OUTPUT_FILE} ({len(feed_items)} entries)")
    return changed


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
        new_cards.append({**card, "discovered_at": now_iso})
    return new_cards


def normalize_feed_items(items: list[dict], known_cards: dict) -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    unique_items = {}
    for card in items:
        key = feed_dedup_key(card)
        if key in unique_items:
            continue
        discovered_at = (
            card.get("discovered_at") or known_cards.get(key)
            or known_cards.get(card.get("oracle_id")) or now_iso
        )
        known_cards.setdefault(key, discovered_at)
        unique_items[key] = trim_card_for_storage({**card, "discovered_at": discovered_at})
    return sorted(unique_items.values(), key=card_sort_date, reverse=True)[:MAX_FEED_ENTRIES]


def main(*, rebuild_only: bool = False) -> int:
    print("=== MTG Spoiler RSS Feed Generator ===")

    known_print_ids = load_known_print_ids()
    is_bootstrap = known_print_ids is None
    known_cards = load_known_cards()
    feed_items = normalize_feed_items(load_feed_items(), known_cards)
    print(f"Known cards in database: {len(known_cards)}")
    print(f"Known print IDs: {len(known_print_ids) if known_print_ids else 0}")

    new_cards = []
    processed_print_ids = known_print_ids
    if not rebuild_only:
        print("Fetching full card manifest from Scryfall (/cards/manifest)...")
        try:
            manifest_entries = fetch_manifest_entries()
            current_print_ids = set(manifest_entries)
            previous_ids = known_print_ids or set()
            candidate_ids = [identifier for identifier in manifest_entries if identifier not in previous_ids]
            selected_ids = candidate_ids[:MAX_FEED_ENTRIES]
            print(f"Processing {len(selected_ids)} of {len(candidate_ids)} candidate print(s).")
            hydrated = hydrate_cards(selected_ids)
        except (urllib.error.URLError, OSError) as error:
            print(f"Fatal: could not fetch cards: {error}", file=sys.stderr)
            set_github_output("new_cards", "false")
            return 1

        resolved_ids = {card["id"] for card in hydrated} & set(selected_ids)
        if is_bootstrap:
            processed_print_ids = (current_print_ids - set(selected_ids)) | resolved_ids
        else:
            processed_print_ids = (previous_ids & current_print_ids) | resolved_ids
        new_cards = select_new_cards_for_feed(
            [card for card in hydrated if card["id"] in resolved_ids], known_cards
        )
        feed_items = normalize_feed_items(new_cards + feed_items, known_cards)

    feed_changed = write_feed(feed_items)
    items_changed = save_feed_items(feed_items)
    cards_changed = save_known_cards(known_cards)
    ids_changed = False
    if processed_print_ids is not None:
        ids_changed = save_known_print_ids(processed_print_ids)

    set_github_output("new_cards", "true" if new_cards else "false")
    set_github_output("feed_changed", "true" if feed_changed else "false")
    set_github_output("state_changed", "true" if items_changed or cards_changed or ids_changed else "false")
    print(f"Done: {len(new_cards)} new card(s) added.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the stored feed without API requests.")
    args = parser.parse_args()
    sys.exit(main(rebuild_only=args.rebuild))
