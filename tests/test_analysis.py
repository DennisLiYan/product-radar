import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import analysis
import radar


def card(identity):
    return {"id": identity, "name": "测试产品", "one_liner": "面向小团队的任务整理工具",
            "core_function": "把零散任务归并成可检索的项目清单", "positioning": "轻量任务整理工具",
            "target_customers": "需要整理重复任务的小团队", "use_case": "每周汇总项目任务",
            "category": "工作效率", "product_stage": "已发布",
            "scores": {"specificity": 4, "positioning": 4, "customer": 3, "novelty": 3, "learnability": 4, "traction": 2},
            "themes": ["Web SaaS", "团队协作"],
            "why_interesting": "工作流具体，但采用情况未知", "confidence": "低", "is_product": True}


def synthesis(identity):
    return {"headline": "本批出现面向小团队的任务整理产品",
            "batch_summary": [{"text": "样本仅为首日产品观察基线", "evidence_ids": [identity]}],
            "customer_patterns": [{"name": "小团队任务整理", "customers": "小团队成员", "shared_need": "汇总重复任务",
                                   "products_comparison": "测试产品集中管理项目清单", "implication": "客户判断仍待核实",
                                   "evidence_ids": [identity]}],
            "product_patterns": [{"name": "轻量整理", "observation": "用结构化清单替代零散记录",
                                  "examples": "测试产品", "what_is_different": "尚无足够产品可比较",
                                  "evidence_ids": [identity]}],
            "derived_thoughts": [{"title": "从整理走向检索", "starting_point": "产品将任务结构化",
                                  "target_customer": "重复处理任务的小团队", "product_direction": "增加历史任务检索",
                                  "reasoning": "结构化数据可以复用", "caveat": "这是模型推断",
                                  "evidence_ids": [identity]}],
            "watch_signals": [{"text": "观察用户是否持续维护清单", "evidence_ids": [identity]}],
            "limitations": ["测试资料，不代表真实分析"]}


