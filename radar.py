#!/usr/bin/env python3
"""Independent, stdlib-only product discovery collector (Python 3.9+)."""
import argparse
import contextlib
import datetime as dt
import email.utils
import fcntl
import html
from html.parser import HTMLParser
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
UTC = dt.timezone.utc
LOG = logging.getLogger("radar")


def stamp(value):
    return dt.datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not value:
        raise ValueError("条目缺少发布时间")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        raise ValueError("条目时间缺少时区")
    return int(parsed.timestamp())


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(value or "")
    return " ".join("".join(parser.parts).split())


def canonical_url(value):
    p = urllib.parse.urlsplit(value.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("条目链接不是有效 HTTP(S) URL")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    # Preserve path and semantic query parameters: different projects on the same host
    # must never be collapsed. Fragments and known tracking parameters are disposable.
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/",
                                   urllib.parse.urlencode(query), ""))


@dataclass
class Item:
    external_id: str
    title: str
    url: str
    discussion_url: str
    published_at: int
    description: str = ""
    points: int = 0
    comments: int = 0


@dataclass
class Batch:
    items: list = field(default_factory=list)
    # limited means a successful bounded feed snapshot, not complete historical coverage.
    status: str = "ok"
    note: str = ""
    gap: tuple = None


def parse_feed(body):
    root = ET.fromstring(body)
    atom = "{http://www.w3.org/2005/Atom}"
    items = []
    if root.tag == atom + "feed":
        entries = root.findall(atom + "entry")
        for entry in entries:
            links = entry.findall(atom + "link")
            url = next((x.attrib["href"] for x in links
                        if x.attrib.get("rel", "alternate") == "alternate" and "href" in x.attrib), "")
            content = entry.findtext(atom + "content") or entry.findtext(atom + "summary") or ""
            items.append(Item(
                entry.findtext(atom + "id") or url,
                plain(entry.findtext(atom + "title")), canonical_url(url), canonical_url(url),
                parse_time(entry.findtext(atom + "published") or entry.findtext(atom + "updated")),
                plain(content)[:12000]))
    elif root.tag == "rss" and root.find("channel") is not None:
        for entry in root.findall("./channel/item"):
            url = canonical_url(entry.findtext("link") or "")
            content = entry.findtext("description") or entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
            items.append(Item(entry.findtext("guid") or url, plain(entry.findtext("title")),
                              url, url, parse_time(entry.findtext("pubDate")), plain(content)[:12000]))
    else:
        raise ValueError("响应不是 RSS 或 Atom，可能是网站错误页")
    for item in items:
        if not item.external_id or not item.title:
            raise ValueError("订阅条目缺少 ID 或标题，保留检查点以便重试")
    return items


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class HTTPClient:
    def __init__(self, config, raw_dir):
        self.config = config
        self.raw_dir = raw_dir
        self.sequence = 0
        self.previous_request = 0.0

    def get(self, url):
        if self.sequence >= self.config["max_requests_per_source"]:
            raise RuntimeError("达到单次请求上限；检查点未推进，下次继续重试。可提高 max_requests_per_source")
        self.sequence += 1
        for attempt in range(self.config["http_attempts"]):
            wait = self.config["request_interval_seconds"] - (time.monotonic() - self.previous_request)
            if wait > 0:
                time.sleep(wait)
            self.previous_request = time.monotonic()
            try:
                request = urllib.request.Request(url, headers={
                    "User-Agent": "ProductRadar/1.0 (personal RSS reader)",
                    "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, */*"})
                with urllib.request.urlopen(request, timeout=self.config["http_timeout_seconds"]) as response:
                    body = response.read(12 * 1024 * 1024 + 1)
                    if len(body) > 12 * 1024 * 1024:
                        raise ValueError("响应超过 12 MiB")
                    suffix = ".json" if "json" in response.headers.get("Content-Type", "") else ".xml"
                atomic_write(self.raw_dir / ("%04d" % self.sequence + suffix), body)
                return body
            except urllib.error.HTTPError as error:
                if error.code not in (408, 429, 500, 502, 503, 504):
                    raise RuntimeError("HTTP %s (%s)" % (error.code, urllib.parse.urlsplit(url).netloc)) from None
                retry_after = error.headers.get("Retry-After", "")
                delay = min(30, int(retry_after)) if retry_after.isdigit() else 2 ** attempt
            except (urllib.error.URLError, TimeoutError, OSError):
                delay = 2 ** attempt
            if attempt + 1 == self.config["http_attempts"]:
                raise RuntimeError("网络请求失败，已重试 %s 次 (%s)" %
                                   (attempt + 1, urllib.parse.urlsplit(url).netloc)) from None
            time.sleep(delay)
        raise RuntimeError("无可用 HTTP 请求次数")

    def json(self, url):
        return json.loads(self.get(url))


