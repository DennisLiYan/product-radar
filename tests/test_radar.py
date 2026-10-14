import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
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

    def test_run_retries_failed_sources_after_a_pause(self):
        responses = [(503, b"waking up"), (200, rss([("fresh", self.now - 10)]))]
        with Server(lambda p: responses.pop(0) if len(responses) > 1 else responses[0]) as server:
            source = {"id": "test", "name": "Test", "kind": "rss", "url": server.url}
            self.config.update(sources=[source], retry_failed_after_seconds=0, llm={"enabled": False})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(radar.run(self.root, self.config), 0)
        db = radar.connect(self.root / "data/radar.db")
        try:
            self.assertEqual([tuple(r) for r in db.execute("SELECT source_id,status FROM source_runs")], [("test", "limited")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
        finally:
            db.close()

    def test_ranked_feed_records_positions_and_v2ex_api_keeps_reply_counts(self):
        entries = [("first", self.now - 50), ("second", self.now - 40)]
        with Server(lambda p: (200, rss(entries))) as server:
            source = {"id": "top", "name": "Top", "kind": "rss", "ranked": True, "url": server.url}
            self.assertEqual(self.collect(source, self.now), "limited")
        ranks = dict(self.db.execute("SELECT external_id, rank FROM item_ranks").fetchall())
        self.assertEqual(ranks, {"first": 1, "second": 2})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0], 0)
        topics = [{"id": 7, "title": "我做了一个工具", "content": "介绍", "replies": 12, "created": self.now - 100},
                  {"id": 6, "title": "旧帖", "content": "", "replies": 3, "created": self.now - 10 * 86400}]
        with Server(lambda p: (200, json.dumps(topics).encode())) as server:
            source = {"id": "v2", "name": "V2", "kind": "v2ex_api", "url": server.url}
            self.assertEqual(self.collect(source, self.now), "limited")
        row = self.db.execute("SELECT * FROM items WHERE source_id='v2'").fetchone()
        self.assertEqual((row["external_id"], row["comments"], row["url"]), ("v2ex:7", 12, "https://www.v2ex.com/t/7"))

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
            self.assertIn("points>=10", query["numericFilters"][0])
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

    def test_hn_thread_keeps_linked_top_level_replies(self):
        thread = {"objectID": "100", "title": "Ask HN: What are you working on? (October 2026)",
                  "created_at_i": self.now - 5 * 86400}
        other = {"objectID": "200", "title": "Ask HN: Who is hiring?", "created_at_i": self.now - 86400}
        text = "I'm building Acme. A tool that turns invoices into ledgers for small shops. "
        comments = [
            {"objectID": "1", "parent_id": 100, "created_at_i": self.now - 60,
             "comment_text": text + '<a href="https:&#x2F;&#x2F;acme.example&#x2F;">acme.example</a>'},
            {"objectID": "2", "parent_id": 1, "created_at_i": self.now - 50,
             "comment_text": "Nice! " + text + '<a href="https://reply.example/">x</a>'},
            {"objectID": "3", "parent_id": 100, "created_at_i": self.now - 40, "comment_text": text},
            {"objectID": "4", "parent_id": 100, "created_at_i": self.now - 30,
             "comment_text": 'Short <a href="https://short.example/">x</a>'},
        ]

        def respond(path):
            tags = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["tags"][0]
            hits = [thread, other] if tags.startswith("story,author_") else comments
            return 200, {"hits": hits, "nbHits": len(hits), "nbPages": 1, "hitsPerPage": 100}

        with Server(respond) as server:
            source = {"id": "thread", "name": "Thread", "kind": "hn_thread", "url": server.url,
                      "author": "david927", "thread_query": "What are you working on",
                      "title_pattern": "^Ask HN: What are you working on\\?", "min_chars": 40}
            self.assertEqual(self.collect(source, self.now), "ok")
            self.assertTrue(any("story_100" in urllib.parse.unquote(p) for p in server.requests))
            self.assertFalse(any("story_200" in urllib.parse.unquote(p) for p in server.requests))
        rows = list(self.db.execute("SELECT * FROM items"))
        self.assertEqual([r["external_id"] for r in rows], ["1"])
        self.assertEqual(rows[0]["url"], "https://acme.example/")
        self.assertEqual(rows[0]["title"], "I'm building Acme.")
        self.assertEqual(rows[0]["discussion_url"], "https://news.ycombinator.com/item?id=1")

    def test_hellogithub_splits_issue_into_projects(self):
        data = {"props": {"pageProps": {"volume": {"data": [{"category_name": "Python 项目", "items": [
            {"full_name": "Acme/Tool", "github_url": "https://github.com/Acme/Tool",
             "description": "命令行效率工具", "stars": 42},
            {"full_name": "", "github_url": ""}]}]}}}}
        page = ('<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>'
                % json.dumps(data, ensure_ascii=False)).encode()

        def respond(path):
            if path.startswith("/periodical/"):
                return 200, page
            return 200, ("<rss><channel><item><guid>v1</guid><title>HelloGitHub 第 1 期</title>"
                         "<link>%speriodical/volume/1</link><pubDate>%s</pubDate></item></channel></rss>"
                         % (server.url, radar.stamp(self.now - 3600))).encode()

        with Server(respond) as server:
            source = {"id": "hg", "name": "HelloGitHub", "kind": "hellogithub", "url": server.url + "rss"}
            self.assertEqual(self.collect(source, self.now), "ok")
            issue = server.url + "periodical/volume/1"
        row = self.db.execute("SELECT * FROM items").fetchone()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
        self.assertEqual((row["external_id"], row["url"]), ("acme/tool", "https://github.com/Acme/Tool"))
        self.assertEqual(row["discussion_url"], radar.canonical_url(issue))
        self.assertIn("命令行效率工具", row["description"])
        with self.assertRaises(ValueError):
            radar.parse_hellogithub_volume(b"<html></html>", issue, self.now)

    def test_hf_spaces_snapshot_keeps_recent_spaces(self):
        spaces = [{"id": "acme/Demo", "createdAt": radar.stamp(self.now - 86400), "likes": 7, "sdk": "gradio",
                   "cardData": {"title": "Acme Demo", "short_description": "Turns photos into floor plans"}},
                  {"id": "old/Space", "createdAt": radar.stamp(self.now - 90 * 86400), "likes": 900}]
        with Server(lambda p: (200, json.dumps(spaces).encode())) as server:
            source = {"id": "hf", "name": "HF", "kind": "hf_spaces", "url": server.url, "max_age_days": 30}
            self.assertEqual(self.collect(source, self.now), "limited")
        rows = list(self.db.execute("SELECT * FROM items"))
        self.assertEqual([r["external_id"] for r in rows], ["acme/demo"])
        self.assertEqual(rows[0]["title"], "Acme Demo")
        self.assertEqual(rows[0]["url"], "https://huggingface.co/spaces/acme/Demo")
        self.assertIn("Turns photos into floor plans", rows[0]["description"])

    def test_sitemap_filters_product_paths_and_uses_update_date(self):
        body = b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://www.ycombinator.com/launches/ABC-useful-product</loc><lastmod>2026-10-10</lastmod></url>
        <url><loc>https://www.ycombinator.com/launches/sitemap</loc><lastmod>2026-10-11</lastmod></url>
        <url><loc>https://www.ycombinator.com/about</loc><lastmod>2026-10-11</lastmod></url>
        </urlset>'''
        source = {"path_pattern": r"^/launches/[^/]+$", "exclude_paths": ["/launches/sitemap"],
                  "strip_code_prefix": True}
        items, skipped = radar.parse_sitemap(body, source)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "useful product")
        self.assertEqual(items[0].published_at, radar.parse_time("2026-10-10T00:00:00Z"))

    def test_yc_launches_uses_created_at_and_product_url(self):
        class Client:
            def json(inner, url):
                return {"hits": [
                    {"id": 7, "title": "Acme: useful product", "tagline": "One clear job",
                     "slug": "ABC-acme", "created_at": radar.stamp(self.now - 2), "total_vote_count": 9,
                     "search_path": "https://www.ycombinator.com/launches/ABC-acme",
                     "company": {"url": "https://acme.example/", "tags": ["SaaS"], "batch": "Fall 2026"}},
                    {"id": 6, "title": "Older", "tagline": "Old", "slug": "XYZ-older",
                     "created_at": radar.stamp(self.now - 20), "total_vote_count": 1,
                     "search_path": "https://www.ycombinator.com/launches/XYZ-older", "company": {}},
                ], "nbPages": 1}

        result = radar.collect_yc_launches({"url": "https://example.test/?page={page}"},
                                           self.now - 10, self.now, Client())
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].external_id, "7")
        self.assertEqual(result.items[0].url, "https://acme.example/")
        self.assertEqual(result.items[0].points, 9)
        self.assertIn("Fall 2026", result.items[0].description)

    def test_betalist_parser_ignores_undated_trending_cards(self):
        body = b'''<a href="/startups/trending"><div class="font-medium text-gray-900">Trending</div></a>
        <div data-duplicate-id="day_1791590400"><strong>Yesterday</strong></div>
        <div class="startup-row"><a class="absolute inset-0" href="/startups/useful"></a>
        <span class="shrink-0 font-medium text-gray-900">Useful</span>
        <span class="text-base text-gray-500 lg:truncate">Does one useful job</span></div>'''
        items = radar.parse_betalist(body)
        self.assertEqual([item.external_id for item in items], ["useful"])
        self.assertEqual(items[0].title, "Useful")
        self.assertEqual(items[0].description, "Does one useful job")

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

    def test_proxy_has_an_explicit_switch(self):
        config = {"proxy_enabled": True, "proxy_url": "http://127.0.0.1:1087"}
        self.assertEqual(radar.effective_proxy(config), "http://127.0.0.1:1087")
        config["proxy_enabled"] = False
        self.assertIsNone(radar.effective_proxy(config))

    def test_linux_and_windows_schedules(self):
        service, timer = schedule.systemd_units(self.root, "/usr/bin/python3", self.config)
        self.assertIn("radar.py\" \"run\" \"--if-due\"", service)
        self.assertIn("OnCalendar=*-*-* 09:00:00 " + self.config["timezone"], timer)
        self.assertIn("Persistent=true", timer)
        daily, reboot = schedule.cron_lines(self.root, "/usr/bin/python3", self.config)
        self.assertTrue(daily.startswith("0 9 * * * ") and reboot.startswith("@reboot "))
        self.assertTrue(daily.endswith(schedule.CRON_MARK + str(self.root)))
        xml = schedule.windows_task_xml(self.root, "C:\\Python\\python.exe", self.config, "PC\\me")
        import xml.etree.ElementTree as ET
        task = ET.fromstring(xml.encode("utf-16"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(task.find(".//t:CalendarTrigger/t:StartBoundary", ns).text[-8:], "09:00:00")
        self.assertIsNotNone(task.find(".//t:LogonTrigger", ns))
        self.assertEqual(task.find(".//t:StartWhenAvailable", ns).text, "true")
        self.assertIn("--if-due", task.find(".//t:Arguments", ns).text)
        self.assertIn(schedule.windows_marker(self.root), xml)

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
