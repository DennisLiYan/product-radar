# 创业产品日报

每天自动从 Show HN、Product Hunt、GitHub Trending、Reddit、V2EX、小众软件等社区采集新产品，交给大模型逐条归纳、筛选、深读和复核，输出一份**几分钟读完的创业产品短报**，以及完整的产品档案与全量目录。

- **零依赖**：仅使用 Python 3.9+ 标准库 + SQLite，无需 `pip install`，不依赖浏览器。
- **大模型研究**：默认使用 DeepSeek（可替换为任何兼容 OpenAI 接口的模型服务），多阶段分析 + 两轮复核，引用由程序校验。
- **可靠补抓**：每个来源独立检查点，关机几天后会自动补抓离线期间的内容；失败不推进检查点，覆盖缺口明确记录。
- **定时运行**：基于 macOS 系统自带的 launchd 用户级定时任务，每天定时执行，无需 sudo。

> 本项目不声称全网完整覆盖，模型输出也不保证语义正确。重要判断请回到原始链接核实。

## 快速开始

```sh
git clone <仓库地址> product-radar
cd product-radar

# 1. 配置模型密钥（建议权限 0600）
cp .env.example .env
chmod 600 .env
# 编辑 .env，填写 LLM_API_KEY=...

# 2. 手动运行一次：采集 + 分析 + 生成日报
python3 radar.py run

# 3. 阅读结果
open latest.md
```

常用命令：

```sh
python3 radar.py run            # 采集并分析
python3 radar.py run --if-due   # 仅当本周期尚未运行时执行（定时任务使用此命令）
python3 radar.py analyze        # 已采集完，只重跑 / 继续大模型分析
python3 radar.py status         # 查看各来源检查点与最近运行状态
```

## 输出内容

| 文件 | 内容 |
|---|---|
| `latest.md` | 最新短报，与当天 `daily.md` 一致 |
| `reports/YYYY-MM-DD/daily.md` | 每日短报：默认 4 个重点产品 + 最多 2 条跨产品思考，约 6000 字符以内 |
| `reports/YYYY-MM-DD/products.md` | 约 12 份完整重点产品档案 |
| `reports/YYYY-MM-DD/catalog.md` | 当天全部新增产品的卡片 |
| `reports/YYYY-MM-DD/collection.md` | 原始采集清单与覆盖缺口 |
| `reports/YYYY-MM-DD/analysis.json` | 结构化模型分析与引用材料 |

## 数据来源

| 来源 | 接入方式 | 说明 |
|---|---|---|
| Show HN | Algolia HN Search API | 时间过滤、分页；结果过多时自动拆分时间窗口，避免被截断 |
| Hacker News 创业讨论 | Algolia HN Search API | 关键词 `startup`，热度 ≥ 10 |
| YC Launches / Launch HN | Algolia HN Search API | 关键词 `Launch HN:` |
| Product Hunt | 官方 RSS 订阅 | 仅近期条目 |
| GitHub Trending | 解析每日热门页面 | 当日快照 |
| Reddit r/SideProject、r/SaaS、r/indiehackers、r/startups | 子版块 RSS 订阅 | 仅近期条目 |
| 小众软件 | 站点 RSS，`feed/?paged=N` | 逐页回溯到时间起点；分页失效时报告失败而不跳过 |
| V2EX 分享创造 | 节点 Atom 订阅 | 仅近期约 50 条，可能缺失的区间会明确记录 |

Indie Hackers、BetaList、Uneed、Peerlist Launchpad 已在配置中保留占位，但因没有稳定的公开 API/RSS 或被 Cloudflare 拦截，默认**未启用**。

采集来源均无需密钥；只有模型分析需要 `LLM_API_KEY`。

## 工作原理

### 采集与补抓

- **首次运行**：默认回看 3 天；首次起点写入数据库，即使首次失败也不会向后漂移。
- **后续运行**：每个来源使用独立检查点 `sources.cursor_at`，并额外重叠回看 48 小时，减少延迟收录和边界漏抓。
- **去重**：以 `(source_id, external_id)` 为唯一键；重跑时更新正文和热度，保留首次发现时间。相同链接的跨来源内容在精选中合并。
- **跨日去重**：最近 30 天已入选的产品（含改名、功能几乎相同的项目）默认不再进入短报，除非有新版本、新定价、新客户群等实质更新。全量目录仍保留全部新条目。
- **失败处理**：网络错误、解析错误、分页重复、请求超限等都不推进该来源检查点，下次重新补抓；其他来源独立完成。
- **事务与并发**：条目、检查点、覆盖缺口在同一 SQLite 事务提交；进程锁防止定时和手动任务并发写入；报告以原子替换方式写入。
- **有限 RSS**：只提供近期条目的订阅源，若最早可见条目晚于应补抓起点，会把可能缺失的区间永久写入 `coverage_gaps`——这表示"采集成功但历史覆盖有限"，而非完整补抓成功。

### 大模型分析流程

1. **逐条产品卡片**：分批分析全部新增条目——它是什么、核心功能、定位、目标客户、使用场景、产品阶段、关注理由。
2. **产品筛选**：从全部卡片中挑选约 12 个值得深读的产品。
3. **补充证据并深读**：抓取重点产品的公开页面，开启思考模式研究功能、客户、流程、价值、替代方案、差异点、收费与衍生思考。
4. **逐份产品复核**：第二轮模型检查每份档案，收窄缺乏证据的确定性表述。
5. **横向编辑**：结合全部卡片、复核后档案和近 7 天日报，形成扩展、垂直化、组合或反面判断。
6. **综合内容复核**：检查引用是否存在、推测是否被误写为事实，复核记录写入日报。
7. **短报与档案分离**：后台保留完整档案，`daily.md` 只摘取最重要的部分。