def collect_hn(source, start, end, client):
    found = {}

    def query(left, right, page):
        arguments = {"tags": source.get("tags", "show_hn"), "hitsPerPage": 100,
                     "numericFilters": "created_at_i>=%d,created_at_i<%d" % (left, right),
                     "page": page}
        if source.get("query"):
            arguments["query"] = source["query"]
        params = urllib.parse.urlencode(arguments)
        data = client.json(source["url"] + "?" + params)
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list) or "nbHits" not in data:
            raise ValueError("HN 搜索 API 返回结构异常")
        if data.get("message") or data.get("error"):
            raise ValueError("HN 搜索 API 返回错误，检查点不推进")
        return data

    def window(left, right):
        first = query(left, right, 0)
        count = int(first["nbHits"])
        capacity = int(first.get("nbPages", 0)) * int(first.get("hitsPerPage", 100))
        # The search API caps accessible results. Split time ranges instead of silently
        # stopping at page 10 when catching up after a long offline period.
        exhaustive = first.get("exhaustiveNbHits", first.get("exhaustive", {}).get("nbHits", True))
        if count > 900 or count > capacity or not exhaustive:
            if right - left <= 1:
                raise RuntimeError("HN 同一秒结果超过 API 可回溯上限，检查点不推进")
            middle = (left + right) // 2
            window(left, middle)
            window(middle, right)
            return
        page_hits = []
        for page in range(max(1, int(first.get("nbPages", 0)))):
            data = first if page == 0 else query(left, right, page)
            page_hits.extend(data["hits"])
        unique = {str(hit["objectID"]): hit for hit in page_hits}
        if len(unique) < count:
            raise RuntimeError("HN 分页结果缺失或移动，检查点不推进，下次重试")
        for identity, hit in unique.items():
            published = int(hit["created_at_i"])
            if not left <= published < right:
                raise ValueError("HN API 未遵守请求时间范围")
            discussion = "https://news.ycombinator.com/item?id=" + identity
            title = plain(hit.get("title") or "")
            if not title:
                raise ValueError("HN 条目缺少标题")
            if int(hit.get("points") or 0) < int(source.get("min_points", 0)):
                continue
            found[identity] = Item(identity, title, canonical_url(hit.get("url") or discussion),
                                   discussion, published, plain(hit.get("story_text") or "")[:12000],
                                   int(hit.get("points") or 0), int(hit.get("num_comments") or 0))

    if start < end:
        window(start, end)
    detail = "标签 %s" % source.get("tags", "show_hn")
    if source.get("query"):
        detail += "，查询 %s" % source["query"]
    if source.get("min_points"):
        detail += "，至少 %d 分" % source["min_points"]
    return Batch(list(found.values()), note="按时间区间分页补抓（%s）；范围仅覆盖 Algolia 已索引内容。" % detail)