class ModelFixture:
    """Local HTTP contract fixture, never a substitute for the live model check."""
    def __init__(self):
        self.stages = []
        self.fail_synthesis = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("Authorization") != "Bearer fixture-key":
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = json.loads(body["messages"][1]["content"])
                prompt = body["messages"][0]["content"]
                if "对输入 items" in prompt:
                    stage = "screen"
                    output = {"items": [card(x["id"]) for x in payload["items"]]}
                elif "你已经拿到" in prompt:
                    stage = "select"
                    output = {"shortlist": [{"id": payload["items"][0]["id"], "why": "具体产品",
                                               "question": "谁在使用", "previous_product": "",
                                               "is_material_update": False}]}
                elif "根据输入 products" in prompt:
                    stage = "deep"
                    products = []
                    for p in payload["products"]:
                        evidence_id = p["evidence"][0]["id"]
                        record = {k: "明确待验证的产品分析" for k in ("name", "one_liner", "positioning", "workflow",
                                  "value_proposition", "alternatives", "differentiation", "pricing_fact",
                                  "business_stage", "watch_reason")}
                        record.update(id=p["id"],
                                      core_features=[{"text": "发布者称可整理任务", "evidence_ids": [evidence_id]}],
                                      customer_segments=[{"segment": "小团队", "role": "使用者", "scenario": "整理任务",
                                                          "need": "减少遗漏", "basis": "模型推断", "evidence_ids": [evidence_id]}],
                                      community_feedback=[],
                                      reception={"signals": "来源未提供热度数据", "why": "工作流具体，属于推断",
                                                 "evidence_ids": [evidence_id]},
                                      derived_thoughts=[{"title": "检索扩展", "analysis": "可扩展历史检索，属于推断",
                                                         "takeaway": "把清单做成可检索的历史库",
                                                         "applies_when": "任务重复度高的小团队",
                                                         "evidence_ids": [evidence_id]}],
                                      risks=["需求待验证"], unknowns=["价格未知"])
                        products.append(record)
                    output = {"products": products}
                elif "重点产品档案复核员" in prompt:
                    stage = "product_audit"
                    output = {"products": payload["products"]}
                elif "事实与措辞复核员" in prompt:
                    stage = "audit"
                    output = {"synthesis": payload["synthesis"], "quality_notes": ["已检查确定性措辞"]}
                else:
                    stage = "synthesize"
                    output = synthesis(payload["screened"][0]["id"])
                owner.stages.append(stage)
                if stage == "synthesize" and owner.fail_synthesis:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": "fixture-response", "model": "fixture-model",
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20}}).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class AnalysisTests(unittest.TestCase):
    def test_validators_reject_missing_ids_and_fabricated_citations(self):
        with self.assertRaises(ValueError):
            analysis.screen_validator(["a", "b"])( {"items": [card("a")]} )
        with self.assertRaises(ValueError):
            analysis.screen_validator(["a"])( {"items": [card("a"), card("a")]} )
        with self.assertRaises(ValueError):
            analysis.synthesis_validator({"a"})(synthesis("invented"))
        with self.assertRaises(ValueError):
            analysis.selection_validator({"a"}, 1)({"shortlist": [{"id": "b", "why": "reason", "question": "question",
                                                                      "previous_product": "", "is_material_update": False}]})

    def test_model_failure_resume_cache_and_collection_checkpoint_isolation(self):
        with tempfile.TemporaryDirectory() as temporary, ModelFixture() as api:
            root = Path(temporary)
            (root / ".env").write_text("TEST_RADAR_LLM_KEY=fixture-key\n")
            config = radar.read_config(radar.ROOT / "config.json")
            config["llm"].update(base_url="http://127.0.0.1:%d" % api.server.server_port,
                                 api_key_env="TEST_RADAR_LLM_KEY", max_attempts=1, concurrency=1,
                                 fetch_product_pages=False, batch_size=16)
            config["sources"].append({"id": "test", "name": "Test", "kind": "rss", "url": "https://example.org/"})
            now = int(time.time())
            db = radar.connect(root / "data/radar.db")
            try:
                with db:
                    db.execute("INSERT INTO sources(id,initial_since,cursor_at,last_status) VALUES('test',?,?, 'ok')", (now - 100, now))
                    for index in range(2):
                        db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   ("test", str(index), "Tool %d" % index, "https://example.org/%d" % index,
                                    "https://example.org/discuss/%d" % index, now - 10, "具体产品描述", 0, 0, now, now))
                    # The same post found by a second, later-configured search is analyzed once.
                    config["sources"].append({"id": "test_dup", "name": "Dup", "kind": "rss", "url": "https://example.org/"})
                    db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               ("test_dup", "0", "Tool 0", "https://example.org/0", "https://example.org/discuss/0",
                                now - 10, "具体产品描述", 0, 0, now, now))
                self.assertEqual(analysis.analyze_daily(root, config, now, db), "failed")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM screenings").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT cursor_at FROM sources").fetchone()[0], now)
                self.assertIn("模型分析未完成", (root / "latest.md").read_text())
                self.assertTrue(analysis.analysis_is_due(db, config, now - 1))
                api.fail_synthesis = False
                self.assertEqual(analysis.analyze_daily(root, config, now, db), "ok")
                self.assertEqual(api.stages.count("screen"), 1)
                self.assertEqual(api.stages.count("select"), 1)
                self.assertEqual(api.stages.count("deep"), 1)
                self.assertEqual(api.stages.count("product_audit"), 1)
                self.assertEqual(api.stages.count("synthesize"), 4)  # 3 failed attempts, then success
                self.assertEqual(api.stages.count("audit"), 1)
                self.assertFalse(analysis.analysis_is_due(db, config, now - 1))
                self.assertIn("每日产品短报", (root / "latest.md").read_text())
                day = dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone(
                    dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
                day_dir = root / "reports" / day
                self.assertTrue((day_dir / "daily.md").exists())
                self.assertTrue((day_dir / "products.md").exists())
                self.assertTrue((day_dir / "catalog.md").exists())
                self.assertTrue((day_dir / "analysis.json").exists())
                self.assertLessEqual(len((day_dir / "daily.md").read_text()), config["report"]["daily_max_chars"])
                self.assertIn("本期重点产品", (day_dir / "products.md").read_text())
                report = json.loads(db.execute("SELECT report_json FROM analysis_reports").fetchone()[0])
                self.assertEqual(report["usage"]["successful"], 2)
                self.assertEqual(report["usage"]["cached"], 3)
                self.assertNotIn("fixture-key", (root / "latest.md").read_text())
                self.assertEqual(db.execute("SELECT cursor_at FROM sources").fetchone()[0], now)
                config["llm"]["focus"] = "新的研究重点"
                self.assertTrue(analysis.analysis_is_due(db, config, now - 1))
            finally:
                db.close()

    def test_rubric_score_and_product_domain(self):
        top = {k: 5 for k in analysis.SCORE_KEYS}
        low = {k: 1 for k in analysis.SCORE_KEYS}
        self.assertEqual(analysis.product_score(top, analysis.DEFAULT_WEIGHTS), 100)
        self.assertEqual(analysis.product_score(low, analysis.DEFAULT_WEIGHTS), 0)
        mixed = dict(low, novelty=5)
        self.assertEqual(analysis.product_score(mixed, {"novelty": 1}), 100)
        self.assertEqual(analysis.product_domain("https://www.Acme.io/pricing"), "acme.io")
        self.assertEqual(analysis.product_domain("https://github.com/Acme/Tool/tree/main"), "github.com/acme/tool")
        self.assertEqual(analysis.product_domain("https://huggingface.co/spaces/a/b"), "huggingface.co/spaces/a/b")
        self.assertIsNone(analysis.product_domain("https://news.ycombinator.com/item?id=1"))
        self.assertIsNone(analysis.product_domain("https://www.producthunt.com/products/x"))

    def test_pool_merges_cross_source_products_and_keeps_category_quota(self):
        def row(identity, source, url, points=0):
            return {"source_id": source, "url": url, "points": points, "comments": 0,
                    "discussion_url": "https://%s.example/%s" % (source, identity)}
        cards, rows = {}, {}
        for i in range(30):
            cards["d%d" % i] = dict(card("d%d" % i), category="开发工具", product_score=60 + i)
            rows["d%d" % i] = row("d%d" % i, "show_hn", "https://dev%d.example/" % i, i)
        cards["x1"] = dict(card("x1"), category="生活消费", product_score=20)
        rows["x1"] = row("x1", "v2ex_create", "https://life.example/")
        # Same product from two sources: merged, with a cross-source bonus.
        cards["m1"] = dict(card("m1"), category="营销销售", product_score=50)
        cards["m2"] = dict(card("m2"), category="营销销售", product_score=40)
        rows["m1"] = row("m1", "product_hunt", "https://www.merged.example/")
        rows["m2"] = row("m2", "reddit_saas", "https://merged.example/launch")
        cards["n1"] = dict(card("n1"), is_product=False, product_score=99)
        rows["n1"] = row("n1", "techcrunch_startups", "https://news.example/")
        pool = analysis.build_pool(cards, rows, {"select_pool_size": 10, "min_per_category": 2})
        ids = [c["id"] for c in pool]
        self.assertEqual(len(pool), 10)
        self.assertIn("x1", ids)
        self.assertNotIn("n1", ids)
        self.assertEqual(len({"m1", "m2"} & set(ids)), 1)
        merged = next(c for c in pool if c["id"] in ("m1", "m2"))
        self.assertEqual(merged["signals"]["seen_in_sources"], ["product_hunt", "reddit_saas"])
        self.assertEqual(merged["rank_score"], merged["product_score"] + 8)
        # Two searches on the same site are one platform: no cross-source bonus.
        rows["m2"]["discussion_url"] = rows["m1"]["discussion_url"] + "?again"
        same = analysis.build_pool(cards, rows, {"select_pool_size": 10, "min_per_category": 2})
        self.assertEqual(next(c for c in same if c["id"] in ("m1", "m2"))["rank_score"], merged["product_score"])
        self.assertEqual(ids[0], "d29")
        # 30 strong developer tools from one source are capped once other candidates exist.
        capped = analysis.build_pool(cards, rows, {"select_pool_size": 10, "min_per_category": 1,
                                                   "max_source_share": 0.5, "max_category_share": 0.5})
        self.assertEqual(sum(c["category"] == "开发工具" for c in capped), 8)
        self.assertEqual(len(capped), 10)
        self.assertEqual(pool, sorted(pool, key=lambda c: -c["rank_score"]))

    def test_selection_caps_limit_one_source_but_relax_when_pool_is_uniform(self):
        def pooled(identity, source, category):
            return dict(card(identity), category=category, signals={"source": source})
        pool = [pooled("a%d" % i, "show_hn", "开发工具") for i in range(8)] + [pooled("b%d" % i, "v2ex", "生活消费") for i in range(8)]
        caps = analysis.selection_caps(pool, 4, 0.5)
        self.assertEqual(caps[("source", "show_hn")], 2)
        pick = lambda ids: {"shortlist": [{"id": i, "why": "w", "question": "q", "previous_product": "",
                                           "is_material_update": False} for i in ids]}
        validate = analysis.selection_validator([c["id"] for c in pool], 4, pool, caps)
        validate(pick(["a0", "a1", "b0", "b1"]))
        with self.assertRaises(ValueError):
            validate(pick(["a0", "a1", "a2", "b0"]))
        over = pick(["a0", "a1", "a2", "b0"])["shortlist"]
        kept = analysis.within_caps(over, pool, caps)
        self.assertEqual([r["id"] for r in kept], ["a0", "a1", "b0"])
        refilled = analysis.refill(kept, 4, pool, caps, {analysis.product_key("测试产品x")})
        self.assertEqual([r["id"] for r in refilled], ["a0", "a1", "b0", "b1"])
        uniform = pool[:8]
        self.assertEqual(analysis.selection_caps(uniform, 4, 0.5)[("source", "show_hn")], 4)

    def test_failing_screen_batch_is_split_so_good_items_survive(self):
        calls = []

        class Client:
            def call(self, stage, payload, validator):
                ids = [x["id"] for x in payload["items"]]
                calls.append(ids)
                if "bad" in ids:
                    raise RuntimeError("模型输出校验失败")
                data = {"items": [card(i) for i in ids]}
                validator(data)
                return data

        errors = []
        batch = [{"id": i} for i in ("a", "b", "bad", "c")]
        cards = analysis.screen_split(Client(), "focus", batch, errors)
        self.assertEqual([c["id"] for c in cards], ["a", "b", "c"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(calls[0], ["a", "b", "bad", "c"])

    def test_page_meta_reads_description_tags(self):
        page = ('<html><head><title>Acme &amp; Co</title>'
                '<meta property="og:description" content="It\'s for invoices">'
                "<meta content='Ledger \"sync\" for shops' name='description'></head></html>")
        self.assertEqual(analysis.page_meta(page), {"title": "Acme & Co", "description": 'Ledger "sync" for shops'})
        self.assertEqual(analysis.page_meta("<p>none</p>"), {"title": "", "description": ""})

    def test_feedback_is_recorded_and_counted_per_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = radar.connect(Path(temporary) / "radar.db")
            try:
                analysis.init_db(db)
                now = int(time.time())
                good, other = dict(card("p_good"), name="Acme Ledger"), dict(card("p_other"), name="Acme Notes")
                with db:
                    for c, source in ((good, "show_hn"), (other, "v2ex_create")):
                        db.execute("INSERT INTO screenings VALUES(?,?,?,?,?,?,NULL)",
                                   (c["id"], source, c["id"], "h", analysis.packed(c), now))
                    report = {"day": "2026-10-11", "screened": [good, other], "pool": ["p_good", "p_other"],
                              "shortlist": [{"id": "p_good"}], "deep_dives": [dict(good)]}
                    db.execute("INSERT INTO analysis_reports VALUES(?,?,?,?,?)",
                               ("2026-10-11", now, "x", "ok", analysis.packed(report)))
                self.assertEqual(len(analysis.record_feedback(db, "acme", "good")), 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)
                self.assertEqual(len(analysis.record_feedback(db, "Acme Ledger", "good", "工作流具体")), 1)
                self.assertEqual(len(analysis.record_feedback(db, "p_other", "bad")), 1)
                examples = analysis.reader_feedback(db, 10)
                self.assertEqual({e["verdict"] for e in examples}, {"good", "bad"})
                self.assertIn("工作流具体", [e["note"] for e in examples])
                config = radar.read_config(radar.ROOT / "config.json")
                stats = {r["source"]: r for r in analysis.source_stats(db, config, now - 60)}
                self.assertEqual((stats["show_hn"]["pool"], stats["show_hn"]["shortlist"], stats["show_hn"]["featured"]), (1, 1, 1))
                self.assertEqual((stats["show_hn"]["good"], stats["v2ex_create"]["bad"]), (1, 1))
            finally:
                db.close()

    def test_heat_lanes_and_featured_order_follow_reader_priorities(self):
        def row(url, points=0, comments=0, rank=0):
            return {"discussion_url": url, "source_id": "s", "points": points, "comments": comments, "rank": rank}
        self.assertEqual(analysis.heat(row("https://news.ycombinator.com/item?id=1", 300, 10), {}), 1)
        self.assertEqual(analysis.heat(row("https://www.reddit.com/r/x/comments/1/", rank=1), {}), 1)
        self.assertEqual(analysis.heat(row("https://www.reddit.com/r/x/comments/1/", rank=26), {}), 0)
        self.assertEqual(analysis.heat(row("https://www.v2ex.com/t/1", comments=20), {}), 0.5)
        scores = card("x")["scores"]
        self.assertEqual(analysis.lane({"scores": dict(scores, traction=4)}, 0), "validated")
        self.assertEqual(analysis.lane({"scores": dict(scores, traction=3)}, 0.9), "hot")
        self.assertEqual(analysis.lane({"scores": dict(scores, traction=1)}, 0.5), "hot")
        self.assertEqual(analysis.lane({"scores": dict(scores, traction=1, novelty=4)}, 0), "interesting")
        self.assertEqual(analysis.lane({"scores": dict(scores, traction=1)}, 0), "other")
        products = [{"id": i} for i in ("o", "i", "h", "v1", "v2", "v3")]
        lanes = {"o": "other", "i": "interesting", "h": "hot", "v1": "validated", "v2": "validated", "v3": "validated"}
        sources = {"o": "a", "i": "b", "h": "c", "v1": "r", "v2": "r", "v3": "r"}
        order = analysis.featured_order(products, lanes, {"v1": 9, "v2": 8, "v3": 7}, sources, 4, 2)
        self.assertEqual([p["id"] for p in order], ["v1", "v2", "h", "i", "v3", "o"])
        pool = [{"id": "hot", "name": "Hot", "lane": "hot", "signals": {"heat": 0.9}},
                {"id": "val", "name": "Val", "lane": "validated", "signals": {"heat": 0}},
                {"id": "warm", "name": "Warm", "lane": "hot", "signals": {"heat": 0.6}},
                {"id": "fun", "name": "Fun", "lane": "interesting", "signals": {"heat": 0}},
                {"id": "meh", "name": "Meh", "lane": "other", "signals": {"heat": 0}}]
        picks = [{"id": "fun"}, {"id": "meh"}, {"id": "warm"}, {"id": "val"}]
        result = analysis.ensure_priority(picks, 4, pool, set(), share=0.75)
        self.assertEqual([r["id"] for r in result], ["fun", "warm", "val", "hot"])
        self.assertEqual(analysis.ensure_priority(picks, 4, pool, {"hot"}, share=0.75), picks)
        self.assertEqual(analysis.signal_text(row("https://github.com/a/b", 886, rank=2), "GitHub Trending"),
                         "GitHub Trending 榜单第 2 位，今日新增 886 stars")

    def test_stream_is_assembled_without_reasoning(self):
        lines = [b": keep-alive\n", b"\n",
                 b'data: {"id":"r1","model":"m","choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
                 b'data: {"id":"r1","model":"m","choices":[{"delta":{"content":"{\\"a\\":"}}]}\n',
                 b'data: {"id":"r1","model":"m","choices":[{"delta":{"content":"1}"},"finish_reason":"stop"}]}\n',
                 b'data: {"id":"r1","model":"m","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n',
                 b"data: [DONE]\n"]
        response = analysis.read_stream(iter(lines))
        self.assertEqual(json.loads(response["choices"][0]["message"]["content"]), {"a": 1})
        self.assertEqual((response["choices"][0]["finish_reason"], response["usage"]["completion_tokens"]), ("stop", 2))

    def test_comments_are_ranked_and_author_replies_marked(self):
        hn = {"author": "maker", "children": [
            {"author": "a", "text": "<p>Looks like Handy</p>", "children": []},
            {"author": "maker", "text": "Thanks, pricing is $5", "children": []},
            {"author": "b", "text": "Why not use the OS dictation?", "children": [{"children": []}, {"children": []}]}]}
        atom = ("<feed xmlns='http://www.w3.org/2005/Atom'>"
                "<entry><author><name>/u/op</name></author><content>post body</content></entry>"
                "<entry><author><name>/u/x</name></author><content>&lt;p&gt;First top comment&lt;/p&gt;</content></entry>"
                "<entry><author><name>/u/op</name></author><content>reply from op</content></entry></feed>")
        original = analysis.fetch_raw
        analysis.fetch_raw = lambda url, cfg, timeout: (json.dumps(hn).encode() if "algolia" in url else atom.encode())
        try:
            rows = analysis.parse_comments("https://news.ycombinator.com/item?id=1", {})
            self.assertEqual(rows[0]["text"], "Why not use the OS dictation?")
            self.assertEqual([r["by_author"] for r in rows], [False, False, True])
            rows = analysis.parse_comments("https://www.reddit.com/r/x/comments/abc/title/", {})
            self.assertEqual([(r["text"], r["by_author"]) for r in rows],
                             [("First top comment", False), ("reply from op", True)])
            self.assertIsNone(analysis.parse_comments("https://example.org/post", {}))
        finally:
            analysis.fetch_raw = original
        evidence = analysis.comment_evidence("p_1", "https://x", rows, 1000)
        self.assertEqual((evidence["id"], evidence["text"].count("[作者回复]")), ("p_1:comments", 1))

    def test_deep_validator_ties_community_feedback_to_comment_evidence(self):
        def product(feedback):
            record = {k: "内容" for k in ("id", "name", "one_liner", "positioning", "workflow", "value_proposition",
                                          "alternatives", "differentiation", "pricing_fact", "business_stage", "watch_reason")}
            record.update(id="p_1", core_features=[{"text": "功能", "evidence_ids": ["p_1:source"]}],
                          customer_segments=[{"segment": "s", "role": "r", "scenario": "c", "need": "n", "basis": "b",
                                              "evidence_ids": ["p_1:source"]}],
                          derived_thoughts=[{"title": "t", "analysis": "a", "takeaway": "k", "applies_when": "w",
                                             "evidence_ids": ["p_1:source"]}],
                          community_feedback=feedback, risks=[], unknowns=[],
                          reception={"signals": "120 分", "why": "推断", "evidence_ids": []})
            return {"products": [record]}
        quote = [{"type": "质疑", "text": "有评论者质疑", "evidence_ids": ["p_1:comments"]}]
        analysis.deep_validator({"p_1": {"p_1:source", "p_1:comments"}})(product(quote))
        analysis.deep_validator({"p_1": {"p_1:source"}})(product([]))
        with self.assertRaises(ValueError):
            analysis.deep_validator({"p_1": {"p_1:source"}})(product(quote))
        with self.assertRaises(ValueError):
            analysis.deep_validator({"p_1": {"p_1:source", "p_1:comments"}})(
                product([dict(quote[0], evidence_ids=["p_1:source"])]))
        with self.assertRaises(ValueError):
            analysis.deep_validator({"p_1": {"p_1:source", "p_1:comments"}})(product([dict(quote[0], type="热门")]))
        with self.assertRaises(ValueError):
            analysis.screen_validator(["p_1"])({"items": [dict(card("p_1"), themes="AI Agent")]})
        self.assertEqual(analysis.normalize_themes(["ai agent", "不存在的主题", "免费", "免费"]), ["AI Agent", "免费"])

    def test_theme_trends_count_window_and_recent_days(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = radar.connect(Path(temporary) / "radar.db")
            try:
                analysis.init_db(db)
                config = radar.read_config(radar.ROOT / "config.json")
                cutoff = radar.parse_time("2026-10-12T02:00:00Z")
                rows = [("a", 0, ["AI Agent"]), ("b", 0, ["AI Agent", "免费"]), ("c", 6, ["免费"]),
                        ("d", 10, ["免费"]), ("e", 30, ["AI Agent"])]
                with db:
                    for name, age_days, themes in rows:
                        seen = cutoff - age_days * 86400
                        db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   ("show_hn", name, name, "https://%s.example/" % name, "https://%s.example/" % name,
                                    seen, "", 0, 0, seen, seen))
                        c = dict(card(name), name=name, themes=themes, product_score=50)
                        db.execute("INSERT INTO screenings VALUES(?,?,?,?,?,?,NULL)",
                                   (name, "show_hn", name, "h", analysis.packed(c), seen))
                trends = analysis.theme_trends(db, config, cutoff, 14, 3)
                tags = {t["tag"]: t for t in trends["tags"]}
                self.assertEqual((trends["products"], trends["recent_products"], trends["days_with_data"]), (4, 2, 3))
                self.assertEqual((tags["AI Agent"]["count"], tags["AI Agent"]["recent"]), (2, 2))
                self.assertEqual((tags["免费"]["count"], tags["免费"]["recent"], tags["免费"]["earlier_share"]), (3, 1, 1.0))
                lines = "\n".join(analysis.trend_lines(trends, 8))
                self.assertIn("AI Agent 2（50%） ↑", lines)
                self.assertIn("免费 3（75%） ↓", lines)
            finally:
                db.close()

    def test_public_page_fetch_rejects_local_resources(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/", "https://user:pass@example.org/", "http://example.org:8000/"):
            with self.assertRaises(ValueError, msg=url):
                analysis.public_url(url)
        for url in ("http://127.0.0.1/", "http://localhost/", "http://printer.local/", "http://[::1]/"):
            with self.assertRaises(ValueError, msg=url):
                analysis.public_url(url, proxied=True)
        # Behind a proxy the proxy resolves names, so a poisoned local DNS answer must not block the page.
        self.assertEqual(analysis.public_url("https://example.org/", proxied=True), "https://example.org/")

    def test_recent_featured_extracts_products_for_cross_day_suppression(self):
        reports = [{"day": "2026-10-10", "deep_dives": [{"name": "Same Tool", "one_liner": "old",
                                                            "positioning": "developer tool"}]}]
        self.assertEqual(analysis.recent_featured(reports)[0]["name"], "Same Tool")
        self.assertEqual(analysis.product_key("Same-Tool (Beta)"), "sametoolbeta")

    def test_empty_day_does_not_invent_a_model_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("TEST_RADAR_LLM_KEY=fixture-key\n")
            config = radar.read_config(radar.ROOT / "config.json")
            config["llm"]["api_key_env"] = "TEST_RADAR_LLM_KEY"
            db = radar.connect(root / "data/radar.db")
            try:
                self.assertEqual(analysis.analyze_daily(root, config, int(time.time()), db), "ok")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], 0)
                self.assertIn("没有待分析的新内容", (root / "latest.md").read_text())
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
