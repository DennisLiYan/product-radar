"""Evidence-grounded model analysis, independent of collection checkpoints."""
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import ipaddress
import http.client
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from radar import ROOT, atomic_write, link, markdown, plain, stamp

LOG = logging.getLogger("radar")
PROMPTS = ROOT / "prompts"
CATEGORIES = {"开发工具", "工作效率", "营销销售", "内容创作", "生活消费", "教育学习", "垂直行业", "基础设施", "其他"}


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def item_id(row):
    return "p_" + digest([row["source_id"], row["external_id"]])[:16]


@contextlib.contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def init_db(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS screenings (
        item_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, external_id TEXT NOT NULL,
        input_hash TEXT NOT NULL, data_json TEXT NOT NULL, updated_at INTEGER NOT NULL,
        reported_at INTEGER);
    CREATE TABLE IF NOT EXISTS llm_cache (
        cache_key TEXT PRIMARY KEY, stage TEXT NOT NULL, model TEXT NOT NULL,
        data_json TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL, model TEXT NOT NULL,
        status TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0, usage_known INTEGER NOT NULL DEFAULT 0,
        elapsed_seconds REAL NOT NULL, response_id TEXT, error TEXT, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS analysis_runs (
        id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL, status TEXT NOT NULL,
        profile_hash TEXT NOT NULL, report_json TEXT, error TEXT, finished_at INTEGER);
    CREATE TABLE IF NOT EXISTS analysis_reports (
        day TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL, profile_hash TEXT NOT NULL,
        status TEXT NOT NULL, report_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS page_evidence (
        url TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL, data_json TEXT NOT NULL);
    """)


def profile_hash(config):
    # Keep the established analysis profile stable when a final quality gate is
    # added or tuned. Each model call still includes its full prompt in the cache
    # key, so audit changes invalidate audit output without re-screening all items.
    names = ("common.txt", "screen.txt", "select.txt", "deep.txt", "synthesize.txt")
    prompt_text = {name: (PROMPTS / name).read_text(encoding="utf-8") for name in names}
    return digest({"llm": config["llm"], "prompts": prompt_text})


def screen_profile_hash(config):
    """Only changes that affect item cards should invalidate 1-by-1 screening."""
    cfg = config["llm"]
    model_fields = {key: cfg[key] for key in ("provider", "base_url", "model", "focus")}
    prompts = {name: (PROMPTS / name).read_text(encoding="utf-8") for name in ("common.txt", "screen.txt")}
    return digest({"model": model_fields, "prompts": prompts})


def analysis_is_due(db, config, due_at):
    init_db(db)
    row = db.execute("SELECT 1 FROM analysis_reports WHERE cutoff_at>=? AND profile_hash=? AND status='ok'",
                     (due_at, profile_hash(config))).fetchone()
    return row is None


def read_secret(root, config):
    name = config["api_key_env"]
    if os.environ.get(name):
        return os.environ[name]
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip("\"'")
    raise RuntimeError("缺少 %s；请配置本项目 .env。采集数据保留，模型分析未执行。" % name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("模型 API 不允许重定向，避免凭据发往其他地址")


class LLMClient:
    def __init__(self, root, config, run_id):
        self.root, self.config, self.run_id = root, config, run_id
        self.db_path = root / "data/radar.db"
        self.secret = read_secret(root, config)
        if not self.secret:
            raise RuntimeError("模型 API 密钥为空")
        self.endpoint = config["base_url"].rstrip("/") + "/chat/completions"
        parsed = urllib.parse.urlsplit(self.endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型 API 地址不可包含凭据、参数或片段")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
            raise ValueError("远程模型 API 必须使用 HTTPS")
        self.opener = urllib.request.build_opener(NoRedirect())
        self.lock = threading.Lock()
        self.attempts = 0

    def record(self, stage, status, elapsed, usage=None, response_id=None, error=None, model=None):
        usage = usage or {}
        known = int("prompt_tokens" in usage and "completion_tokens" in usage)
        with database(self.db_path) as db, db:
            db.execute("""INSERT INTO llm_calls(run_id,stage,model,status,prompt_tokens,completion_tokens,
                       usage_known,elapsed_seconds,response_id,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (self.run_id, stage, model or self.config["model"], status,
                        int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), known,
                        elapsed, response_id, (error or "").replace(self.secret, "[REDACTED]")[:500], int(time.time())))

    def call(self, stage, payload, validator):
        prompt = (PROMPTS / "common.txt").read_text() + "\n" + (PROMPTS / (stage + ".txt")).read_text()
        deep = stage in ("deep", "product_audit", "synthesize", "audit")
        body = {"model": self.config["model"], "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": packed(payload)}],
            "response_format": {"type": "json_object"},
            "max_tokens": self.config["deep_max_tokens"] if deep else self.config["screen_max_tokens"]}
        if self.config["provider"] == "deepseek":
            body["thinking"] = {"type": "enabled" if deep else "disabled"}
            if deep:
                body["reasoning_effort"] = "high"
        cache_key = digest({"endpoint": self.endpoint, "request": body})
        with database(self.db_path) as db:
            cached = db.execute("SELECT data_json FROM llm_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if cached:
            data = json.loads(cached[0])
            validator(data)
            self.record(stage, "cached", 0)
            return data
        last_error = ""
        attempt_limit = max(self.config["max_attempts"], 3 if stage == "audit" else 1)
        for attempt in range(attempt_limit):
            with self.lock:
                if self.attempts >= self.config["max_calls_per_run"]:
                    raise RuntimeError("达到本次模型请求上限，已完成分析保留，下次可继续")
                self.attempts += 1
            start = time.monotonic()
            response, retryable = {}, True
            try:
                request = urllib.request.Request(self.endpoint, data=packed(body).encode(), method="POST", headers={
                    "Content-Type": "application/json", "Authorization": "Bearer " + self.secret})
                with self.opener.open(request, timeout=self.config["timeout_seconds"]) as handle:
                    response = json.loads(handle.read(16 * 1024 * 1024))
                choice = response["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ValueError("模型未完整完成输出（%s）" % choice.get("finish_reason"))
                # Persist only final structured output and usage, never reasoning_content.
                data = json.loads(choice["message"]["content"])
                validator(data)
                self.record(stage, "ok", time.monotonic() - start, response.get("usage"), response.get("id"),
                            model=response.get("model"))
                with database(self.db_path) as db, db:
                    db.execute("INSERT OR REPLACE INTO llm_cache VALUES(?,?,?,?,?)",
                               (cache_key, stage, self.config["model"], packed(data), int(time.time())))
                LOG.info("模型 %s 完成（%.1fs）", stage, time.monotonic() - start)
                return data
            except urllib.error.HTTPError as error:
                last_error = "模型 API HTTP %d" % error.code
                retryable = error.code in (408, 429, 500, 502, 503, 504)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead,
                    http.client.RemoteDisconnected):
                last_error = "模型 API 网络请求失败或超时"
            except (ValueError, KeyError, TypeError, IndexError) as error:
                last_error = "模型输出校验失败：" + str(error)[:220]
            self.record(stage, "failed", time.monotonic() - start, response.get("usage"), response.get("id"), last_error)
            if not retryable or attempt + 1 == attempt_limit:
                break
            LOG.warning("%s，重试 %s", last_error, stage)
            time.sleep(2 ** attempt)
        raise RuntimeError(last_error)


def require_text(obj, keys):
    if not isinstance(obj, dict):
        raise ValueError("应返回 JSON 对象")
    for key in keys:
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            raise ValueError("缺少文本字段 " + key)


def require_list(obj, key):
    if not isinstance(obj, dict):
        raise ValueError("应返回 JSON 对象")
    if not isinstance(obj.get(key), list):
        raise ValueError("缺少数组字段 " + key)
    return obj[key]


def validate_ids(records, expected):
    ids = [r.get("id") for r in records if isinstance(r, dict)]
    if len(ids) != len(records) or len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise ValueError("输出 ID 必须与输入逐项一致，不可缺失、重复或增加")


def validate_refs(obj, allowed, nonempty=True):
    ids = require_list(obj, "evidence_ids")
    if (nonempty and not ids) or any(not isinstance(x, str) or x not in allowed for x in ids):
        raise ValueError("引用了不存在的证据 ID 或缺少证据")


def screen_validator(expected):
    def validate(data):
        rows = require_list(data, "items")
        validate_ids(rows, expected)
        for row in rows:
            require_text(row, ["id", "name", "one_liner", "core_function", "positioning", "target_customers",
                               "use_case", "category", "product_stage", "why_interesting", "confidence"])
            if type(row.get("product_score")) is not int or not 0 <= row["product_score"] <= 100:
                raise ValueError("产品关注度须为 0—100 整数")
            if row["category"] not in CATEGORIES or row["confidence"] not in ("高", "中", "低"):
                raise ValueError("分类或置信度不在规定范围")
            if not isinstance(row.get("is_product"), bool):
                raise ValueError("is_product 必须为布尔值")
    return validate


def selection_validator(allowed, count):
    def validate(data):
        rows = require_list(data, "shortlist")
        if any(not isinstance(r, dict) for r in rows):
            raise ValueError("深读条目必须为对象")
        ids = [r.get("id") for r in rows]
        if len(rows) > count or len(ids) != len(set(ids)) or not set(ids) <= set(allowed):
            raise ValueError("深读名单重复、超额或引用未知产品")
        for row in rows:
            require_text(row, ["id", "why", "question"])
            if not isinstance(row.get("previous_product"), str):
                raise ValueError("缺少历史产品匹配说明")
            if not isinstance(row.get("is_material_update"), bool):
                raise ValueError("is_material_update 必须为布尔值")
    return validate


def product_key(name):
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", name.casefold())


def recent_featured(reports):
    rows = []
    for report in reports:
        for product in report.get("deep_dives", []):
            rows.append({"day": report.get("day", ""), "name": product.get("name", ""),
                         "one_liner": product.get("one_liner", ""),
                         "positioning": product.get("positioning", "")})
    return rows


def deep_validator(evidence_by_product):
    def validate(data):
        products = require_list(data, "products")
        validate_ids(products, evidence_by_product)
        for p in products:
            require_text(p, ["id", "name", "one_liner", "positioning", "workflow", "value_proposition",
                             "alternatives", "differentiation", "pricing_fact", "business_stage", "watch_reason"])
            features = require_list(p, "core_features")
            if not features:
                raise ValueError("重点产品缺少核心功能")
            for feature in features:
                require_text(feature, ["text"])
                validate_refs(feature, evidence_by_product[p["id"]])
            customers = require_list(p, "customer_segments")
            if not customers:
                raise ValueError("重点产品缺少客户分析")
            for customer in customers:
                require_text(customer, ["segment", "role", "scenario", "need", "basis"])
                validate_refs(customer, evidence_by_product[p["id"]])
            thoughts = require_list(p, "derived_thoughts")
            if not thoughts:
                raise ValueError("重点产品缺少衍生思考")
            for thought in thoughts:
                require_text(thought, ["title", "analysis"])
                validate_refs(thought, evidence_by_product[p["id"]])
            for key in ("risks", "unknowns"):
                if any(not isinstance(x, str) for x in require_list(p, key)):
                    raise ValueError("风险与未知项应为文字")
    return validate


def synthesis_validator(allowed):
    def validate(data):
        require_text(data, ["headline"])
        schemas = {"batch_summary": ["text"],
                   "customer_patterns": ["name", "customers", "shared_need", "products_comparison", "implication"],
                   "product_patterns": ["name", "observation", "examples", "what_is_different"],
                   "derived_thoughts": ["title", "starting_point", "target_customer", "product_direction", "reasoning", "caveat"],
                   "watch_signals": ["text"]}
        for key, fields in schemas.items():
            rows = require_list(data, key)
            if key == "batch_summary" and not rows:
                raise ValueError("缺少本批产品概览")
            for row in rows:
                require_text(row, fields)
                validate_refs(row, allowed)
        if any(not isinstance(x, str) for x in require_list(data, "limitations")):
            raise ValueError("资料局限必须为文字数组")
    return validate


def audit_validator(allowed):
    validate_synthesis = synthesis_validator(allowed)

    def validate(data):
        if not isinstance(data, dict):
            raise ValueError("复核结果应为 JSON 对象")
        validate_synthesis(data.get("synthesis"))
        notes = require_list(data, "quality_notes")
        if not notes or any(not isinstance(x, str) or not x.strip() for x in notes):
            raise ValueError("复核结果缺少质量说明")
    return validate


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("不是公开 HTTP(S) 地址")
    if parsed.port not in (None, 80, 443):
        raise ValueError("不抓取非标准端口")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("不抓取内网或本机地址")
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def enrich(row, root, cfg):
    identity = item_id(row)
    base = {"id": identity + ":source", "url": row["discussion_url"],
            "text": row["title"] + "\n" + row["description"], "kind": "发布来源/发布者自述"}
    result = {"id": identity, "evidence": [base], "page_status": "未抓取"}
    if not cfg["fetch_product_pages"]:
        return result
    db_path = root / "data/radar.db"
    with database(db_path) as db:
        cached = db.execute("SELECT * FROM page_evidence WHERE url=?", (row["url"],)).fetchone()
    if cached and cached["fetched_at"] > int(time.time()) - 86400:
        page = json.loads(cached["data_json"])
    else:
        try:
            public_url(row["url"])
            request = urllib.request.Request(row["url"], headers={"User-Agent": "ProductRadar/2.0 (personal research reader)"})
            with urllib.request.build_opener(PublicRedirect()).open(request, timeout=cfg["page_timeout_seconds"]) as handle:
                content_type = handle.headers.get("Content-Type", "")
                if not any(t in content_type for t in ("text/html", "text/plain", "application/xhtml")):
                    raise ValueError("非文本网页")
                body = handle.read(2 * 1024 * 1024)
                charset = handle.headers.get_content_charset() or "utf-8"
                text = plain(body.decode(charset, errors="replace"))[:cfg["evidence_max_chars"]]
                if len(text) < 100:
                    raise ValueError("网页有效正文不足")
                page = {"status": "ok", "url": handle.geturl(), "text": text}
        except Exception as error:
            # Do not pass network stack details or response bodies to model prompts/logs.
            code = error.code if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            page = {"status": "unavailable", "url": row["url"], "reason": str(code)}
        with database(db_path) as db, db:
            db.execute("INSERT OR REPLACE INTO page_evidence VALUES(?,?,?)", (row["url"], int(time.time()), packed(page)))
    result["page_status"] = page["status"]
    if page["status"] == "ok":
        result["evidence"].append({"id": identity + ":page", "url": page["url"],
                                    "text": page["text"], "kind": "公开网页内容，非独立核验"})
    else:
        result["page_note"] = "产品页无法读取；仅据发布来源分析，不能推测已核实官网信息。"
    return result


def usage_for(db, run_id):
    row = db.execute("""SELECT COUNT(*) total, SUM(status='cached') cached, SUM(status='ok') successful,
                     SUM(status='failed') failed, COALESCE(SUM(prompt_tokens),0) input_tokens,
                     COALESCE(SUM(completion_tokens),0) output_tokens,
                     SUM(status!='cached' AND usage_known=0) unknown_usage FROM llm_calls WHERE run_id=?""", (run_id,)).fetchone()
    return dict(row)


def analyze_daily(root, config, cutoff, db):
    init_db(db)
    cfg = config["llm"]
    version = profile_hash(config)
    screening_version = screen_profile_hash(config)
    day = dt.datetime.fromtimestamp(cutoff, ZoneInfo(config["timezone"])).strftime("%Y-%m-%d")
    midnight = int(dt.datetime.fromtimestamp(cutoff, ZoneInfo(config["timezone"])).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    run_id = "analysis-" + uuid.uuid4().hex[:16]
    with db:
        db.execute("UPDATE analysis_runs SET status='interrupted' WHERE status='running'")
        db.execute("INSERT INTO analysis_runs(id,cutoff_at,status,profile_hash) VALUES(?,?,'running',?)", (run_id, cutoff, version))
    try:
        client = LLMClient(root, cfg, run_id)
        prior = {r["item_id"]: dict(r) for r in db.execute("SELECT * FROM screenings")}
        all_rows = [dict(r) for r in db.execute("SELECT * FROM items WHERE first_seen_at<=? ORDER BY published_at,source_id,external_id", (cutoff,))]
        candidates = [r for r in all_rows if r["first_seen_at"] >= midnight or
                      not prior.get(item_id(r), {}).get("reported_at")]
        records, fingerprints, pending = {}, {}, []
        for row in candidates:
            identity = item_id(row)
            source = {"id": identity, "source": row["source_id"], "title": row["title"],
                      "url": row["url"], "description": row["description"][:2400]}
            fingerprints[identity] = digest({"profile": screening_version, "source": source})
            old = prior.get(identity)
            if old and old["input_hash"] == fingerprints[identity]:
                records[identity] = json.loads(old["data_json"])
            else:
                pending.append(source)
        work = pending[:cfg["max_items_per_run"]]
        remaining = len(pending) - len(work)
        batches = [work[i:i + cfg["batch_size"]] for i in range(0, len(work), cfg["batch_size"])]
        LOG.info("开始模型全量归纳：待处理 %d，已缓存 %d，批次 %d", len(work), len(records), len(batches))
        by_id = {item_id(r): r for r in candidates}
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
            futures = {pool.submit(client.call, "screen", {"focus": cfg["focus"], "items": batch},
                                   screen_validator([r["id"] for r in batch])): batch for batch in batches}
            for future in concurrent.futures.as_completed(futures):
                try:
                    output = future.result()["items"]
                    with db:
                        for card in output:
                            identity = card["id"]
                            row = by_id[identity]
                            if not row["description"]:
                                card["confidence"] = "低"
                            records[identity] = card
                            db.execute("""INSERT INTO screenings VALUES(?,?,?,?,?,?,NULL)
                                       ON CONFLICT(item_id) DO UPDATE SET input_hash=excluded.input_hash,
                                       data_json=excluded.data_json,updated_at=excluded.updated_at""",
                                       (identity, row["source_id"], row["external_id"], fingerprints[identity], packed(card), cutoff))
                    LOG.info("已归纳 %d / %d 条", len(records), len(candidates))
                except Exception as error:
                    errors.append(str(error))
        if errors:
            raise RuntimeError("%d 批产品归纳未完成；已成功批次缓存保留。%s" % (len(errors), errors[0]))
        screened = sorted(records.values(), key=lambda r: (-r["product_score"], r["id"]))
        if not screened:
            # No fabricated observations on an empty day.
            report = {"headline": "本次没有待分析的新内容", "batch_summary": [], "customer_patterns": [],
                      "product_patterns": [], "derived_thoughts": [], "watch_signals": [],
                      "limitations": ["没有新增样本，不能据此推断市场没有变化。"]}
            shortlist, deep_dives, enriched, history = [], [], [], []
        else:
            repeat_days = config["report"]["repeat_suppression_days"]
            recent_reports = [json.loads(r[0]) for r in db.execute(
                "SELECT report_json FROM analysis_reports WHERE day<? AND cutoff_at>=? ORDER BY day DESC",
                (day, cutoff - repeat_days * 86400))]
            seen_products = recent_featured(recent_reports)
            shortlist = client.call("select", {"focus": cfg["focus"], "count": cfg["deep_dive_count"],
                                    "items": screened, "recent_featured": seen_products},
                                    selection_validator([r["id"] for r in screened if r["is_product"]], cfg["deep_dive_count"]))["shortlist"]
            recent_keys = {product_key(p["name"]) for p in seen_products if product_key(p["name"])}
            shortlist = [p for p in shortlist if p["is_material_update"] or
                         product_key(records[p["id"]]["name"]) not in recent_keys]
            LOG.info("模型选择 %d 个重点产品，开始补充公开页面证据", len(shortlist))
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
                enriched = list(pool.map(lambda p: enrich(by_id[p["id"]], root, cfg), shortlist))
            for product in enriched:
                product["screening"] = records[product["id"]]
            deep_dives = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
                futures = []
                for i in range(0, len(enriched), 2):
                    group = enriched[i:i + 2]
                    refs = {p["id"]: {e["id"] for e in p["evidence"]} for p in group}
                    futures.append(pool.submit(client.call, "deep", {"focus": cfg["focus"], "products": group}, deep_validator(refs)))
                for future in concurrent.futures.as_completed(futures):
                    deep_dives.extend(future.result()["products"])
            order = {p["id"]: i for i, p in enumerate(shortlist)}
            deep_dives.sort(key=lambda p: order[p["id"]])
            LOG.info("开始逐份复核重点产品档案")
            reviewed_products = []
            evidence_lookup = {p["id"]: p for p in enriched}
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
                futures = []
                for i in range(0, len(deep_dives), 2):
                    group = deep_dives[i:i + 2]
                    refs = {p["id"]: {e["id"] for e in evidence_lookup[p["id"]]["evidence"]} for p in group}
                    materials = [evidence_lookup[p["id"]] for p in group]
                    futures.append(pool.submit(client.call, "product_audit",
                                               {"products": group, "evidence": materials}, deep_validator(refs)))
                for future in concurrent.futures.as_completed(futures):
                    reviewed_products.extend(future.result()["products"])
            deep_dives = sorted(reviewed_products, key=lambda p: order[p["id"]])
            history = [json.loads(r[0]) for r in db.execute(
                "SELECT report_json FROM analysis_reports WHERE day<? AND cutoff_at>=? ORDER BY day DESC LIMIT ?",
                (day, cutoff - cfg["history_days"] * 86400, cfg["history_days"]))]
            historical = []
            for h in history:
                previous = h["synthesis"]
                historical.append({"day": h["day"], "headline": previous["headline"],
                                   "customer_patterns": previous.get("customer_patterns", previous.get("themes", [])),
                                   "product_patterns": previous.get("product_patterns", []),
                                   "sample_size": len(h["screened"])})
            coverage = {"current_sample": len(screened), "pending": remaining,
                        "source_status": [dict(r) for r in db.execute("SELECT id,last_status FROM sources")],
                        "gap_count": db.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0],
                        "historical_days": len(history), "note": "按首次发现时间归入日报，可能包括多天补抓；样本来自已配置平台，非全市场。"}
            LOG.info("开始跨产品综合归纳：%d 条样本，%d 份深读，%d 天历史", len(screened), len(deep_dives), len(history))
            draft = client.call("synthesize", {"focus": cfg["focus"], "screened": screened, "deep_dives": deep_dives,
                                              "history": historical, "coverage": coverage}, synthesis_validator(records))
            LOG.info("开始模型事实与措辞复核")
            reviewed = client.call("audit", {"focus": cfg["focus"], "synthesis": draft, "screened": screened,
                                             "deep_dives": deep_dives, "coverage": coverage}, audit_validator(records))
            report = reviewed["synthesis"]
            quality_notes = reviewed["quality_notes"]
        if not screened:
            quality_notes = ["没有新增样本，未调用模型复核。"]
        result = {"day": day, "cutoff_at": cutoff, "model": cfg["model"], "focus": cfg["focus"],
                  "timezone": config["timezone"], "artifact_root": str(root),
                  "source_names": {s["id"]: s["name"] for s in config["sources"]},
                  "run_id": run_id, "screened": screened, "shortlist": shortlist, "deep_dives": deep_dives,
                  "evidence": enriched, "synthesis": report, "pending": remaining,
                  "quality_notes": quality_notes,
                  "history_days": len(history), "usage": usage_for(db, run_id),
                  "collection_status": [dict(r) for r in db.execute("SELECT id,cursor_at,last_status FROM sources")],
                  "sources": {identity: {"title": r["title"], "url": r["url"], "discussion_url": r["discussion_url"],
                                          "source": r["source_id"], "published_at": r["published_at"]}
                              for identity, r in by_id.items() if identity in records}}
        status = "partial" if remaining else "ok"
        day_dir = root / "reports" / day
        details = render_analysis(result)
        daily = render_daily(result, config)
        atomic_write(day_dir / "daily.md", daily)
        atomic_write(day_dir / "products.md", details)
        atomic_write(day_dir / "analysis.json", packed(result))
        atomic_write(day_dir / "catalog.md", render_catalog(result))
        atomic_write(root / "latest.md", daily)
        with db:
            db.execute("INSERT OR REPLACE INTO analysis_reports VALUES(?,?,?,?,?)", (day, cutoff, version, status, packed(result)))
            db.execute("UPDATE analysis_runs SET status=?,report_json=?,finished_at=? WHERE id=?",
                       (status, packed(result), int(time.time()), run_id))
            db.executemany("UPDATE screenings SET reported_at=? WHERE item_id=?", [(cutoff, p["id"]) for p in screened])
        atomic_write(root / "analysis-status.json", packed({"status": status, "day": day, "pending": remaining, "run_id": run_id}))
        LOG.info("模型日报生成完成：%d 条产品归纳，%d 份产品深读，%d 条衍生思考",
                 len(screened), len(deep_dives), len(report["derived_thoughts"]))
        return status
    except Exception as error:
        message = str(error)[:600]
        with db:
            db.execute("UPDATE analysis_runs SET status='failed',error=?,finished_at=? WHERE id=?", (message, int(time.time()), run_id))
        atomic_write(root / "analysis-status.json", packed({"status": "failed", "day": day, "error": message, "run_id": run_id}))
        # Preserve the last completed daily analysis file; make latest failure visible.
        previous = db.execute("SELECT day FROM analysis_reports ORDER BY day DESC LIMIT 1").fetchone()
        lines = ["# 创业产品日报：模型分析未完成", "", "本次：" + day, "", markdown(message), "",
                 "已采集数据和已完成的分析缓存保留。下次执行会继续处理；不会用原文摘录冒充模型分析。", "",
                 "[本次采集清单](reports/%s/collection.md)" % day]
        if previous:
            lines.extend(["", "[上次完成的研究日报](reports/%s/daily.md)" % previous[0]])
        atomic_write(root / "latest.md", "\n".join(lines) + "\n")
        LOG.error("模型分析失败：%s", message)
        return "failed"


def render_daily(result, config):
    """Reader-facing brief. Full model output remains in the dated folder."""
    s, sources = result["synthesis"], result["sources"]
    report_cfg = config["report"]

    def cut(value, limit):
        value = " ".join(str(value).split())
        if len(value) <= limit:
            return value
        excerpt = value[:limit]
        boundary = max(excerpt.rfind(mark) for mark in "。；！？")
        if boundary >= int(limit * 0.6):
            return excerpt[:boundary + 1]
        return excerpt[:limit - 1].rstrip() + "…"

    def source_link(identity):
        source = sources[identity]
        return link(source["title"], source["discussion_url"])

    def build(product_count, insight_count):
        lines = ["# 每日产品短报 · " + result["day"], "", "**" + markdown(cut(s["headline"], 220)) + "**", "",
                 "本次后台分析 %d 条新增信息；日报只展示 %d 个重点产品。首次出现的同一帖子不会跨日重复，近 %d 天已入选的同名产品默认不再入选，实质更新除外。" %
                 (len(result["screened"]), min(product_count, len(result["deep_dives"])), report_cfg["repeat_suppression_days"]), ""]
        if s["batch_summary"]:
            lines.extend(["## 今天怎么看", ""])
            for row in s["batch_summary"][:2]:
                lines.append("- " + markdown(cut(row["text"], 240)))
            lines.append("")
        lines.extend(["## 重点产品", ""])
        if not result["deep_dives"]:
            lines.extend(["本次没有适合进入重点区的新增产品。", ""])
        for product in result["deep_dives"][:product_count]:
            features = cut(product["core_features"][0]["text"], 190)
            customers = "、".join(x["segment"] for x in product["customer_segments"][:3])
            thought = product["derived_thoughts"][0] if product["derived_thoughts"] else None
            lines.extend(["### " + markdown(product["name"]), "", markdown(cut(product["one_liner"], 130)), "",
                          "- **核心功能**：" + markdown(features),
                          "- **产品定位**：" + markdown(cut(product["positioning"], 170)),
                          "- **主要客户**：" + markdown(cut(customers, 170))])
            if thought:
                lines.append("- **衍生思考**：%s——%s" %
                             (markdown(cut(thought["title"], 45)), markdown(cut(thought["analysis"], 190))))
            lines.extend(["- **继续关注的理由**：" + markdown(cut(product["watch_reason"], 150)),
                          "- **入口**：" + source_link(product["id"]), ""])
        if s["derived_thoughts"] and insight_count:
            lines.extend(["## 跨产品衍生思考", ""])
            for row in s["derived_thoughts"][:insight_count]:
                lines.extend(["- **%s**：%s；边界：%s" %
                              (markdown(cut(row["title"], 60)), markdown(cut(row["product_direction"], 160)),
                               markdown(cut(row["caveat"], 120)))])
            lines.append("")
        day_dir = Path(result.get("artifact_root", ROOT)) / "reports" / result["day"]
        lines.extend(["## 展开阅读", "",
                      "[%d 份完整产品档案](<%s>) · [全部产品卡片](<%s>) · [采集记录](<%s>)" %
                      (len(result["deep_dives"]), day_dir / "products.md", day_dir / "catalog.md", day_dir / "collection.md"), "",
                      "样本来自已配置来源，客户与衍生判断中未被来源明确支持的部分均属于模型推断。"])
        return "\n".join(lines) + "\n"

    product_count = min(report_cfg["daily_feature_count"], len(result["deep_dives"]))
    insight_count = min(report_cfg["daily_insight_count"], len(s["derived_thoughts"]))
    text = build(product_count, insight_count)
    while len(text) > report_cfg["daily_max_chars"] and (insight_count > 1 or product_count > 3):
        if insight_count > 1:
            insight_count -= 1
        else:
            product_count -= 1
        text = build(product_count, insight_count)
    return text


def render_analysis(result):
    s, sources = result["synthesis"], result["sources"]

    def refs(ids):
        return "；".join(link(sources[x]["title"], sources[x]["discussion_url"]) for x in dict.fromkeys(ids))

    lines = ["# 创业产品观察日报 · " + result["day"], "", "**" + markdown(s["headline"]) + "**", "",
             "分析方向：" + markdown(result["focus"]), "",
             "大模型逐条整理 **%d** 条信息，重点研究 **%d** 个产品；可比较历史 **%d** 天，待处理 **%d** 条。" %
             (len(result["screened"]), len(result["deep_dives"]), result["history_days"], result["pending"]), "",
             "正文先回答产品是什么、解决什么问题、服务哪些客户，再给出衍生思考。功能、价格和产品阶段以标注来源为据；客户推断与延伸判断会明确标注。重点产品档案与综合内容均经过第二轮模型事实与措辞复核。", "",
             "## 重点产品速览", ""]
    for product in result["deep_dives"]:
        customers = "、".join(c["segment"] for c in product["customer_segments"][:3])
        lines.extend(["- **%s**：%s" % (markdown(product["name"]), markdown(product["one_liner"])),
                      "  主要客户：" + markdown(customers) + "。产品入口：" + refs([product["id"]])])
    lines.extend(["", "## 本批产品概览", ""])
    for row in s["batch_summary"]:
        lines.extend(["- " + markdown(row["text"]), "  相关产品：" + refs(row["evidence_ids"])])
    lines.extend(["", "## 本期重点产品", ""])
    evidence = {e["id"]: e for product in result["evidence"] for e in product["evidence"]}
    for p in result["deep_dives"]:
        lines.extend(["### " + markdown(p["name"]), "", "**一句话定位**：" + markdown(p["one_liner"]), "",
                      "**产品定位**：" + markdown(p["positioning"]), "", "#### 核心功能", ""])
        for feature in p["core_features"]:
            citations = "、".join(link("来源", evidence[x]["url"]) for x in dict.fromkeys(feature["evidence_ids"]))
            lines.append("- " + markdown(feature["text"]) + "（" + citations + "）")
        lines.extend(["", "#### 客户是谁", ""])
        for customer in p["customer_segments"]:
            citations = "、".join(link("来源", evidence[x]["url"]) for x in dict.fromkeys(customer["evidence_ids"]))
            lines.extend(["- **%s**：%s；使用场景：%s；要完成的任务：%s。依据性质：%s（%s）" %
                          (markdown(customer["segment"]), markdown(customer["role"]), markdown(customer["scenario"]),
                           markdown(customer["need"]), markdown(customer["basis"]), citations)])
        product_fields = [("workflow", "典型使用流程"), ("value_proposition", "核心价值"),
                          ("alternatives", "现有替代方式"), ("differentiation", "差异点"),
                          ("pricing_fact", "收费与商业模式"), ("business_stage", "产品阶段")]
        lines.append("")
        for key, label in product_fields:
            lines.extend(["**%s**：%s" % (label, markdown(p[key])), ""])
        lines.extend(["#### 衍生思考", ""])
        for thought in p["derived_thoughts"]:
            citations = "、".join(link("来源产品", evidence[x]["url"]) for x in dict.fromkeys(thought["evidence_ids"]))
            lines.append("- **%s**：%s（%s）" % (markdown(thought["title"]), markdown(thought["analysis"]), citations))
        lines.extend(["", "**为什么值得关注**：" + markdown(p["watch_reason"]), "",
                      "**风险**：" + "；".join(markdown(x) for x in p["risks"]), "",
                      "**仍需核实**：" + "；".join(markdown(x) for x in p["unknowns"]), "",
                      "产品入口：" + refs([p["id"]]), "", "---", ""])
    lines.extend(["## 跨产品客户观察", ""])
    for row in s["customer_patterns"]:
        lines.extend(["### " + markdown(row["name"]), "", "**客户群**：" + markdown(row["customers"]), "",
                      "**共同任务**：" + markdown(row["shared_need"]), "",
                      "**产品如何分别处理**：" + markdown(row["products_comparison"]), "",
                      "**衍生判断**：" + markdown(row["implication"]), "", "相关产品：" + refs(row["evidence_ids"]), ""])
    lines.extend(["## 产品形态横向观察", ""])
    for row in s["product_patterns"]:
        lines.extend(["### " + markdown(row["name"]), "", markdown(row["observation"]), "",
                      "**代表产品**：" + markdown(row["examples"]), "",
                      "**真正不同的地方**：" + markdown(row["what_is_different"]), "",
                      "相关产品：" + refs(row["evidence_ids"]), ""])
    lines.extend(["## 从这些产品延伸出的思考", ""])
    for row in s["derived_thoughts"]:
        lines.extend(["### " + markdown(row["title"]), "", "- **观察起点**：" + markdown(row["starting_point"]),
                      "- **可能服务的客户**：" + markdown(row["target_customer"]),
                      "- **可延伸的产品方向**：" + markdown(row["product_direction"]),
                      "- **为什么可能成立**：" + markdown(row["reasoning"]),
                      "- **需要警惕**：" + markdown(row["caveat"]), "", "相关产品：" + refs(row["evidence_ids"]), ""])
    lines.extend(["## 后续值得观察的信号", ""])
    for row in s["watch_signals"]:
        lines.extend(["- " + markdown(row["text"]), "  相关产品：" + refs(row["evidence_ids"])])
    lines.extend(["## 资料边界与运行记录", ""])
    for text in s["limitations"]:
        lines.append("- " + markdown(text))
    u = result["usage"]
    lines.extend(["", "模型：%s。本次 API 成功 %d 次，缓存命中 %d 次，失败尝试 %d 次。" %
                  (markdown(result["model"]), u["successful"] or 0, u["cached"] or 0, u["failed"] or 0),
                  "已报告用量：输入 %d / 输出 %d tokens；%d 次请求未返回用量，不据此估算精确费用。" %
                  (u["input_tokens"], u["output_tokens"], u["unknown_usage"] or 0), "",
                  "**质量复核记录**：" + "；".join(markdown(x) for x in result.get("quality_notes", [])), "",
                  "分析启动时间：%s（%s）。实际采集区间以各来源记录为准。" %
                  (dt.datetime.fromtimestamp(result["cutoff_at"], ZoneInfo(result.get("timezone", "Asia/Shanghai"))).strftime("%Y-%m-%d %H:%M:%S"),
                   result.get("timezone", "Asia/Shanghai")), ""])
    for source in result.get("collection_status", []):
        names = result.get("source_names", {})
        status = {"ok": "成功", "limited": "有限历史，缺口详见采集清单", "failed": "失败"}.get(source["last_status"], "未知")
        lines.append("- %s：%s；最近采集检查点 %s。" % (names.get(source["id"], source["id"]), status,
                     stamp(source["cursor_at"]) if source["cursor_at"] is not None else "尚无"))
    artifact_root = Path(result.get("artifact_root", ROOT))
    day_dir = artifact_root / "reports" / result["day"]
    lines.extend(["", "[每日短报](<%s>) · [全量产品中文归纳](<%s>) · [原始采集清单与覆盖缺口](<%s>)" %
                  (day_dir / "daily.md", day_dir / "catalog.md", day_dir / "collection.md"), ""])
    rendered = "\n".join(lines)
    # Models occasionally repeat internal evidence IDs inside prose even though
    # citations are rendered separately. Keep those implementation IDs out of
    # the reader-facing report.
    return re.sub(r"[（(]?p\\?_[0-9a-f]{16}:(?:source|page)(?:[、,，]\s*p\\?_[0-9a-f]{16}:(?:source|page))*[）)]?", "", rendered)


def render_catalog(result):
    lines = ["# 全量产品目录 · " + result["day"], "", "逐条由大模型整理。产品关注度用于决定后续深读顺序，不代表商业成功概率。", "",
             "| 产品 | 分类 / 阶段 | 一句话定位 | 核心功能 | 目标客户 / 场景 | 产品关注度 / 置信度 | 为什么有意思 |", "|---|---|---|---|---|---|---|"]
    for p in result["screened"]:
        source = result["sources"][p["id"]]
        lines.append("| %s | %s | %s | %s | %s | %d / %s | %s |" %
                     (link(p["name"], source["discussion_url"]), markdown(p["category"] + " / " + p["product_stage"]),
                      markdown(p["one_liner"]), markdown(p["core_function"]),
                      markdown(p["target_customers"] + " / " + p["use_case"]), p["product_score"],
                      markdown(p["confidence"]), markdown(p["why_interesting"])))
    return "\n".join(lines) + "\n"
