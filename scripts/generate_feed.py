#!/usr/bin/env python3
"""
Magic: The Gathering Spoiler RSS Feed Generator

Detects new cards by diffing the full Scryfall /cards/manifest catalog
against the previous run's snapshot (data/known_print_ids.json), instead of
relying on a rolling date-window search. New prints are tracked per
(oracle_id, set) via data/known_cards.json, so a reprint in a new set is
treated as a new feed entry, while duplicate variants within the same set
are not.

Excluded sets and already-known (oracle_id, set) combinations are filtered
from the manifest itself, so they never cost a /cards/collection request.
"""

import argparse
import gzip
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ParseError, fromstring, indent, tostring
import urllib.error
import urllib.request

# --- Configuration ---
SCRYFALL_MANIFEST_URL = "https://api.scryfall.com/cards/manifest"
SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
# Hard rate limits per https://scryfall.com/docs/api/rate-limits
MANIFEST_MIN_INTERVAL = 6.0   # /cards/manifest: 10/minute
COLLECTION_MIN_INTERVAL = 0.5  # /cards/collection: 2/second
COLLECTION_BATCH_SIZE = 75    # max identifiers per /cards/collection request
# Set codes that never count as "new cards"; overridable via EXCLUDED_SETS.
EXCLUDED_SET_CODES = {"plst", "unk", "ounk", "punk", "atrk", "atrc"}  # The List, Un-Sets, Art Series
MAX_FEED_ENTRIES = 500
MAX_NEW_PRINTS_PER_RUN = 500  # API work per run, independent of the feed size
KEPT_IMAGE_KEYS = ("normal", "small")  # image URLs persisted in feed_items.json
# Pushover limits: https://pushover.net/api#limits
PUSHOVER_MESSAGE_LIMIT = 1024
PUSHOVER_TITLE_LIMIT = 250
NOTIFICATION_TOP_SETS = 5  # sets named in the notification, the rest is summarised
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
FEED_LANGUAGE = "de-DE"


