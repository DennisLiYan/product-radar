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
            "category": "工作效率", "product_stage": "已发布", "product_score": 70,
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
                                      derived_thoughts=[{"title": "检索扩展", "analysis": "可扩展历史检索，属于推断",
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
            now = int(time.time())
            db = radar.connect(root / "data/radar.db")
            try:
                with db:
                    db.execute("INSERT INTO sources(id,initial_since,cursor_at,last_status) VALUES('test',?,?, 'ok')", (now - 100, now))
                    for index in range(2):
                        db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   ("test", str(index), "Tool %d" % index, "https://example.org/%d" % index,
                                    "https://example.org/discuss/%d" % index, now - 10, "具体产品描述", 0, 0, now, now))
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
                self.assertEqual(api.stages.count("synthesize"), 2)
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

    def test_public_page_fetch_rejects_local_resources(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/", "https://user:pass@example.org/", "http://example.org:8000/"):
            with self.assertRaises(ValueError, msg=url):
                analysis.public_url(url)

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
