# MTG Spoiler RSS Feed

Automatisch generierter RSS Feed für neu gespoilerte und veröffentlichte
Magic: The Gathering Karten. Daten kommen von der [Scryfall API](https://scryfall.com/docs/api),
der Build läuft alle 2 Stunden via GitHub Actions, das Ergebnis wird auf
GitHub Pages veröffentlicht.

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
Ab dann läuft der Workflow automatisch alle 2 Stunden.

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
| `LOOKBACK_DAYS` | `14` | Wie weit zurück nach neuen Karten gesucht wird |
| `MAX_FEED_ENTRIES` | `250` | Maximale Anzahl Einträge im Feed |

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
│       └── update-feed.yml   # GitHub Actions Workflow
├── data/
│   └── known_cards.json      # Bekannte Oracle IDs (Duplikat-Schutz)
├── docs/                     # GitHub Pages Root
│   ├── feed.xml              # Generierter RSS Feed
│   └── index.html            # Info-Seite
├── scripts/
│   └── generate_feed.py      # Feed-Generator
└── README.md
```

---

## Datenschutz & Lizenz

Kartendaten und -bilder stammen von [Scryfall](https://scryfall.com) und
sind Eigentum von Wizards of the Coast LLC.
Magic: The Gathering ist eine eingetragene Marke von Wizards of the Coast.

Dieses Projekt steht unter der [MIT License](LICENSE).