def fetch_json(url: str, *, method: str = "GET", body: dict | None = None, max_retries: int = 3) -> dict:
    """Fetch JSON from the Scryfall API with retries for transient errors and 429s."""
    data = None
    headers = {
        "User-Agent": "mtg-spoiler-rss/1.0 (github-actions)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
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
                payload = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt >= max_retries:
                    raise
                wait = 30
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = max(wait, int(retry_after))
                    except ValueError:
                        pass
                wait += attempt
                print(f"  Rate limited (429). Waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
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


def _load_json_file(path: Path, default):
    """Load a JSON state file, returning *default* if it does not exist yet.

    Raises with a clear message (instead of a bare traceback) when the file
    exists but is not valid JSON, e.g. after a manual edit or merge conflict.
    """
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Fatal: state file {path} is not valid JSON: {error}") from error


def load_known_cards() -> dict:
    return _load_json_file(DATA_FILE, {})


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
    ids = _load_json_file(KNOWN_PRINT_IDS_FILE, None)
    if ids is None:
        return None
    return set(ids)


def save_known_print_ids(ids: set[str]) -> bool:
    return write_text_if_changed(KNOWN_PRINT_IDS_FILE, json.dumps(sorted(ids), separators=(",", ":")))


def load_feed_items() -> list[dict]:
    return _load_json_file(FEED_ITEMS_FILE, [])


def save_feed_items(items: list[dict]) -> bool:
    return write_text_if_changed(FEED_ITEMS_FILE, json.dumps(items, indent=2, ensure_ascii=False))


def _image_url(images: dict) -> str | None:
    return images.get("normal") or images.get("large") or images.get("small") or images.get("png")


def card_image_urls(card: dict) -> list[str]:
    """Front image first, then the remaining faces of multi-faced cards."""
    urls: list[str] = []
    front = _image_url(card.get("image_uris") or {})
    if front:
        urls.append(front)
    for face in card.get("card_faces") or []:
        url = _image_url(face.get("image_uris") or {})
        if url and url not in urls:
            urls.append(url)
    return urls


def card_image_url(card: dict) -> str | None:
    urls = card_image_urls(card)
    return urls[0] if urls else None


def image_content_type(url: str) -> str:
    suffix = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return {
        "png": "image/png",
        "webp": "image/webp",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
    }.get(suffix, "image/jpeg")


def card_mana_cost(card: dict) -> str:
    """Multi-faced cards carry the cost on each face instead of the card itself."""
    cost = card.get("mana_cost")
    if cost:
        return cost
    faces = card.get("card_faces") or []
    return " // ".join(face["mana_cost"] for face in faces if face.get("mana_cost"))


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

    name         = card.get("name", "Unknown Card")
    scryfall_uri = card.get("scryfall_uri", "https://scryfall.com")
    set_name     = card.get("set_name", "")
    mana_cost    = card_mana_cost(card)
    type_line    = card.get("type_line", "")
    oracle_text  = card_oracle_text(card)
    rarity       = card.get("rarity", "").capitalize()
    image_urls   = card_image_urls(card)

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
    for index, image_url in enumerate(image_urls):
        alt = name if index == 0 else f"{name} (Rückseite)"
        desc_parts.append(f'<img src="{escape(image_url)}" alt="{escape(alt)}" style="max-width:300px"/>')
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
    if image_urls:
        SubElement(
            item, "enclosure",
            url=image_urls[0], type=image_content_type(image_urls[0]), length="0",
        )

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

    indent(rss, space="  ")
    return '<?xml version="1.0"?>\n' + tostring(rss, encoding="unicode") + "\n"


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
    seen_urls: set[str] = set()
    while url and url not in seen_urls:
        seen_urls.add(url)
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
    """Fetch full Card objects for the given Scryfall IDs via POST /cards/collection.

    A failing batch is skipped and its IDs stay unresolved, so they are retried on
    the next run instead of aborting the whole run.
    """
    cards: list[dict] = []
    first_error: Exception | None = None
    batches = failures = 0
    for i in range(0, len(ids), COLLECTION_BATCH_SIZE):
        batch = ids[i:i + COLLECTION_BATCH_SIZE]
        body = {"identifiers": [{"id": card_id} for card_id in batch]}
        print(f"  Hydrating {i + len(batch)}/{len(ids)} card(s)...")
        batches += 1
        try:
            data = fetch_json(SCRYFALL_COLLECTION_URL, method="POST", body=body)
        except (urllib.error.URLError, OSError, ValueError) as error:
            failures += 1
            first_error = first_error or error
            print(f"  Warning: batch failed ({error}); keeping {len(batch)} ID(s) pending.", file=sys.stderr)
            continue
        cards.extend(data.get("data", []))
        not_found = data.get("not_found", [])
        if not_found:
            print(f"  Warning: {len(not_found)} identifier(s) not found.", file=sys.stderr)
        if i + COLLECTION_BATCH_SIZE < len(ids):
            time.sleep(COLLECTION_MIN_INTERVAL)
    if batches and failures == batches and first_error is not None:
        raise first_error
    return cards


def excluded_set_codes() -> set[str]:
    """Excluded sets from the defaults, overridable with EXCLUDED_SETS=a,b,c."""
    override = os.environ.get("EXCLUDED_SETS")
    if override is None:
        return set(EXCLUDED_SET_CODES)
    return {code.strip().lower() for code in override.split(",") if code.strip()}


def is_feed_eligible(card: dict) -> bool:
    """Filters out cards that aren't interesting "new card" spoilers."""
    if card.get("set") in excluded_set_codes():
        return False
    return True


def feed_dedup_key(card: dict) -> str:
    """Cards are tracked per (oracle card, set) so a reprint in a new set is a new entry."""
    return f"{card.get('oracle_id') or card.get('id')}:{card.get('set')}"


def manifest_dedup_key(entry: dict) -> str:
    """Same identity as feed_dedup_key(), for lightweight /cards/manifest entries."""
    return f"{entry.get('oracle_id') or entry.get('id')}:{entry.get('set_code')}"


def trim_image_uris(images: dict) -> dict:
    """Keep only the image variants the feed renders, so feed_items.json stays small."""
    kept = {key: images[key] for key in KEPT_IMAGE_KEYS if images.get(key)}
    return kept or dict(images)


def trim_card_for_storage(card: dict) -> dict:
    """Keep only the fields build_rss_item() needs, to keep feed_items.json small."""
    keep_keys = (
        "id", "oracle_id", "name", "set", "set_name", "mana_cost", "type_line",
        "oracle_text", "rarity", "released_at", "discovered_at", "scryfall_uri", "image_uris",
    )
    trimmed = {k: card[k] for k in keep_keys if k in card}
    if trimmed.get("image_uris"):
        trimmed["image_uris"] = trim_image_uris(trimmed["image_uris"])
    if "card_faces" in card:
        face_keys = ("name", "oracle_text", "mana_cost", "image_uris")
        faces = []
        for face in card["card_faces"]:
            trimmed_face = {k: face[k] for k in face_keys if k in face}
            if trimmed_face.get("image_uris"):
                trimmed_face["image_uris"] = trim_image_uris(trimmed_face["image_uris"])
            faces.append(trimmed_face)
        trimmed["card_faces"] = faces
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


def set_label(card: dict) -> str:
    """Human readable set name; GitHub step outputs are line based, so flatten whitespace."""
    label = card.get("set_name") or card.get("set") or "Unknown set"
    return " ".join(str(label).split())


def summarize_new_cards(cards: list[dict]) -> list[tuple[str, int]]:
    """Card counts per set name, largest set first, ties alphabetically."""
    counts: dict[str, int] = {}
    for card in cards:
        label = set_label(card)
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def notification_message(new_cards: list[dict]) -> str:
    """Single line summary of the new cards, within the Pushover message limit."""
    total = len(new_cards)
    if not total:
        return "Feed updated — no new cards detected."

    sets = summarize_new_cards(new_cards)
    noun = "card" if total == 1 else "cards"

    def render(entries: tuple[tuple[str, int], ...]) -> str:
        parts = [f"{count}× {label}" for label, count in entries]
        rest = sets[len(entries):]
        if rest:
            rest_cards = sum(count for _, count in rest)
            part_suffix = "s" if len(rest) > 1 else ""
            card_suffix = "s" if rest_cards > 1 else ""
            parts.append(f"+{len(rest)} more set{part_suffix} ({rest_cards} card{card_suffix})")
        return f"{total} new {noun}: " + ", ".join(parts)

    entries = tuple(sets[:NOTIFICATION_TOP_SETS])
    while True:
        message = render(entries) if entries else f"{total} new {noun}."
        if len(message) <= PUSHOVER_MESSAGE_LIMIT or not entries:
            break
        entries = entries[:-1]
    return _truncate(message, PUSHOVER_MESSAGE_LIMIT)


def notification_title(new_cards: list[dict]) -> str:
    total = len(new_cards)
    if not total:
        return "MTG feed updated"
    noun = "spoiler" if total == 1 else "spoilers"
    return _truncate(f"{total} new MTG {noun}", PUSHOVER_TITLE_LIMIT)


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
            excluded_sets = excluded_set_codes()
            candidate_ids: list[str] = []
            treated_ids: set[str] = set()
            pending_keys: set[str] = set()
            for identifier, entry in manifest_entries.items():
                if identifier in previous_ids:
                    continue
                if entry.get("set_code") in excluded_sets:
                    treated_ids.add(identifier)
                    continue
                key = manifest_dedup_key(entry) if entry.get("oracle_id") and entry.get("set_code") else None
                if key is not None and (key in known_cards or key in pending_keys):
                    treated_ids.add(identifier)
                    continue
                if key is not None:
                    pending_keys.add(key)
                candidate_ids.append(identifier)
            selected_ids = candidate_ids[:MAX_NEW_PRINTS_PER_RUN]
            print(
                f"Processing {len(selected_ids)} of {len(candidate_ids)} candidate print(s), "
                f"{len(treated_ids)} skipped without hydration."
            )
            hydrated = hydrate_cards(selected_ids)
        except (urllib.error.URLError, OSError, ValueError) as error:
            print(f"Fatal: could not fetch cards: {error}", file=sys.stderr)
            set_github_output("new_cards", "false")
            return 1

        resolved_ids = {card["id"] for card in hydrated} & set(selected_ids)
        if is_bootstrap:
            processed_print_ids = (current_print_ids - set(selected_ids)) | resolved_ids
        else:
            processed_print_ids = (previous_ids & current_print_ids) | resolved_ids | treated_ids
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
    set_github_output("new_card_count", str(len(new_cards)))
    set_github_output("notification_title", notification_title(new_cards))
    set_github_output("notification_message", notification_message(new_cards))
    set_github_output("feed_changed", "true" if feed_changed else "false")
    set_github_output("state_changed", "true" if items_changed or cards_changed or ids_changed else "false")
    print(f"Done: {len(new_cards)} new card(s) added.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the stored feed without API requests.")
    args = parser.parse_args()
    sys.exit(main(rebuild_only=args.rebuild))
