# MTG Spoiler RSS Feed

*[English version](README.md)*

Automatisch generierter RSS Feed für neu gespoilerte und veröffentlichte
Magic: The Gathering Karten. Daten kommen von der [Scryfall API](https://scryfall.com/docs/api),
der Build läuft alle 2 Stunden via GitHub Actions, das Ergebnis wird auf
GitHub Pages veröffentlicht.

## Wie neue Karten erkannt werden

Statt eines rollierenden Zeitfensters wird bei jedem Lauf der komplette
Scryfall-Kartenkatalog über [`/cards/manifest`](https://scryfall.com/docs/api/cards/manifest)
abgerufen (paginiert, ~8 Requests) und die Menge aller Print-IDs mit dem
letzten bekannten Stand (`data/known_print_ids.json`) verglichen. Neue IDs
werden per [`/cards/collection`](https://scryfall.com/docs/api/cards/collection)
vollständig aufgelöst. Das ist robuster als eine Datums-Suche, weil dabei
nichts durch ein abgelaufenes Zeitfenster oder Preview-Datums-Eigenheiten
verloren gehen kann – ein Kartendruck gilt als neu, sobald seine ID zum
ersten Mal im Katalog auftaucht.

Karten werden pro `(oracle_id, set)`-Kombination getrackt (`data/known_cards.json`):
ein Nachdruck in einem neuen Set erzeugt also einen neuen Feed-Eintrag,
mehrere Varianten (Foil, Showcase, …) im selben Set aber nur einen.

Beide Scryfall-Rate-Limits werden eingehalten: 10 Requests/Minute für
`/cards/manifest`, 2 Requests/Sekunde für `/cards/collection`.

## Feed abonnieren

Nach dem ersten Deployment ist der Feed erreichbar unter:

```
https://<dein-username>.github.io/<repo-name>/feed.xml
```

Diese URL in jeden RSS Reader eintragen (z. B. Feedly, NewsBlur, NetNewsWire,
Thunderbird, …).

## Repository einrichten

### 1. Repository erstellen

Ein neues **öffentliches** GitHub Repository erstellen (oder dieses forken).

### 2. GitHub Pages aktivieren

In den Repository-Einstellungen:

```
Settings → Pages → Source: GitHub Actions
```

### 3. Ersten Run anstoßen

```
Actions → "Update MTG Spoiler RSS Feed" → Run workflow
```

Der erste Lauf befüllt `data/known_cards.json` und generiert `docs/feed.xml`.
Ab dann läuft der Workflow automatisch alle 4 Stunden.

### 4. Feed URL ermitteln

```
https://<username>.github.io/<repo>/feed.xml
```

---

## Konfiguration

Alle Parameter sind im Script `scripts/generate_feed.py` oben als Konstanten
definiert:

| Konstante | Standard | Beschreibung |
|---|---|---|
| `MAX_FEED_ENTRIES` | `750` | Maximale Anzahl Einträge im Feed |
| `EXCLUDED_SET_CODES` | `{"plist"}` | Set-Codes, deren Karten nie als "neu" gelten (z. B. "The List") |

---

## Lokale Ausführung

```bash
# Abhängigkeiten: keine (nur Python 3.11+ Stdlib)
python scripts/generate_feed.py
```

Der generierte Feed liegt dann unter `docs/feed.xml`.

---

## Dateistruktur

```
.
├── .github/
│   └── workflows/
│       └── update-feed.yml     # GitHub Actions Workflow
├── data/
│   ├── known_cards.json        # Bekannte (oracle_id, set) Kombinationen (Duplikat-Schutz)
│   ├── known_print_ids.json    # Baseline aller Scryfall Print-IDs (letzter Manifest-Stand)
│   └── feed_items.json         # Zuletzt gerenderte Feed-Einträge (Basis für den nächsten Build)
├── docs/                       # GitHub Pages Root
│   ├── feed.xml                # Generierter RSS Feed
│   └── index.html              # Info-Seite
├── scripts/
│   └── generate_feed.py        # Feed-Generator
└── README.md
```

---

## Datenschutz & Lizenz

Kartendaten und -bilder stammen von [Scryfall](https://scryfall.com) und
sind Eigentum von Wizards of the Coast LLC.
Magic: The Gathering ist eine eingetragene Marke von Wizards of the Coast.

Dieses Projekt steht unter der [MIT License](LICENSE).