def parse_github_trending(body, published_at):
    """Parse repository cards from GitHub's public Trending page."""
    text = body.decode("utf-8", errors="replace")
    blocks = re.findall(r'<article\b[^>]*class="[^"]*\bBox-row\b[^"]*"[^>]*>(.*?)</article>',
                        text, flags=re.IGNORECASE | re.DOTALL)
    items = []
    for block in blocks:
        match = re.search(r'<h2\b[^>]*>.*?<a\b[^>]*href="(/[^/"?#]+/[^/"?#]+)"',
                          block, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        repository = html.unescape(match.group(1)).strip("/")
        if repository.count("/") != 1:
            continue
        description_match = re.search(
            r'<p\b[^>]*class="[^"]*\bcolor-fg-muted\b[^"]*"[^>]*>(.*?)</p>',
            block, flags=re.IGNORECASE | re.DOTALL)
        description = plain(description_match.group(1)) if description_match else ""
        card_text = plain(block)
        stars_match = re.search(r'([\d,]+)\s+stars?\s+today\b', card_text, flags=re.IGNORECASE)
        stars_today = int(stars_match.group(1).replace(",", "")) if stars_match else 0
        url = canonical_url("https://github.com/" + repository)
        items.append(Item(repository.lower(), repository, url, url, published_at,
                          (description + ("；今日新增 %d stars" % stars_today if stars_today else ""))[:12000],
                          stars_today, 0))
    if not items:
        raise ValueError("GitHub Trending 页面未找到仓库卡片，可能是页面结构变化")
    return items


def collect_github_trending(source, start, end, client):
    if start >= end:
        return Batch([], "limited", "GitHub Trending 是当前排行榜快照；没有可请求的历史区间。")
    items = parse_github_trending(client.get(source["url"]), end - 1)
    return Batch(items, "limited",
                 "GitHub Trending 是当前排行榜快照，不提供发布时间或历史分页；仓库以 owner/repo 去重。")


def collect_feed(source, start, end, client, required_start):
    selected = {}
    seen_pages = set()
    page = 1
    while True:
        url = source["url"].format(page=page)
        entries = parse_feed(client.get(url))
        if not entries:
            if source["kind"] == "rss":
                raise ValueError("有限订阅源返回空列表，无法确认覆盖范围，检查点不推进")
            return Batch(list(selected.values()), note="RSS 分页已到历史末尾。")
        signature = tuple(sorted(x.external_id for x in entries))
        if signature in seen_pages:
            raise ValueError("历史分页重复，无法确认覆盖范围，检查点不推进")
        seen_pages.add(signature)
        for item in entries:
            if start <= item.published_at < end:
                selected[item.external_id] = item
        oldest = min(x.published_at for x in entries)
        if source["kind"] == "rss":
            if oldest > required_start:
                gap_end = min(oldest, end)
                return Batch(list(selected.values()), "limited",
                             "仅有最近 %d 条；%s 至 %s 可能有历史缺口，已永久记录。" %
                             (len(entries), stamp(required_start), stamp(gap_end)),
                             (required_start, gap_end))
            return Batch(list(selected.values()), note="有限 RSS 快照已覆盖本次新增区间；无法保证已删除或迟到的条目。")
        if oldest <= start:
            return Batch(list(selected.values()), note="RSS 历史分页已覆盖回溯起点。")
        page += 1


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY, cursor_at INTEGER, initial_since INTEGER NOT NULL,
        last_attempt_at INTEGER, last_status TEXT, last_error TEXT);
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, started_at INTEGER NOT NULL, cutoff_at INTEGER NOT NULL,
        finished_at INTEGER, status TEXT NOT NULL, report_path TEXT);
    CREATE TABLE IF NOT EXISTS source_runs (
        run_id TEXT NOT NULL REFERENCES runs(id), source_id TEXT NOT NULL,
        since_at INTEGER NOT NULL, until_at INTEGER NOT NULL, status TEXT NOT NULL,
        found INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL,
        PRIMARY KEY(run_id,source_id));
    CREATE TABLE IF NOT EXISTS items (
        source_id TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
        url TEXT NOT NULL, discussion_url TEXT NOT NULL, published_at INTEGER NOT NULL,
        description TEXT NOT NULL, points INTEGER NOT NULL, comments INTEGER NOT NULL,
        first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
        PRIMARY KEY(source_id,external_id));
    CREATE INDEX IF NOT EXISTS items_first_seen ON items(first_seen_at);
    CREATE TABLE IF NOT EXISTS coverage_gaps (
        id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, since_at INTEGER NOT NULL,
        until_at INTEGER NOT NULL, detected_at INTEGER NOT NULL, note TEXT NOT NULL,
        UNIQUE(source_id,since_at,until_at));
    """)
    return db


def collect_source(db, source, cutoff, config, client, run_id):
    identity = source["id"]
    with db:
        db.execute("INSERT OR IGNORE INTO sources(id,initial_since) VALUES(?,?)",
                   (identity, cutoff - int(config["initial_lookback_days"] * 86400)))
    state = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
    required_start = state["cursor_at"] if state["cursor_at"] is not None else state["initial_since"]
    if required_start > cutoff:
        raise ValueError("系统时间早于数据库检查点，请检查时钟；不回退检查点")
    start = max(state["initial_since"], required_start - int(config["overlap_hours"] * 3600))
    try:
        if source["kind"] == "hn_search":
            batch = collect_hn(source, start, cutoff, client)
        elif source["kind"] in ("rss", "paged_rss"):
            batch = collect_feed(source, start, cutoff, client, required_start)
        elif source["kind"] == "github_trending":
            batch = collect_github_trending(source, start, cutoff, client)
        else:
            raise ValueError("不支持的来源类型：" + source["kind"])
        added = 0
        # Items, checkpoint and coverage warnings commit together. An interruption
        # before commit cannot move a cursor beyond unsaved records.
        with db:
            for item in batch.items:
                args = (identity, item.external_id, item.title, item.url, item.discussion_url,
                        item.published_at, item.description, item.points, item.comments, cutoff, cutoff)
                cursor = db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)", args)
                added += cursor.rowcount
                db.execute("""UPDATE items SET title=?,url=?,discussion_url=?,published_at=?,description=?,
                           points=?,comments=?,last_seen_at=? WHERE source_id=? AND external_id=?""",
                           (item.title, item.url, item.discussion_url, item.published_at, item.description,
                            item.points, item.comments, cutoff, identity, item.external_id))
            db.execute("UPDATE sources SET cursor_at=?,last_attempt_at=?,last_status=?,last_error=NULL WHERE id=?",
                       (cutoff, cutoff, batch.status, identity))
            if batch.gap and batch.gap[0] < batch.gap[1]:
                db.execute("INSERT OR IGNORE INTO coverage_gaps(source_id,since_at,until_at,detected_at,note) VALUES(?,?,?,?,?)",
                           (identity, batch.gap[0], batch.gap[1], cutoff, batch.note))
            db.execute("INSERT INTO source_runs VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, identity, start, cutoff, batch.status, len(batch.items), added, batch.note))
        LOG.info("%s: %s, fetched=%d, new=%d", source["name"], batch.status, len(batch.items), added)
        return batch.status
    except Exception as error:
        message = str(error)[:500]
        with db:
            db.execute("UPDATE sources SET last_attempt_at=?,last_status='failed',last_error=? WHERE id=?",
                       (cutoff, message, identity))
            db.execute("INSERT INTO source_runs VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, identity, start, cutoff, "failed", 0, 0, message))
        LOG.error("%s: %s", source["name"], message)
        return "failed"


def markdown(value):
    value = " ".join(str(value).split())
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", value)


def link(label, url):
    safe = urllib.parse.quote(url, safe=":/?=&%#@+;,$!~*'-._")
    return "[%s](%s)" % (markdown(label), safe)


def rank(item, config):
    text = (item["title"] + " " + item["description"]).lower()
    relevance = sum(1 for word in config["preferred_keywords"] if word.lower() in text)
    return relevance * 2 + min(5, math.log1p(max(0, item["points"]))) + min(2, math.log1p(max(0, item["comments"])))


def render_report(db, config, cutoff):
    tz = ZoneInfo(config["timezone"])
    local_now = dt.datetime.fromtimestamp(cutoff, tz)
    midnight = int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    names = {s["id"]: s["name"] for s in config["sources"]}

    def display(timestamp):
        return dt.datetime.fromtimestamp(timestamp, tz).strftime("%Y-%m-%d %H:%M:%S")

    rows = list(db.execute("SELECT * FROM items WHERE first_seen_at>=? AND first_seen_at<=? ORDER BY published_at DESC",
                           (midnight, cutoff)))
    unique = {}
    for row in sorted(rows, key=lambda r: (rank(r, config), r["published_at"]), reverse=True):
        unique.setdefault(row["url"], row)
    top = list(unique.values())[:config["top_n"]]
    latest_runs = list(db.execute("""SELECT sr.* FROM source_runs sr JOIN
                     (SELECT source_id,MAX(rowid) rid FROM source_runs GROUP BY source_id) x ON sr.rowid=x.rid"""))
    gaps = list(db.execute("SELECT * FROM coverage_gaps ORDER BY detected_at DESC"))
    lines = ["# 创业产品日报 · " + local_now.strftime("%Y-%m-%d"), "",
             "更新至：%s（%s）。本日首次发现 **%d** 条，按相同链接合并后 **%d** 条。" %
             (display(cutoff), config["timezone"], len(rows), len(unique)), "",
             "补抓到的旧内容会计入本次发现，保留原始发布时间；同日重跑保留本日已发现内容。", "",
             "## 采集状态", "", "| 来源 | 状态 | 本次回溯起点 | 截止时间 | 抓取 / 新增 |", "|---|---|---|---|---|"]
    statuses = {"ok": "成功", "limited": "成功，但有历史缺口", "failed": "失败，检查点未推进"}
    for row in latest_runs:
        lines.append("| %s | %s | %s | %s | %d / %d |" %
                     (markdown(names.get(row["source_id"], row["source_id"])), statuses[row["status"]],
                      display(row["since_at"]), display(row["until_at"]), row["found"], row["added"]))
    lines.append("")
    for row in latest_runs:
        lines.append("- **%s**：%s" % (markdown(names.get(row["source_id"], row["source_id"])), markdown(row["note"])))
    if gaps:
        lines.extend(["", "### 已记录的历史覆盖缺口", "",
                      "以下区间无法通过有限订阅源确认完整性，后续成功运行不会自动抹去这些记录。"])
        for gap in gaps[:20]:
            lines.append("- %s：%s → %s" % (markdown(names.get(gap["source_id"], gap["source_id"])),
                                           display(gap["since_at"]), display(gap["until_at"])))
        if len(gaps) > 20:
            lines.append("- 完整 %d 条缺口记录保存在数据库 coverage_gaps 表。" % len(gaps))
    lines.extend(["", "## 今日精选", "",
                  "按配置关键词和来源热度粗筛，非人工评测。以下简介摘自来源；没有正文的条目只展示标题。"])
    if not top:
        lines.extend(["", "本日暂未发现新增条目，请结合上方来源状态判断。"])
    for index, item in enumerate(top, 1):
        source_name = names.get(item["source_id"], item["source_id"])
        description = item["description"]
        excerpt = description[:280] + ("…" if len(description) > 280 else "")
        lines.extend(["", "### %d. %s" % (index, markdown(item["title"])), "",
                      "%s · 发布于 %s" % (markdown(source_name), display(item["published_at"])), "",
                      markdown(excerpt) if excerpt else "来源未提供正文摘要，点击链接查看详情。", "",
                      link("产品 / 原文", item["url"]) + " · " + link("来源讨论", item["discussion_url"])])
        if item["points"] or item["comments"]:
            lines.append("来源热度：%d 分 / %d 条评论（采集时）。" % (item["points"], item["comments"]))
    lines.extend(["", "## 本日发现的全部条目", "",
                  "| 来源 | 标题 | 原始发布时间 |", "|---|---|---|"])
    for item in rows:
        lines.append("| %s | %s | %s |" % (markdown(names.get(item["source_id"], item["source_id"])),
                                           link(item["title"], item["url"]), display(item["published_at"])))
    lines.extend(["", "生成方式：本地程序整理原文，无大模型调用。完整采集响应保存在 data/raw/。", ""])
    return "\n".join(lines)


def due_timestamp(now, config):
    local = dt.datetime.fromtimestamp(now, ZoneInfo(config["timezone"]))
    hour, minute = map(int, config["daily_time"].split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < due:
        due -= dt.timedelta(days=1)
    return int(due.timestamp())


def publish(root, config, cutoff, db):
    """Collection output is a separate appendix when model analysis is enabled."""
    local_date = dt.datetime.fromtimestamp(cutoff, ZoneInfo(config["timezone"])).strftime("%Y-%m-%d")
    collection = render_report(db, config, cutoff)
    day_dir = root / "reports" / local_date
    atomic_write(day_dir / "collection.md", collection)
    if config.get("llm", {}).get("enabled"):
        from analysis import analyze_daily
        return analyze_daily(root, config, cutoff, db)
    atomic_write(day_dir / "daily.md", collection)
    atomic_write(root / "latest.md", collection)
    return "ok"


def run(root, config, if_due=False):
    cutoff = int(time.time())
    with contextlib.closing(connect(root / "data/radar.db")) as db:
        if if_due:
            previous = db.execute("SELECT MAX(cutoff_at) FROM runs WHERE status IN ('ok','partial')").fetchone()[0]
            if previous is not None and previous >= due_timestamp(cutoff, config):
                needs_analysis = False
                if config.get("llm", {}).get("enabled"):
                    from analysis import analysis_is_due
                    needs_analysis = analysis_is_due(db, config, due_timestamp(cutoff, config))
                if not needs_analysis:
                    LOG.info("当前周期采集和分析已经完成，跳过。")
                    return 0
                LOG.info("当前周期已采集，继续尚未完成的模型分析。")
                return 0 if publish(root, config, cutoff, db) == "ok" else 1
        identity = dt.datetime.fromtimestamp(cutoff, UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        with db:
            db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            db.execute("INSERT INTO runs(id,started_at,cutoff_at,status) VALUES(?,?,?,'running')", (identity, cutoff, cutoff))
        statuses = []
        for source in config["sources"]:
            if not source.get("enabled", True):
                continue
            client = HTTPClient(config, root / "data/raw" / identity / source["id"])
            statuses.append(collect_source(db, source, cutoff, config, client, identity))
        local_date = dt.datetime.fromtimestamp(cutoff, ZoneInfo(config["timezone"])).strftime("%Y-%m-%d")
        report_path = root / "reports" / local_date / "daily.md"
        analysis_status = publish(root, config, cutoff, db)
        status = "failed" if all(x == "failed" for x in statuses) else ("ok" if all(x == "ok" for x in statuses) else "partial")
        with db:
            db.execute("UPDATE runs SET status=?,finished_at=?,report_path=? WHERE id=?",
                       (status, int(time.time()), str(report_path), identity))
        LOG.info("Finished: %s — %s", status, report_path)
        print(json.dumps({"status": status, "analysis_status": analysis_status,
                          "report": str(report_path), "latest": str(root / "latest.md")}, ensure_ascii=False))
        return 1 if "failed" in statuses or analysis_status != "ok" else 0


def read_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    ZoneInfo(config["timezone"])
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config["daily_time"]):
        raise ValueError("daily_time 必须为 HH:MM")
    for field_name in ("initial_lookback_days", "http_timeout_seconds", "http_attempts", "max_requests_per_source", "top_n"):
        if config[field_name] <= 0:
            raise ValueError(field_name + " 必须大于 0")
    if config["overlap_hours"] < 0 or config["request_interval_seconds"] < 0:
        raise ValueError("回溯时间和请求间隔不能为负数")
    identities = [s["id"] for s in config["sources"]]
    if len(identities) != len(set(identities)) or not all(re.fullmatch(r"[a-z0-9_]+", x) for x in identities):
        raise ValueError("来源 ID 必须唯一，且只包含小写字母、数字、下划线")
    if not any(s.get("enabled", True) for s in config["sources"]):
        raise ValueError("至少启用一个来源")
    llm = config.get("llm", {})
    if llm.get("enabled"):
        for key in ("batch_size", "concurrency", "deep_dive_count", "max_items_per_run", "max_calls_per_run",
                    "timeout_seconds", "max_attempts", "screen_max_tokens", "deep_max_tokens",
                    "history_days", "page_timeout_seconds", "evidence_max_chars"):
            if type(llm.get(key)) is not int or llm[key] <= 0:
                raise ValueError("llm." + key + " 必须为正整数")
        for key in ("provider", "base_url", "model", "api_key_env", "focus"):
            if not isinstance(llm.get(key), str) or not llm[key].strip():
                raise ValueError("缺少 llm." + key)
    report = config.get("report", {})
    for key in ("daily_feature_count", "daily_insight_count", "daily_max_chars", "repeat_suppression_days"):
        if type(report.get(key)) is not int or report[key] <= 0:
            raise ValueError("report.%s 必须为正整数" % key)
    if report["daily_max_chars"] < 2000:
        raise ValueError("report.daily_max_chars 不能小于 2000")
    return config


@contextlib.contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个采集任务正在运行，本次跳过") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "analyze", "status"))
    parser.add_argument("--root", type=Path, default=ROOT, help="独立数据目录，默认脚本所在目录")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--if-due", action="store_true", help="当前定时周期已成功/部分成功执行时跳过")
    args = parser.parse_args()
    config = read_config(args.config)
    root = args.root.resolve()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(root / "logs/radar.log",
                                   maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
    with process_lock(root / "data/run.lock"):
        if args.command == "status":
            with contextlib.closing(connect(root / "data/radar.db")) as db:
                from analysis import init_db
                init_db(db)
                print(json.dumps({"sources": [dict(r) for r in db.execute("SELECT * FROM sources")],
                                  "items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                                  "coverage_gaps": db.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0],
                                  "last_run": dict(db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone() or {}),
                                  "analyzed_items": db.execute("SELECT COUNT(*) FROM screenings").fetchone()[0],
                                  "last_analysis": dict(db.execute("SELECT id,status,error,cutoff_at FROM analysis_runs ORDER BY rowid DESC LIMIT 1").fetchone() or {})},
                                 indent=2, ensure_ascii=False))
            return 0
        if args.command == "analyze":
            with contextlib.closing(connect(root / "data/radar.db")) as db:
                return 0 if publish(root, config, int(time.time()), db) == "ok" else 1
        return run(root, config, args.if_due)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        LOG.error("%s", error)
        sys.exit(1)