安全与可靠性：

- 模型只能引用输入中的产品/证据 ID，程序校验引用、字段和数量。
- 网页和帖子内容作为不可信资料处理，不执行其中的指令。
- 卡片、模型请求结果、网页证据分别缓存；配置、提示词或原文变化时对应缓存失效。
- 分析状态与采集检查点相互独立：模型失败不会丢失原始数据，`latest.md` 会显示失败原因和上次成功报告的入口。

## 配置

编辑 `config.json`：

| 字段 | 说明 |
|---|---|
| `daily_time` | 每天调度时间，默认 `09:00`；修改后需重新执行 `schedule.py install` |
| `timezone` | 报告日期和到期判断的时区，默认 `Asia/Shanghai`（launchd 本身使用系统时区，请保持一致） |
| `initial_lookback_days` | 新来源首次回看天数，默认 3 |
| `overlap_hours` | 重叠回看小时数，默认 48 |
| `top_n`、`preferred_keywords` | 原始采集精选数量和排序偏好，只影响排序不影响收录 |
| `max_requests_per_source` | 单次任务每个来源的请求上限，默认 200 |
| `sources[].enabled` | 启用或停用某个来源 |
| `llm.provider`、`base_url`、`model` | 默认 DeepSeek `deepseek-flash`；其他兼容服务将 `provider` 设为 `compatible` |
| `llm.focus` | 研究方向描述 |
| `llm.batch_size`、`concurrency` | 每批条数（默认 16）与并发请求数（默认 3） |
| `llm.deep_dive_count` | 深读产品数，默认 12 |
| `llm.max_items_per_run`、`max_calls_per_run` | 单次处理上限，默认 1200 条 / 120 次请求（含重试） |
| `llm.enabled` | 设为 `false` 时只生成原始采集日报 |
| `report.daily_feature_count` | 短报重点产品数，默认 4 |
| `report.daily_insight_count` | 短报跨产品思考数，默认 2 |
| `report.daily_max_chars` | 短报字符上限，默认 6000 |
| `report.repeat_suppression_days` | 重点产品跨日去重窗口，默认 30 天 |

提示词位于 `prompts/` 目录，可直接修改，下次运行即生效：

```text
common.txt          通用约束
screen.txt          逐条产品卡片
select.txt          重点产品筛选
deep.txt            深读
product_audit.txt   单份产品档案复核
synthesize.txt      横向编辑
audit.txt           综合内容复核
```

### 密钥与费用

- 密钥放在项目目录的 `.env` 中（已被 `.gitignore` 忽略），进程环境变量优先。真实密钥不会写入提示词、报告或日志。
- **调用模型会产生 API 费用。**日报中会记录成功调用、缓存命中、失败次数和返回的 token 用量；数据库 `llm_calls` 表保存调用元数据。首次处理多天积压的内容时，用量会明显高于之后的每日增量。

## 定时任务（macOS）

```sh
python3 schedule.py prepare     # 仅生成 scheduler/ 下的 plist 供审阅，不安装
python3 schedule.py install     # 注册用户级 LaunchAgent（无需 sudo）
python3 schedule.py status      # 查看任务状态与上次退出码
python3 schedule.py uninstall   # 移除定时任务，保留全部数据
```

- 安装位置：`~/Library/LaunchAgents/com.yanli.product-radar.plist`，使用安装时 Python 的绝对路径；**移动项目目录后需重新安装**。
- 需要用户处于登录状态；注销或关机时不运行。
- `RunAtLoad` 会在登录时检查本周期是否已运行，错过的任务会补跑；睡眠期间错过的任务由 launchd 唤醒后补跑。
- 本周期已采集但分析未成功时，`--if-due` 会继续分析。

参考：[Apple 定时任务文档](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)。

## 项目结构

```text
radar.py            采集、数据库、报告与命令行入口
analysis.py         大模型归纳、证据补充、深读、复核、综合日报
schedule.py         macOS launchd 调度安装器
config.json         来源、调度、模型与报告配置
prompts/            各分析阶段的提示词
tests/              单元测试
.env.example        密钥模板

# 以下为运行时生成，不纳入版本控制
data/radar.db       检查点、条目、运行记录、覆盖缺口、模型调用元数据
data/raw/           原始网络响应
reports/            每日报告
latest.md           最新短报
analysis-status.json 最近一次分析状态
logs/               运行日志（自动轮转）与 launchd 输出
```

备份时请暂停采集并使用 SQLite 在线备份，或连同 `-wal` / `-shm` 文件一起复制。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时目录、真实 SQLite 和本地 HTTP 服务器，覆盖离线补抓、去重、来源失败、历史分页、HN 结果上限、每日调度、模型输出引用校验、分析失败恢复与缓存复用。测试不调用真实模型 API；实际的模型质量与连通性请通过 `radar.py analyze` 或 `run` 验证。

## 参考资料

- [HN 搜索接口](https://hn.algolia.com/api)
- [小众软件订阅说明](https://www.appinn.com/feeds-subscribe/)
- [V2EX 分享创造](https://www.v2ex.com/go/create)
- [DeepSeek JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
