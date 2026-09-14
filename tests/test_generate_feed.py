import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from scripts import generate_feed as feed


def make_card(**changes):
    card = {
        "id": "new", "oracle_id": "oracle", "set": "abc",
        "name": "Example", "set_name": "Example Set", "reprint": False,
        "released_at": "2030-10-01", "scryfall_uri": "https://scryfall.com/card/abc/1",
        "image_uris": {"normal": "https://cards.scryfall.io/example.jpg"},
    }
    card.update(changes)
    return card


class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


class FeedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, filename in {
            "DATA_FILE": "known_cards.json",
            "KNOWN_PRINT_IDS_FILE": "known_print_ids.json",
            "FEED_ITEMS_FILE": "feed_items.json",
            "OUTPUT_FILE": "feed.xml",
        }.items():
            patcher = patch.object(feed, name, self.root / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.root / "outputs"), "FEED_URL": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def run_generator(self, manifest, cards, missing=()):
        response = {"data": cards, "not_found": [{"id": identifier} for identifier in missing]}
        with patch.object(feed, "fetch_manifest_entries", return_value=manifest), \
                patch.object(feed, "fetch_json", return_value=response) as fetch, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(feed.main(), 0)
        return fetch

    def test_guids_follow_oracle_and_set_identity(self):
        first = make_card()
        other_set = make_card(id="other", set="xyz")
        variant = make_card(id="variant")
        guid = lambda card: feed.build_rss_item(card).findtext("guid")
        self.assertNotEqual(guid(first), guid(other_set))
        self.assertEqual(guid(first), guid(variant))

    def test_reprints_are_selected_but_variants_and_excluded_sets_are_not(self):
        cards = [make_card(reprint=True), make_card(id="variant"), make_card(id="excluded", set="plst")]
        selected = feed.select_new_cards_for_feed(cards, {})
        self.assertEqual([card["id"] for card in selected], ["new"])

    def test_html_attributes_and_text_survive_xml_roundtrip(self):
        card = make_card(name='"Quoted" & <Example>', oracle_text="A < B & C\nNext line")
        item = ElementTree.fromstring(ElementTree.tostring(feed.build_rss_item(card)))
        description = item.findtext("description")
        parser = ImageParser()
        parser.feed(description)
        self.assertEqual(parser.images[0]["alt"], card["name"])
        self.assertEqual(set(parser.images[0]), {"src", "alt", "style"})
        self.assertIn("A &lt; B &amp; C<br/>Next line", description)

    def test_pub_date_uses_discovery_time(self):
        card = make_card(discovered_at="2026-09-14T12:00:00+00:00")
        self.assertEqual(feed.build_rss_item(card).findtext("pubDate"), "Mon, 14 Sep 2026 12:00:00 +0000")

    def test_missing_collection_card_is_retried_next_run(self):
        feed.save_known_print_ids({"old"})
        manifest = {identifier: {} for identifier in ("old", "new", "missing")}
        self.run_generator(manifest, [make_card()], missing=["missing"])
        self.assertNotIn("missing", feed.load_known_print_ids())
        fetch = self.run_generator(manifest, [make_card(id="missing", oracle_id="missing-oracle")])
        self.assertEqual(fetch.call_args.kwargs["body"]["identifiers"], [{"id": "missing"}])
        self.assertIn("missing", feed.load_known_print_ids())

    def test_old_release_is_published_before_existing_items(self):
        feed.save_known_print_ids({"old"})
        old = make_card(id="old", oracle_id="old-oracle", discovered_at="2026-01-01T00:00:00+00:00")
        feed.save_feed_items([old])
        with patch.object(feed, "MAX_FEED_ENTRIES", 1):
            self.run_generator({"old": {}, "new": {}}, [make_card(released_at="2000-01-01")])
        self.assertEqual(feed.load_feed_items()[0]["id"], "new")

    def test_bootstrap_does_not_replay_catalog_but_retries_missing_seed(self):
        manifest = {identifier: {} for identifier in ("new", "missing", "historical")}
        with patch.object(feed, "MAX_FEED_ENTRIES", 2):
            self.run_generator(manifest, [make_card()], missing=["missing"])
        self.assertEqual(feed.load_known_print_ids(), {"new", "historical"})
        self.assertTrue(feed.OUTPUT_FILE.exists())

    def test_configurable_self_url(self):
        with patch.dict(os.environ, {"FEED_URL": "https://example.org/custom/feed.xml"}):
            root = ElementTree.fromstring(feed.build_rss_feed([], datetime.now(timezone.utc)))
        link = root.find("./channel/{http://www.w3.org/2005/Atom}link")
        self.assertEqual(link.get("href"), "https://example.org/custom/feed.xml")

    def test_no_self_url_is_better_than_an_unrelated_repository(self):
        root = ElementTree.fromstring(feed.build_rss_feed([], datetime.now(timezone.utc)))
        self.assertIsNone(root.find("./channel/{http://www.w3.org/2005/Atom}link"))

    def test_backlog_larger_than_feed_is_processed_over_multiple_runs(self):
        feed.save_known_print_ids({"old"})
        cards = {identifier: make_card(id=identifier, oracle_id=identifier) for identifier in ("first", "second", "third")}
        manifest = {identifier: {} for identifier in ("old", *cards)}
        with patch.object(feed, "MAX_FEED_ENTRIES", 2):
            fetch = self.run_generator(manifest, [cards["first"], cards["second"]])
            self.assertEqual(len(fetch.call_args.kwargs["body"]["identifiers"]), 2)
            self.assertNotIn("third", feed.load_known_print_ids())
            self.assertEqual({card["id"] for card in feed.load_feed_items()}, {"first", "second"})
            self.run_generator(manifest, [cards["third"]])
        self.assertIn("third", feed.load_known_print_ids())
        self.assertEqual(feed.load_feed_items()[0]["id"], "third")

    def test_filtered_only_run_updates_state_without_changing_feed(self):
        feed.save_known_print_ids({"old"})
        self.run_generator({"old": {}}, [])
        before = feed.OUTPUT_FILE.read_bytes()
        self.run_generator({"old": {}, "new": {}}, [make_card(set="plst")])
        self.assertIn("new", feed.load_known_print_ids())
        self.assertEqual(feed.OUTPUT_FILE.read_bytes(), before)
        self.assertEqual((self.root / "outputs").read_text().splitlines()[-3:], [
            "new_cards=false", "feed_changed=false", "state_changed=true",
        ])

    def test_unchanged_run_preserves_dates_and_file_contents(self):
        feed.save_known_print_ids({"old"})
        manifest = {"old": {}, "new": {}}
        self.run_generator(manifest, [make_card()])
        paths = [feed.OUTPUT_FILE, feed.FEED_ITEMS_FILE, feed.DATA_FILE, feed.KNOWN_PRINT_IDS_FILE]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        fetch = self.run_generator(manifest, [])
        fetch.assert_not_called()
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})
        self.assertEqual((self.root / "outputs").read_text().splitlines()[-3:], [
            "new_cards=false", "feed_changed=false", "state_changed=false",
        ])

    def test_rebuild_migrates_legacy_dates_and_deduplicates_without_network(self):
        timestamp = "2026-01-02T03:04:05+00:00"
        feed.save_known_cards({"oracle": timestamp})
        feed.save_known_print_ids({"new", "variant"})
        feed.save_feed_items([make_card(), make_card(id="variant")])
        with patch.object(feed, "fetch_manifest_entries") as manifest, \
                patch.object(feed, "fetch_json") as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(feed.main(rebuild_only=True), 0)
        manifest.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(len(feed.load_feed_items()), 1)
        self.assertEqual(feed.load_feed_items()[0]["discovered_at"], timestamp)
        self.assertEqual(feed.load_known_cards()["oracle:abc"], timestamp)
        self.assertEqual(feed.load_known_print_ids(), {"new", "variant"})

    def test_rebuild_without_baseline_does_not_skip_future_bootstrap(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(feed.main(rebuild_only=True), 0)
        self.assertIsNone(feed.load_known_print_ids())
        self.assertTrue(feed.OUTPUT_FILE.exists())

    def test_empty_bootstrap_still_creates_feed(self):
        self.run_generator({}, [])
        self.assertEqual(feed.load_known_print_ids(), set())
        self.assertEqual(ElementTree.parse(feed.OUTPUT_FILE).findall("./channel/item"), [])

    def test_feed_write_failure_does_not_advance_baseline(self):
        feed.save_known_print_ids({"old"})
        with patch.object(feed, "write_feed", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.run_generator({"old": {}, "new": {}}, [make_card()])
        self.assertEqual(feed.load_known_print_ids(), {"old"})
        self.assertEqual(feed.load_known_cards(), {})

    def test_api_failure_does_not_advance_baseline(self):
        feed.save_known_print_ids({"old"})
        with patch.object(feed, "fetch_manifest_entries", return_value={"old": {}, "new": {}}), \
                patch.object(feed, "fetch_json", side_effect=OSError("Offline")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(feed.main(), 1)
        self.assertEqual(feed.load_known_print_ids(), {"old"})
        self.assertFalse(feed.OUTPUT_FILE.exists())

    def test_collection_batches_respect_limits(self):
        identifiers = [f"card-{index}" for index in range(76)]
        with patch.object(feed, "fetch_json", side_effect=[{"data": [make_card()]}, {"data": []}]) as fetch, \
                patch.object(feed.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(len(feed.hydrate_cards(identifiers)), 1)
        self.assertEqual([len(call.kwargs["body"]["identifiers"]) for call in fetch.call_args_list], [75, 1])
        sleep.assert_called_once_with(feed.COLLECTION_MIN_INTERVAL)

    def test_manifest_follows_pagination_and_rate_limit(self):
        pages = [
            {"data": [{"id": "first"}], "next_page": "https://api.scryfall.com/cards/manifest?page=2"},
            {"data": [{"id": "second"}]},
        ]
        with patch.object(feed, "fetch_json", side_effect=pages) as fetch, \
                patch.object(feed.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(list(feed.fetch_manifest_entries()), ["first", "second"])
        self.assertEqual(fetch.call_args.args[0], pages[0]["next_page"])
        sleep.assert_called_once_with(feed.MANIFEST_MIN_INTERVAL)

    def test_atomic_write_failure_preserves_previous_file(self):
        target = self.root / "state.json"
        feed.write_text_if_changed(target, "old")
        with patch.object(Path, "replace", side_effect=OSError("Write failed")), self.assertRaises(OSError):
            feed.write_text_if_changed(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(list(self.root.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()