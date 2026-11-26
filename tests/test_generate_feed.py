import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree
import urllib.error

from scripts import generate_feed as feed

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_response(payload: bytes):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = payload
    return response


def http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, f"HTTP {code}", headers or {}, None)


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

    def output_lines(self):
        return (self.root / "outputs").read_text(encoding="utf-8").splitlines()

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

    def test_excluded_sets_are_configurable(self):
        self.assertFalse(feed.is_feed_eligible(make_card(set="unk")))
        self.assertFalse(feed.is_feed_eligible(make_card(set="atrk")))
        self.assertTrue(feed.is_feed_eligible(make_card(set="abc")))
        with patch.dict(os.environ, {"EXCLUDED_SETS": " abcd , "}):
            self.assertEqual(feed.excluded_set_codes(), {"abcd"})
            self.assertTrue(feed.is_feed_eligible(make_card(set="unk")))

    def test_excluded_and_duplicate_variants_are_not_hydrated(self):
        feed.save_known_print_ids({"old"})
        feed.save_known_cards({"oracle-known:abc": "2026-01-01T00:00:00+00:00"})
        manifest = {
            "old": {"id": "old", "oracle_id": "oracle-old", "set_code": "abc"},
            "excluded": {"id": "excluded", "oracle_id": "oracle-ex", "set_code": "plst"},
            "known-variant": {"id": "known-variant", "oracle_id": "oracle-known", "set_code": "abc"},
            "new-first": {"id": "new-first", "oracle_id": "oracle-new", "set_code": "abc"},
            "new-second": {"id": "new-second", "oracle_id": "oracle-new", "set_code": "abc"},
        }
        fetch = self.run_generator(manifest, [make_card(id="new-first", oracle_id="oracle-new")])
        requested = [identifier["id"] for call in fetch.call_args_list for identifier in call.kwargs["body"]["identifiers"]]
        self.assertEqual(requested, ["new-first"])
        known_print_ids = feed.load_known_print_ids()
        for identifier in ("excluded", "known-variant", "new-second", "new-first"):
            self.assertIn(identifier, known_print_ids)

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

    def test_multiface_card_shows_both_face_manacosts_and_images(self):
        card = make_card(mana_cost="", image_uris=None, card_faces=[
            {"name": "Front", "mana_cost": "{2}{U}", "oracle_text": "Front text", "image_uris": {"normal": "front.jpg"}},
            {"name": "Back", "mana_cost": "{3}{U}", "oracle_text": "Back text", "image_uris": {"normal": "back.jpg"}},
        ])
        item = ElementTree.fromstring(ElementTree.tostring(feed.build_rss_item(card)))
        description = item.findtext("description")
        self.assertIn("Manakosten: {2}{U} // {3}{U}", description)
        self.assertEqual(item.find("enclosure").get("url"), "front.jpg")
        parser = ImageParser()
        parser.feed(description)
        self.assertEqual([image["src"] for image in parser.images], ["front.jpg", "back.jpg"])
        self.assertEqual(parser.images[1]["alt"], "Example (Rückseite)")

    def test_enclosure_type_matches_image_extension(self):
        png = feed.build_rss_item(make_card(image_uris={"normal": "https://cards.scryfall.io/x.png?v=1"}))
        self.assertEqual(png.find("enclosure").get("type"), "image/png")
        jpeg = feed.build_rss_item(make_card(image_uris={"normal": "https://cards.scryfall.io/x.jpg?v=1"}))
        self.assertEqual(jpeg.find("enclosure").get("type"), "image/jpeg")

    def test_stored_cards_keep_only_used_image_urls(self):
        card = make_card(image_uris={"small": "s.jpg", "normal": "n.jpg", "large": "l.jpg", "png": "p.png"})
        trimmed = feed.trim_card_for_storage(card)
        self.assertEqual(trimmed["image_uris"], {"normal": "n.jpg", "small": "s.jpg"})
        self.assertEqual(feed.card_image_url(trimmed), "n.jpg")
        face_card = make_card(card_faces=[
            {"name": "Front", "mana_cost": "{1}", "image_uris": {"normal": "front.jpg", "large": "large.jpg"}},
        ])
        self.assertEqual(feed.trim_card_for_storage(face_card)["card_faces"][0]["image_uris"], {"normal": "front.jpg"})
        self.assertEqual(feed.trim_image_uris({"png": "p.png"}), {"png": "p.png"})

    def test_known_print_ids_are_stored_compactly(self):
        self.assertTrue(feed.save_known_print_ids({"b", "a"}))
        self.assertEqual(feed.KNOWN_PRINT_IDS_FILE.read_text(encoding="utf-8"), '["a","b"]')
        self.assertEqual(feed.load_known_print_ids(), {"a", "b"})

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
        with patch.object(feed, "MAX_NEW_PRINTS_PER_RUN", 2):
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

    def test_backlog_larger_than_run_limit_is_processed_over_multiple_runs(self):
        feed.save_known_print_ids({"old"})
        cards = {identifier: make_card(id=identifier, oracle_id=identifier) for identifier in ("first", "second", "third")}
        manifest = {identifier: {} for identifier in ("old", *cards)}
        with patch.object(feed, "MAX_NEW_PRINTS_PER_RUN", 2):
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
        self.assertEqual(self.output_lines()[-6:], [
            "new_cards=false", "new_card_count=0", "notification_title=MTG feed updated",
            "notification_message=Feed updated — no new cards detected.",
            "feed_changed=false", "state_changed=true",
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
        self.assertEqual(self.output_lines()[-6:], [
            "new_cards=false", "new_card_count=0", "notification_title=MTG feed updated",
            "notification_message=Feed updated — no new cards detected.",
            "feed_changed=false", "state_changed=false",
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

    def test_failed_collection_batch_keeps_ids_pending(self):
        feed.save_known_print_ids({"old"})
        pending = [f"card-{index}" for index in range(feed.COLLECTION_BATCH_SIZE)]
        manifest = {"old": {}, **{identifier: {} for identifier in (*pending, "last")}}
        bad_request = urllib.error.HTTPError("https://example.invalid", 400, "Bad Request", {}, None)
        with patch.object(feed, "fetch_manifest_entries", return_value=manifest), \
                patch.object(feed, "fetch_json", side_effect=[bad_request, {"data": [make_card(id="last", oracle_id="last")]}]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(feed.main(), 0)
        known_print_ids = feed.load_known_print_ids()
        self.assertIn("last", known_print_ids)
        self.assertNotIn("card-0", known_print_ids)
        self.assertEqual({card["id"] for card in feed.load_feed_items()}, {"last"})

    def test_rate_limit_does_not_sleep_before_giving_up(self):
        limited = urllib.error.HTTPError("https://example.invalid", 429, "Too Many Requests", {}, None)
        with patch.object(feed.urllib.request, "urlopen", side_effect=limited), \
                patch.object(feed.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(urllib.error.HTTPError):
                feed.fetch_json("https://example.invalid", max_retries=1)
        sleep.assert_not_called()

    def test_atomic_write_failure_preserves_previous_file(self):
        target = self.root / "state.json"
        feed.write_text_if_changed(target, "old")
        with patch.object(Path, "replace", side_effect=OSError("Write failed")), self.assertRaises(OSError):
            feed.write_text_if_changed(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_rate_limit_is_retried_and_honours_retry_after(self):
        limited = http_error(429, {"Retry-After": "1"})
        with patch.object(feed.urllib.request, "urlopen", side_effect=[limited, make_response(b'{"ok": true}')]), \
                patch.object(feed.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(feed.fetch_json("https://example.invalid"), {"ok": True})
        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args.args[0], 31)

    def test_transient_failures_are_retried(self):
        for failure in (http_error(503), urllib.error.URLError("offline")):
            with self.subTest(failure=repr(failure)):
                with patch.object(feed.urllib.request, "urlopen", side_effect=[failure, make_response(b'{"ok": true}')]), \
                        patch.object(feed.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(feed.fetch_json("https://example.invalid"), {"ok": True})
                sleep.assert_called_once()

    def test_client_errors_are_not_retried(self):
        with patch.object(feed.urllib.request, "urlopen", side_effect=http_error(400)), \
                patch.object(feed.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                feed.fetch_json("https://example.invalid")
        sleep.assert_not_called()

    def test_invalid_manifest_json_fails_without_touching_state(self):
        feed.save_known_print_ids({"old"})
        with patch.object(feed.urllib.request, "urlopen", return_value=make_response(b"{not json")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(feed.main(), 1)
        self.assertEqual(feed.load_known_print_ids(), {"old"})
        self.assertFalse(feed.OUTPUT_FILE.exists())

    def test_manifest_stops_on_repeated_next_page(self):
        repeated = "https://api.scryfall.com/cards/manifest?page=2"
        pages = [
            {"data": [{"id": "first"}], "next_page": repeated},
            {"data": [{"id": "second"}], "next_page": repeated},
        ]
        with patch.object(feed, "fetch_json", side_effect=pages) as fetch, \
                patch.object(feed.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(list(feed.fetch_manifest_entries()), ["first", "second"])
        self.assertEqual(fetch.call_count, 2)

    def test_card_without_images_has_no_media(self):
        item = ElementTree.fromstring(ElementTree.tostring(feed.build_rss_item(make_card(image_uris=None))))
        self.assertIsNone(item.find("enclosure"))
        self.assertNotIn("<img", item.findtext("description"))

    def test_invalid_discovery_timestamp_falls_back_to_now(self):
        item = feed.build_rss_item(make_card(discovered_at="not-a-date"))
        published = parsedate_to_datetime(item.findtext("pubDate"))
        self.assertLess(abs((datetime.now(timezone.utc) - published).total_seconds()), 120)

    def test_feed_document_is_well_formed_and_not_double_escaped(self):
        card = make_card(name="Adventurer's Map")
        xml_text = feed.build_rss_feed([feed.build_rss_item(card)], datetime.now(timezone.utc))
        self.assertTrue(xml_text.startswith('<?xml version="1.0"?>'))
        root = ElementTree.fromstring(xml_text)
        self.assertEqual(len(root.findall("./channel")), 1)
        description = root.find("./channel/item/description").text
        self.assertNotIn("&amp;#x27;", description)
        self.assertIn("Adventurer's Map", unescape(description))

    def test_all_failed_collection_batches_abort_the_run(self):
        feed.save_known_print_ids({"old"})
        with patch.object(feed, "fetch_manifest_entries", return_value={"old": {}, "new": {}}), \
                patch.object(feed, "fetch_json", side_effect=http_error(400)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(feed.main(), 1)
        self.assertEqual(feed.load_known_print_ids(), {"old"})
        self.assertFalse(feed.OUTPUT_FILE.exists())
        self.assertEqual(feed.load_known_cards(), {})

    def test_multiface_oracle_text_is_joined(self):
        card = make_card(card_faces=[{"oracle_text": "Front text"}, {"oracle_text": "Back text"}])
        self.assertEqual(feed.card_oracle_text(card), "Front text\n//\nBack text")

    def test_write_text_if_changed_creates_directories_and_reports_changes(self):
        target = self.root / "nested" / "deeper" / "state.json"
        self.assertTrue(feed.write_text_if_changed(target, "content"))
        self.assertEqual(target.read_text(encoding="utf-8"), "content")
        self.assertFalse(feed.write_text_if_changed(target, "content"))

    def test_github_output_is_optional(self):
        with patch.dict(os.environ, {"GITHUB_OUTPUT": ""}):
            self.assertIsNone(feed.set_github_output("new_cards", "false"))

    def test_empty_excluded_sets_override_excludes_nothing(self):
        with patch.dict(os.environ, {"EXCLUDED_SETS": ""}):
            self.assertEqual(feed.excluded_set_codes(), set())
            self.assertTrue(feed.is_feed_eligible(make_card(set="plst")))

    def test_published_entries_in_excluded_sets_are_retained(self):
        feed.save_known_print_ids({"old"})
        feed.save_feed_items([make_card(id="kept", oracle_id="kept", set="plst")])
        self.run_generator({"old": {}}, [])
        self.assertEqual([card["set"] for card in feed.load_feed_items()], ["plst"])

    def test_manifest_key_falls_back_to_print_id(self):
        self.assertEqual(feed.manifest_dedup_key({"id": "print", "set_code": "abc"}), "print:abc")
        self.assertEqual(
            feed.manifest_dedup_key({"id": "print", "oracle_id": "oracle", "set_code": "abc"}),
            "oracle:abc",
        )

    def test_notification_lists_top_sets_by_count(self):
        cards = [make_card(set_name="Small")] + [make_card(set_name="Large")] * 3 + [make_card(set_name="Medium")] * 2
        self.assertEqual(
            feed.notification_message(cards),
            "6 new cards: 3× Large, 2× Medium, 1× Small",
        )

    def test_notification_limits_to_top_five_sets(self):
        cards = []
        for index in range(8):
            cards += [make_card(set_name=f"Set {index}")] * (8 - index)
        message = feed.notification_message(cards)
        self.assertTrue(message.startswith("36 new cards: 8× Set 0, 7× Set 1"), message)
        self.assertEqual(message.count("×"), feed.NOTIFICATION_TOP_SETS)
        self.assertIn("+3 more sets (6 cards)", message)

    def test_notification_aggregates_by_set_name(self):
        cards = [
            make_card(set="aaa", set_name="Shared Set"),
            make_card(set="bbb", set_name="Shared Set"),
            make_card(set="ccc", set_name=None),
        ]
        self.assertEqual(feed.summarize_new_cards(cards), [("Shared Set", 2), ("ccc", 1)])
        self.assertEqual(feed.set_label(make_card(set_name=" Name\n\twith   spaces ")), "Name with spaces")
        self.assertEqual(feed.set_label({"set": "xyz"}), "xyz")
        self.assertEqual(feed.set_label({}), "Unknown set")

    def test_notification_title_and_message_are_singular_for_one_card(self):
        cards = [make_card(set_name="Foundations")]
        self.assertEqual(feed.notification_title(cards), "1 new MTG spoiler")
        self.assertEqual(feed.notification_message(cards), "1 new card: 1× Foundations")

    def test_notification_without_new_cards(self):
        self.assertEqual(feed.notification_title([]), "MTG feed updated")
        self.assertEqual(feed.notification_message([]), "Feed updated — no new cards detected.")

    def test_notification_sanitizes_set_names_and_stays_within_limits(self):
        cards = [make_card(set_name="Line\nbreak\tand  tabs")] * 2
        message = feed.notification_message(cards)
        self.assertNotIn("\n", message)
        self.assertIn("2× Line break and tabs", message)

        many = [make_card(set_name="X" * 400 + str(index)) for index in range(5)]
        self.assertLessEqual(len(feed.notification_message(many)), feed.PUSHOVER_MESSAGE_LIMIT)

        self.assertEqual(feed.notification_message([make_card(set_name="Y" * 1100)]), "1 new card.")
        self.assertLessEqual(len(feed.notification_title([{}] * 300)), feed.PUSHOVER_TITLE_LIMIT)

    def test_main_writes_notification_outputs(self):
        feed.save_known_print_ids({"old"})
        manifest = {"old": {}, "a": {}, "b": {}, "c": {}}
        cards = [
            make_card(id="a", oracle_id="a", set_name="Big Set"),
            make_card(id="b", oracle_id="b", set_name="Big Set"),
            make_card(id="c", oracle_id="c", set_name="Small Set"),
        ]
        self.run_generator(manifest, cards)
        outputs = dict(line.split("=", 1) for line in self.output_lines())
        self.assertEqual(outputs["new_card_count"], "3")
        self.assertEqual(outputs["notification_title"], "3 new MTG spoilers")
        self.assertEqual(outputs["notification_message"], "3 new cards: 2× Big Set, 1× Small Set")


class WorkflowTests(unittest.TestCase):
    def workflow_text(self) -> str:
        return (REPO_ROOT / ".github" / "workflows" / "update-feed.yml").read_text(encoding="utf-8")

    def step_blocks(self) -> dict[str, str]:
        """Step name to its raw YAML block, split on the list item markers."""
        blocks = {}
        for piece in self.workflow_text().split("\n      - name: ")[1:]:
            blocks[piece.split("\n", 1)[0].strip()] = piece
        return blocks

    def test_step_conditions_avoid_the_secrets_context(self):
        text = self.workflow_text()
        conditions = [line.strip() for line in text.splitlines() if line.strip().startswith("if:")]
        self.assertTrue(conditions)
        for condition in conditions:
            self.assertNotIn("secrets.", condition, condition)
        self.assertIn('[ -z "$PUSHOVER_TOKEN" ]', text)
        self.assertIn('[ -z "$PUSHOVER_USER" ]', text)

    def test_notification_step_uses_env_indirection(self):
        notification = self.step_blocks()["Send Pushover notification when feed changed"]
        run_block = notification.split("run: |", 1)[1]
        self.assertNotIn("${{", run_block)
        self.assertIn('--form-string "message=$PUSHOVER_MESSAGE"', run_block)
        self.assertIn("PUSHOVER_MESSAGE: ${{ steps.generate.outputs.notification_message }}", notification)

    def test_notification_is_not_gated_on_success(self):
        notification = self.step_blocks()["Send Pushover notification when feed changed"]
        condition = next(line.strip() for line in notification.splitlines() if line.strip().startswith("if:"))
        self.assertNotIn("success()", condition)
        self.assertIn("!cancelled()", condition)
        self.assertIn("feed_changed == 'true'", condition)

    def test_notification_cannot_change_the_run_result(self):
        blocks = self.step_blocks()
        for name in ("Send Pushover notification when feed changed", "Send Pushover notification on failure"):
            self.assertIn("continue-on-error: true", blocks[name], name)

    def test_notification_retries_delivery(self):
        run_block = self.step_blocks()["Send Pushover notification when feed changed"].split("run: |", 1)[1]
        self.assertIn("for attempt in 1 2 3;", run_block)
        self.assertIn("sleep 10", run_block)
        self.assertIn("::warning::", run_block)

    def test_deploy_is_retried_once(self):
        blocks = self.step_blocks()
        self.assertIn("uses: actions/deploy-pages@v5", blocks["Deploy to GitHub Pages"])
        self.assertIn("continue-on-error: true", blocks["Deploy to GitHub Pages"])
        self.assertIn("id: deployment\n", blocks["Deploy to GitHub Pages"])
        retry = blocks["Retry Deploy to GitHub Pages"]
        self.assertIn("uses: actions/deploy-pages@v5", retry)
        self.assertIn("if: steps.deployment.outcome == 'failure'", retry)
        self.assertNotIn("continue-on-error", retry)
        text = self.workflow_text()
        self.assertIn("steps.deployment.outputs.page_url || steps.deployment_retry.outputs.page_url", text)


if __name__ == "__main__":
    unittest.main()