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

Nicht auflösbare IDs werden in späteren Läufen erneut angefragt. Pro Lauf werden
höchstens 500 neue Print-IDs verarbeitet. Größere Rückstände verteilen sich auf
mehrere Läufe, damit Einträge nicht schon vor ihrer ersten Ausgabe verschwinden.
Beim ersten Lauf ohne Baseline werden nur die ersten 500 Prints des nach
Veröffentlichung absteigend sortierten Manifests für den Start-Feed berücksichtigt;
der Rest bildet den historischen Ausgangsbestand. Filter und Varianten können
dazu führen, dass der Start-Feed weniger als 500 Einträge enthält.

Karten werden pro `(oracle_id, set)`-Kombination getrackt (`data/known_cards.json`):
ein Nachdruck in einem neuen Set erzeugt also einen neuen Feed-Eintrag,
mehrere Varianten (Foil, Showcase, …) im selben Set aber nur einen.

Nachdrucke sind enthalten, ausgenommen ausgeschlossene Sets wie `plst` (The List).
RSS-GUIDs verwenden dieselbe Identität `(oracle_id, set)`. Einträge werden nach
einem dauerhaft gespeicherten Entdeckungszeitpunkt sortiert, der auch als RSS-
`pubDate` dient, nicht nach dem Veröffentlichungsdatum der Karte. Der Feed enthält
die 500 zuletzt entdeckten Einträge. Reader müssen weiterhin regelmäßig abrufen:
Der begrenzte Feed ist kein vollständiges Archiv.

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

Der erste Lauf befüllt die Zustandsdateien und generiert `docs/feed.xml`, sofern
noch keine Baseline existiert. Ein Fork mit Zustandsdateien setzt diesen Stand
fort. Ab dann läuft der Workflow automatisch alle 2 Stunden.

Zustandsänderungen werden auch ohne neue Feed-Einträge committed. Pushes nach
`master` und manuelle Läufe veröffentlichen die Seite unabhängig von neuen Karten;
geplante Läufe deployen nur bei geändertem Feed. Der optionale Boolean
`force_commit` fordert den Commit/Push-Schritt auch ohne gemeldete Änderungen an;
ein leerer Commit wird nicht erstellt. Regressionstests laufen vor jeder Erzeugung.

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
| `MAX_FEED_ENTRIES` | `500` | Maximale Feed-Einträge und pro Lauf verarbeitete neue Print-IDs |
| `EXCLUDED_SET_CODES` | `{"plst"}` | Set-Codes, deren Karten nie als "neu" gelten (z. B. "The List") |

Die optionale Umgebungsvariable `FEED_URL` legt die absolute RSS-Self-URL fest.
GitHub Actions leitet sie aus der Pages-URL ab, einschließlich eigener Domains.
Eine Repository-Variable namens `FEED_URL` kann diesen Wert überschreiben.
Bei lokaler Ausführung ohne diese Variable entfällt der optionale Self-Link.

---

## Lokale Ausführung

```bash
# Abhängigkeiten: keine (nur Python 3.11+ Stdlib)
python scripts/generate_feed.py
```

Der generierte Feed liegt dann unter `docs/feed.xml`.

Gespeicherte Einträge ohne Scryfall-Zugriff neu erzeugen oder migrieren:

```bash
python scripts/generate_feed.py --rebuild
```

Bestehende Einträge erhalten ihren gespeicherten Entdeckungszeitpunkt aus
`known_cards.json`, auch aus alten Schlüsseln ohne Set. Fehlt ein Zeitstempel,
wird einmalig der Migrationszeitpunkt gespeichert. Durch die Umstellung der GUIDs
auf Karte und Set können Reader vorhandene Einträge einmal erneut anzeigen.
Von älteren Versionen bereits übersprungene Karten werden nicht automatisch
nachgeliefert; der historische Ausgangsbestand bleibt erhalten.

Dateien werden atomar ersetzt. Feed und Feed-Einträge werden vor dem Fortschreiben
der bekannten Karten und Print-IDs gespeichert. Unveränderte Läufe erhalten
Dateiinhalte und `lastBuildDate`. Die Webseite zeigt diesen tatsächlichen
Feed-Zeitstempel oder einen Nicht-verfügbar-Status bei fehlgeschlagenem Abruf.

## Tests

```bash
python -m unittest discover -s tests -v
```

Die Tests verwenden temporäre Verzeichnisse und simulierte API-Antworten. Sie
verändern den gespeicherten Feed nicht und rufen Scryfall nicht auf. Abgedeckt
sind fehlende IDs, Bootstrap, Rückstände, Filter, GUIDs, HTML-Escaping,
Entdeckungszeitpunkte, Migration, unveränderte Läufe, Schreibfehler,
Paginierung und Collection-Batches.

Lokale Browser-Vorschau einschließlich Abruf des Feed-Zeitstempels:

```bash
python -m http.server 8000 --bind 127.0.0.1 --directory docs
```

Aufruf über `http://127.0.0.1:8000/` oder `http://127.0.0.1:8000/index.html`.

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
├── tests/
│   └── test_generate_feed.py    # Offline-Regressionstests
└── README.md
```

---

## Datenschutz & Lizenz

Kartendaten und -bilder stammen von [Scryfall](https://scryfall.com) und
sind Eigentum von Wizards of the Coast LLC.
Magic: The Gathering ist eine eingetragene Marke von Wizards of the Coast.

Dieses Projekt steht unter der [MIT License](LICENSE).
