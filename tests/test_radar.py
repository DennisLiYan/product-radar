import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
import urllib.parse
from zoneinfo import ZoneInfo

import radar
import schedule


def rss(entries):
    items = "".join("<item><guid>%s</guid><title>Tool %s</title><link>https://example.org/%s</link>"
                    "<pubDate>%s</pubDate><description>Small useful tool</description></item>" %
                    (identity, identity, identity, radar.stamp(timestamp)) for identity, timestamp in entries)
    return ("<rss><channel>" + items + "</channel></rss>").encode()


class Server:
    def __init__(self, callback):
        self.callback = callback
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                status, data = owner.callback(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if isinstance(data, dict) else "application/xml")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode() if isinstance(data, dict) else data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.server.server_port

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = radar.read_config(radar.ROOT / "config.json")
        self.config.update(http_attempts=1, request_interval_seconds=0, overlap_hours=0)
        self.db = radar.connect(self.root / "radar.db")
        self.now = radar.parse_time("2026-10-11T01:00:00Z")
        self.counter = 0

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def collect(self, source, cutoff):
        self.counter += 1
        run_id = "run%d" % self.counter
        with self.db:
            self.db.execute("INSERT INTO runs(id,started_at,cutoff_at,status) VALUES(?,?,?,'running')",
                            (run_id, cutoff, cutoff))
        client = radar.HTTPClient(self.config, self.root / "raw" / run_id)
        status = radar.collect_source(self.db, source, cutoff, self.config, client, run_id)
        return status

    def test_offline_catchup_boundaries_and_repeat_preserve_first_seen(self):
        cutoff = self.now
        entries = [("old", cutoff - 4 * 86400), ("one", cutoff - 1), ("boundary", cutoff)]
        with Server(lambda p: (200, rss(entries))) as server:
            source = {"id": "test", "name": "Test", "kind": "paged_rss", "url": server.url}
            self.assertEqual(self.collect(source, cutoff), "ok")
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            # Offline for four days: an item from the middle of the gap must survive.
            entries.append(("middle", cutoff + 2 * 86400))
            self.assertEqual(self.collect(source, cutoff + 4 * 86400), "ok")
            rows = {r["external_id"]: dict(r) for r in self.db.execute("SELECT * FROM items")}
            self.assertEqual(set(rows), {"one", "boundary", "middle"})
            self.assertEqual(rows["one"]["first_seen_at"], cutoff)
            self.assertEqual(rows["boundary"]["published_at"], cutoff)
            self.config["overlap_hours"] = 120
            self.assertEqual(self.collect(source, cutoff + 4 * 86400 + 1), "ok")
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)
            self.assertEqual(self.db.execute("SELECT added FROM source_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0], 0)

    def test_failure_keeps_checkpoint_and_initial_start_then_recovers(self):
        with Server(lambda p: (503, b"unavailable")) as server:
            source = {"id": "test", "name": "Test", "kind": "paged_rss", "url": server.url}
            self.assertEqual(self.collect(source, self.now), "failed")
            state = self.db.execute("SELECT * FROM sources").fetchone()
            self.assertIsNone(state["cursor_at"])
            initial = state["initial_since"]
            server.callback = lambda p: (200, rss([("before", initial - 1), ("recovered", self.now - 10)]))
            self.assertEqual(self.collect(source, self.now + 86400), "ok")
            state = self.db.execute("SELECT * FROM sources").fetchone()
            self.assertEqual(state["initial_since"], initial)
            self.assertEqual(state["cursor_at"], self.now + 86400)
            server.callback = lambda p: (503, b"unavailable")
            self.assertEqual(self.collect(source, self.now + 2 * 86400), "failed")
            self.assertEqual(self.db.execute("SELECT cursor_at FROM sources").fetchone()[0], self.now + 86400)

    def test_bounded_feed_gap_persists_after_later_success(self):
        entries = [("one", self.now - 100), ("two", self.now - 10)]
        with Server(lambda p: (200, rss(entries))) as server:
            source = {"id": "test", "name": "Test", "kind": "rss", "url": server.url}
            self.assertEqual(self.collect(source, self.now), "limited")
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0], 1)
            self.assertEqual(self.collect(source, self.now + 10), "ok")
            report = radar.render_report(self.db, self.config, self.now + 10)
            self.assertIn("历史覆盖缺口", report)
            self.assertIn("Tool one", report)
            self.assertIn("Tool two", report)

    def test_rss_pages_reach_old_records_and_repeated_page_fails(self):
        def pages(path):
            return (200, rss([("one", self.now - 1)] if "page=1" in path else
                             [("two", self.now - 86400), ("old", self.now - 4 * 86400)]))
        with Server(pages) as server:
            source = {"id": "test", "name": "Test", "kind": "paged_rss", "url": server.url + "?page={page}"}
            self.assertEqual(self.collect(source, self.now), "ok")
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            old_cursor = self.db.execute("SELECT cursor_at FROM sources").fetchone()[0]
            server.callback = lambda p: (200, rss([("too-new", self.now + 20)]))
            self.assertEqual(self.collect(source, self.now + 30), "failed")
            self.assertEqual(self.db.execute("SELECT cursor_at FROM sources").fetchone()[0], old_cursor)

    def test_hn_large_range_splits_instead_of_truncating(self):
        start = self.now - 2000
        all_hits = [{"objectID": str(i), "created_at_i": start + i, "title": "Show HN: Tool %d" % i,
                     "url": "https://example.org/%d" % i, "points": 1} for i in range(1005)]

        def search(path):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            left, right = map(int, re.findall(r"\d+", q["numericFilters"][0]))
            page = int(q["page"][0])
            matching = [h for h in all_hits if left <= h["created_at_i"] < right]
            visible = matching[:1000]
            return 200, {"hits": visible[page * 100:(page + 1) * 100], "nbHits": len(matching),
                         "nbPages": (len(visible) + 99) // 100, "hitsPerPage": 100}

        with Server(search) as server:
            source = {"id": "hn", "kind": "hn_search", "url": server.url}
            result = radar.collect_hn(source, start, self.now, radar.HTTPClient(self.config, self.root / "raw"))
            self.assertEqual(len(result.items), 1005)
            self.assertEqual(min(i.published_at for i in result.items), start)
            self.assertGreater(len(server.requests), 10)

    def test_hn_source_query_tags_and_minimum_points(self):
        def search(path):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            self.assertEqual(query["tags"], ["story"])
            self.assertEqual(query["query"], ["startup"])
            hits = [
                {"objectID": "strong", "created_at_i": self.now - 2, "title": "A startup product",
                 "url": "https://example.org/strong", "points": 12},
                {"objectID": "weak", "created_at_i": self.now - 1, "title": "A startup idea",
                 "url": "https://example.org/weak", "points": 3},
            ]
            return 200, {"hits": hits, "nbHits": 2, "nbPages": 1, "hitsPerPage": 100}

        with Server(search) as server:
            source = {"id": "hn", "kind": "hn_search", "url": server.url,
                      "tags": "story", "query": "startup", "min_points": 10}
            result = radar.collect_hn(source, self.now - 10, self.now,
                                      radar.HTTPClient(self.config, self.root / "raw"))
            self.assertEqual([item.external_id for item in result.items], ["strong"])
            self.assertIn("至少 10 分", result.note)

    def test_github_trending_snapshot_has_stable_repository_ids(self):
        body = b'''<html><article class="Box-row"><h2 class="h3 lh-condensed">
        <a data-view-component="true" href="/Acme/Useful"><span>Acme /</span> Useful</a></h2>
        <p class="col-9 color-fg-muted my-1">A &amp; B automation tool</p>
        <span class="d-inline-block float-sm-right">1,234 stars today</span></article></html>'''
        items = radar.parse_github_trending(body, self.now - 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "acme/useful")
        self.assertEqual(items[0].title, "Acme/Useful")
        self.assertEqual(items[0].url, "https://github.com/Acme/Useful")
        self.assertEqual(items[0].points, 1234)
        self.assertIn("A & B automation tool", items[0].description)
        with self.assertRaises(ValueError):
            radar.parse_github_trending(b"<html>rate limited</html>", self.now)

    def test_source_commit_rolls_back_items_and_checkpoint_together(self):
        # Force a DB write failure on the second item, after the first INSERT.
        self.db.executescript("""CREATE TRIGGER reject_second BEFORE INSERT ON items
                               WHEN NEW.external_id='two' BEGIN SELECT RAISE(ABORT,'disk write test'); END;""")
        with Server(lambda p: (200, rss([("one", self.now - 10), ("two", self.now - 5),
                                        ("old", self.now - 4 * 86400)]))) as server:
            source = {"id": "test", "name": "Test", "kind": "paged_rss", "url": server.url}
            self.assertEqual(self.collect(source, self.now), "failed")
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
            self.assertIsNone(self.db.execute("SELECT cursor_at FROM sources").fetchone()[0])

    def test_atom_dates_html_and_distinct_urls(self):
        body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>Example</title>
        <link href="https://example.org/project?id=2&amp;utm_source=feed#reply1"/>
        <published>2026-10-10T08:00:00+08:00</published><updated>2026-10-11T08:00:00+08:00</updated>
        <content type="html">&lt;p&gt;Hello&lt;/p&gt;&lt;script&gt;ignore&lt;/script&gt;world</content></entry></feed>'''
        item = radar.parse_feed(body)[0]
        self.assertEqual(item.published_at, radar.parse_time("2026-10-10T00:00:00Z"))
        self.assertEqual(item.description, "Hello world")
        self.assertEqual(item.url, "https://example.org/project?id=2")
        self.assertNotEqual(item.url, radar.canonical_url("https://example.org/project?id=3"))
        with self.assertRaises(ValueError):
            radar.parse_feed(b"<html>blocked</html>")

    def test_daily_due_and_schedule_are_independent_of_codex(self):
        morning = radar.parse_time("2026-10-11T00:59:00Z")
        self.assertEqual(radar.due_timestamp(morning, self.config), radar.parse_time("2026-10-10T01:00:00Z"))
        self.assertEqual(radar.due_timestamp(self.now, self.config), self.now)
        spec = schedule.spec(self.root, "/usr/bin/python3", self.config)
        self.assertEqual(spec["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertTrue(spec["RunAtLoad"])
        self.assertIn("--if-due", spec["ProgramArguments"])
        self.assertNotIn("KeepAlive", spec)

    def test_exclusive_process_lock_and_atomic_report(self):
        lock = self.root / "run.lock"
        with radar.process_lock(lock):
            with self.assertRaises(RuntimeError):
                with radar.process_lock(lock):
                    pass
        with radar.process_lock(lock):
            radar.atomic_write(self.root / "latest.md", "完整日报")
        self.assertEqual((self.root / "latest.md").read_text(), "完整日报")
        self.assertFalse(list(self.root.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
