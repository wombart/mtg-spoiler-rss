# MTG Spoiler RSS Feed

*[Deutsche Version](README.de.md)*

Automatically generated RSS feed for newly spoiled and released
Magic: The Gathering cards. Data comes from the [Scryfall API](https://scryfall.com/docs/api),
the build runs every 2 hours via GitHub Actions, and the result is published
on GitHub Pages.

## How new cards are detected

Instead of a rolling time window, each run fetches the complete Scryfall
card catalog via [`/cards/manifest`](https://scryfall.com/docs/api/cards/manifest)
(paginated, ~8 requests) and compares the set of all print IDs against the
last known state (`data/known_print_ids.json`). New IDs are then fully
resolved via [`/cards/collection`](https://scryfall.com/docs/api/cards/collection).
This is more robust than a date-based search, since nothing can get lost
due to an expired time window or preview-date quirks – a card print counts
as new as soon as its ID first appears in the catalog.

Cards are tracked per `(oracle_id, set)` combination (`data/known_cards.json`):
a reprint in a new set creates a new feed entry, but multiple variants
(foil, showcase, …) within the same set only produce one.

Both Scryfall rate limits are respected: 10 requests/minute for
`/cards/manifest`, 2 requests/second for `/cards/collection`.

## Subscribing to the feed

After the first deployment, the feed is available at:

```
https://<your-username>.github.io/<repo-name>/feed.xml
```

Add this URL to any RSS reader (e.g. Feedly, NewsBlur, NetNewsWire,
Thunderbird, …).

## Setting up the repository

### 1. Create a repository

Create a new **public** GitHub repository (or fork this one).

### 2. Enable GitHub Pages

In the repository settings:

```
Settings → Pages → Source: GitHub Actions
```

### 3. Trigger the first run

```
Actions → "Update MTG Spoiler RSS Feed" → Run workflow
```

The first run populates `data/known_cards.json` and generates `docs/feed.xml`.
After that, the workflow runs automatically every 4 hours.

### 4. Determine the feed URL

```
https://<username>.github.io/<repo>/feed.xml
```

---

## Configuration

All parameters are defined as constants at the top of the
`scripts/generate_feed.py` script:

| Constant | Default | Description |
|---|---|---|
| `MAX_FEED_ENTRIES` | `750` | Maximum number of entries in the feed |
| `EXCLUDED_SET_CODES` | `{"plist"}` | Set codes whose cards never count as "new" (e.g. "The List") |

---

## Running locally

```bash
# Dependencies: none (Python 3.11+ stdlib only)
python scripts/generate_feed.py
```

The generated feed is then located at `docs/feed.xml`.

---

## File structure

```
.
├── .github/
│   └── workflows/
│       └── update-feed.yml     # GitHub Actions workflow
├── data/
│   ├── known_cards.json        # Known (oracle_id, set) combinations (duplicate protection)
│   ├── known_print_ids.json    # Baseline of all Scryfall print IDs (last manifest state)
│   └── feed_items.json         # Most recently rendered feed entries (base for the next build)
├── docs/                       # GitHub Pages root
│   ├── feed.xml                # Generated RSS feed
│   └── index.html              # Info page
├── scripts/
│   └── generate_feed.py        # Feed generator
└── README.md
```

---

## Privacy & License

Card data and images are sourced from [Scryfall](https://scryfall.com) and
are the property of Wizards of the Coast LLC.
Magic: The Gathering is a registered trademark of Wizards of the Coast.

This project is licensed under the [MIT License](LICENSE).
