# 创业产品日报

每天自动从 Show HN、Product Hunt、GitHub Trending、Reddit、V2EX、小众软件等社区采集新产品，交给大模型逐条归纳、筛选、深读和复核，输出一份**几分钟读完的创业产品短报**，以及完整的产品档案与全量目录。

- **零依赖**：仅使用 Python 3.9+ 标准库 + SQLite，不依赖浏览器（Windows 需额外安装时区数据 `tzdata`）。
- **大模型研究**：默认使用 DeepSeek（可替换为任何兼容 OpenAI 接口的模型服务），多阶段分析 + 两轮复核，引用由程序校验。
- **可靠补抓**：每个来源独立检查点，关机几天后会自动补抓离线期间的内容；失败不推进检查点，覆盖缺口明确记录。
- **一键定时**：`python3 schedule.py install` 自动识别系统，使用 macOS launchd、Linux systemd/cron 或 Windows 任务计划程序，无需管理员权限。

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

# 3. 阅读结果：打开项目目录下的 latest.md

# 4. 安装每日定时任务（可选）
python3 schedule.py install
```

> **网络代理**：`config.json` 默认开启代理 `http://127.0.0.1:1087`。如果你不需要代理，或代理端口不同，先修改 `proxy_enabled` / `proxy_url`（见下文“代理开关”），否则所有网络请求都会失败。

> **Windows 用户**：命令中的 `python3` 换成 `python`，`cp` 换成 `copy`，并先执行 `pip install tzdata`。

常用命令：

```sh
python3 radar.py run            # 采集并分析
python3 radar.py run --if-due   # 仅当本周期尚未运行时执行（定时任务使用此命令）
python3 radar.py analyze        # 已采集完，只重跑 / 继续大模型分析
python3 radar.py status         # 查看各来源检查点与最近运行状态
python3 radar.py stats          # 各来源最近 30 天的产出率（--days N 调整天数）
python3 radar.py feedback <反馈ID或产品名> good|bad --note "原因"   # 告诉系统你喜欢 / 不喜欢哪个产品
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
| YC Launches | YC Launches 公开 JSON 列表 | 按真实 `created_at` 分页，保存产品官网、YC 发布页、标签与批次 |
| YC 公司目录更新（默认停用） | YC 官方 companies sitemap | 只有网址缩写、没有产品描述，实测卡片全部低置信度，默认停用；`lastmod` 是页面更新日，不等于产品发布日期 |
| Product Hunt | 官方 Atom 订阅 | 仅近期条目，无需 API 令牌 |
| GitHub Trending | 解析每日热门页面 | 当前榜单快照；用 `owner/repo` 稳定去重，没有历史补抓能力 |
| Reddit r/SideProject、r/SaaS、r/indiehackers、r/microsaas、r/alphaandbetausers | 子版块 RSS 订阅 | 仅近期条目；来源独立重试与记录缺口 |
| HN 月度帖 “Ask HN: What are you working on?” | Algolia HN Search API | 读取近 45 天内月度帖的一级回复，每条回复视为一个产品自述；默认只保留至少 80 字符且带产品链接的回复。新帖发布后几天内回复量较大 |
| HelloGitHub 月刊 | 月刊 RSS + 期刊页面内嵌数据 | 每期拆分为单个开源项目，以 `owner/repo` 去重 |
| Hugging Face Spaces 热门 | 官方公开 API | 当前热门榜快照，只保留 30 天内创建的 Space，没有历史补抓能力 |
| 少数派 | 站点 RSS | 仅最近约 10 条，含非产品类文章，由模型筛选 |
| 阮一峰科技爱好者周刊 | 博客 Atom 订阅 | 一篇周刊包含多个工具，作为一个条目分析 |
| TechCrunch Startups | 频道 RSS | 偏融资与公司新闻，正文只有摘要 |
| BetaList | 带日期的产品列表分页 | 按发布日期逐页回溯到检查点；用产品 slug 去重 |
| 小众软件 | 站点 RSS，`feed/?paged=N` | 逐页回溯到时间起点；分页失效时报告失败而不跳过 |
| V2EX 分享创造 | 节点 Atom 订阅 | 仅近期约 50 条，可能缺失的区间会明确记录 |

Indie Hackers Products、Uneed、Peerlist Launchpad 已在配置中保留占位，但因没有适合独立定时程序的稳定公开产品 API/RSS，或自动请求被 Cloudflare 拦截，默认**未启用**。Lobsters Show（`lobste.rs/t/show.rss`）已加入配置但默认停用：在部分网络环境下无法连接，可达时将 `enabled` 改为 `true` 即可。36 氪的订阅地址目前返回反爬验证页，未接入。Reddit 的 r/startups feed 持续返回 429，也暂时停用；r/SideProject、r/SaaS、r/indiehackers 与 HN 创业讨论继续覆盖相邻信号。

已启用的 19 个采集来源均无需密钥；只有模型分析需要 `LLM_API_KEY`。

## 工作原理

### 采集与补抓

- **首次运行**：默认回看 3 天；首次起点写入数据库，即使首次失败也不会向后漂移。
- **后续运行**：每个来源使用独立检查点 `sources.cursor_at`，并额外重叠回看 48 小时，减少延迟收录和边界漏抓。
- **去重**：以 `(source_id, external_id)` 为唯一键；重跑时更新正文和热度，保留首次发现时间。相同链接的跨来源内容在精选中合并。
- **跨日去重**：最近 30 天已入选的产品（含改名、功能几乎相同的项目）默认不再进入短报，除非有新版本、新定价、新客户群等实质更新。全量目录仍保留全部新条目。
- **失败处理**：网络错误、解析错误、分页重复、请求超限等都不推进该来源检查点，下次重新补抓；其他来源独立完成。
- **事务与并发**：条目、检查点、覆盖缺口在同一 SQLite 事务提交；进程锁防止定时和手动任务并发写入；报告以原子替换方式写入。
- **有限 RSS**：只提供近期条目的订阅源，若最早可见条目晚于应补抓起点，会把可能缺失的区间永久写入 `coverage_gaps`——这表示"采集成功但历史覆盖有限"，而非完整补抓成功。
- **页面快照**：GitHub Trending 以采集时间记入数据库。YC 公司目录按页面 `lastmod` 发现更新；若站点批量重写日期并超过阈值，该来源失败且不推进检查点，避免把全库误当新品。

### 筛选与分析流程

每天的条目经过一个逐层收窄的漏斗：越便宜的环节越先过滤，模型只在值得的地方花算力。

1. **补充首页摘要（不调用模型）**：来源只给标题或简介很短的条目，先读取产品首页的标题、描述标签和开头正文，作为 `page_preview` 一起交给模型。讨论站（HN、Reddit、V2EX）不抓取；首页缓存一天，深读阶段复用。
2. **逐条产品卡片**：模型说明每条是什么、核心功能、定位、客户、场景和阶段，并按 5 个有锚点的维度各打 1—5 分：信息具体度、定位清晰度、客户明确度、形态新意、对小团队的借鉴价值。总分由程序按 `score_weights` 加权算出，不让模型直接给笼统分数。
3. **程序排序与截断**：总分 + 来源内热度排名（不同平台的点赞数不直接比较）+ 跨来源出现加分。同一产品官网（或 GitHub 仓库、HF Space）在多个来源出现时合并为一张卡片。每个类别保留最低名额后，取前 `select_pool_size`（默认 60）张作为候选池。
4. **模型精选**：只在候选池里挑约 12 个值得深读的产品，同时参考近 30 天已入选产品（避免重复）和你的反馈样例。
5. **补充证据并深读**：抓取重点产品的公开页面，开启思考模式研究功能、客户、流程、价值、替代方案、差异点、收费与衍生思考。
6. **逐份产品复核**：第二轮模型检查每份档案，收窄缺乏证据的确定性表述。
7. **横向编辑与综合复核**：基于候选池、复核后档案和近 7 天日报形成跨产品判断，再检查引用和措辞。
8. **短报与档案分离**：后台保留完整档案，`daily.md` 只摘取最重要的部分。

### 持续调优

- **看来源产出**：`python3 radar.py stats` 列出每个来源的条目数、产品率、低置信度比例、平均分，以及进入候选池、深读和日报的次数。长期没有产出的来源可以在 `config.json` 里停用。
- **给反馈**：日报每个重点产品后都有“反馈 ID”。执行 `python3 radar.py feedback <反馈ID> good --note "工作流很具体"` 或 `bad`，最近 20 条反馈会作为口味样例交给精选环节；`stats` 也会按来源汇总好评和差评。
- **调权重**：如果更看重某个维度，比如“对小团队的借鉴价值”，提高 `score_weights` 里对应的权重即可。

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
| `source_start_interval_seconds` | 不同来源之间的间隔，默认 2 秒，避免同一平台的多个 feed 连续触发限流 |
| `proxy_enabled` | 代理总开关。`true` 时使用 `proxy_url`，`false` 时完全忽略该地址并直连 |
| `proxy_url` | HTTP(S) 代理地址；当前为本机 `127.0.0.1:1087`，启用后由采集、产品页读取和 DeepSeek 共用，launchd 无需继承终端环境 |
| `sources[].enabled` | 启用或停用某个来源 |
| `llm.provider`、`base_url`、`model` | 默认 DeepSeek `deepseek-flash`；其他兼容服务将 `provider` 设为 `compatible` |
| `llm.focus` | 研究方向描述 |
| `llm.batch_size`、`concurrency` | 每批条数（默认 16）与并发请求数（默认 3） |
| `llm.deep_dive_count` | 深读产品数，默认 12 |
| `llm.preview_pages` | 是否为简介不足的条目读取首页摘要，默认开启（受 `fetch_product_pages` 总开关控制） |
| `llm.preview_below_chars`、`preview_max_chars` | 简介少于多少字符时读取首页（默认 200）；摘要最多保留多少字符（默认 600） |
| `llm.preview_timeout_seconds`、`preview_concurrency` | 首页读取超时（默认 8 秒）与并发数（默认 8） |
| `llm.score_weights` | 5 个评分维度的权重，默认各 0.2 |
| `llm.select_pool_size`、`min_per_category` | 交给模型精选的候选池大小（默认 60）与每个类别最低名额（默认 2） |
| `llm.max_source_share`、`max_category_share` | 候选池中单个来源 / 单个类别的最高占比（默认 0.4 / 0.35）；候选不足时自动放宽 |
| `llm.max_pick_share` | 精选的重点产品中单个来源 / 单个类别最多占比（默认 0.4，即 12 个中最多 5 个），由程序校验 |
| `llm.engagement_bonus`、`cross_source_bonus` | 来源内热度排名最高加分（默认 10）；每多一个来源出现加分（默认 8，最多计 2 个） |
| `llm.feedback_examples` | 交给精选环节的最近反馈条数，默认 20 |
| `llm.max_items_per_run`、`max_calls_per_run` | 单次处理上限，默认 1200 条 / 120 次请求（含重试） |
| `llm.enabled` | 设为 `false` 时只生成原始采集日报 |
| `report.daily_feature_count` | 短报重点产品数，默认 4 |
| `report.daily_insight_count` | 短报跨产品思考数，默认 2 |
| `report.daily_max_chars` | 短报字符上限，默认 6000 |
| `report.repeat_suppression_days` | 重点产品跨日去重窗口，默认 30 天 |

### 代理开关

代理由 `proxy_enabled` 控制。开启代理：

```json
"proxy_enabled": true,
"proxy_url": "http://127.0.0.1:1087"
```

关闭代理并直接联网：

```json
"proxy_enabled": false,
"proxy_url": "http://127.0.0.1:1087"
```

关闭时程序会完全忽略 `proxy_url`。开启时，采集请求、重点产品网页读取和 DeepSeek API 请求共用该 HTTP(S) 代理。`127.0.0.1:1087` 只是当前机器的配置；如果代理软件使用其他端口，需要同步修改 `proxy_url`。这里不能填写 SOCKS 端口。

修改代理开关或地址后，下次运行立即生效，无需重新安装定时任务。如果代理已开启但代理软件没有运行，对外网络请求会失败，来源检查点不会推进，之后可以重新补抓。

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

## 定时任务

`schedule.py` 会自动识别操作系统，在当前用户下安装每日定时任务，无需 sudo / 管理员权限。

| 系统 | 调度方式 | 每日触发 | 错过后补跑 |
|---|---|---|---|
| macOS | launchd 用户级任务 | `daily_time`（系统时区） | 登录时检查 |
| Linux | systemd 用户定时器 | `daily_time`（按 `timezone` 配置，与系统时区无关） | 开机 / 登录后补跑 |
| Linux（无 systemd） | cron | `daily_time`（系统时区） | 开机后补跑一次 |
| Windows | 任务计划程序 | `daily_time`（系统时区） | 登录时检查，并在错过后尽快补跑 |

所有方式实际执行的都是 `radar.py run --if-due`：本周期已完成时直接退出，因此多次触发不会重复采集或重复调用模型。

### macOS

```sh
cd product-radar
python3 schedule.py install      # 安装
python3 schedule.py status       # 确认：输出中出现 state、last exit code 等信息即为已注册
python3 schedule.py uninstall    # 卸载
```

- 安装位置：`~/Library/LaunchAgents/com.yanli.product-radar.plist`
- 运行日志：`logs/radar.log`；调度器输出：`logs/launchd.stdout.log`、`logs/launchd.stderr.log`
- 也可以用 `launchctl list | grep product-radar` 确认任务已加载。
- 系统时区需与 `config.json` 的 `timezone` 一致（系统设置 → 通用 → 日期与时间）。
- 睡眠时错过的任务在唤醒后补跑；关机或注销时不运行，下次登录时补跑。

### Linux

```sh
cd product-radar
python3 schedule.py install      # 安装（有 systemd 用户会话时用 systemd，否则自动改用 cron）
python3 schedule.py status       # 确认：显示下次触发时间和上次执行结果
python3 schedule.py uninstall    # 卸载
```

**systemd（多数桌面和服务器发行版）**

- 安装位置：`~/.config/systemd/user/product-radar.service`、`product-radar.timer`
- 确认：`systemctl --user list-timers product-radar.timer`，NEXT 一列为下次运行时间。
- 调度器输出：`journalctl --user -u product-radar -e`；运行日志：`logs/radar.log`
- 默认只在用户登录期间运行。服务器上希望未登录也运行，执行一次：`loginctl enable-linger $USER`
- 触发时间按 `config.json` 的 `timezone` 计算，不要求系统时区一致（需 systemd 235 及以上）。

**cron（无 systemd 的环境，如部分容器 / WSL）**

- 写入当前用户的 crontab：每天 `daily_time` 一条，外加开机后延迟 1 分钟补跑一条。
- 确认：`crontab -l | grep product-radar`
- 调度器输出：`logs/cron.log`；运行日志：`logs/radar.log`
- 系统时区需与 `config.json` 的 `timezone` 一致；需确保 cron 服务在运行。

可用 `--backend systemd` 或 `--backend cron` 强制指定方式，例如 `python3 schedule.py install --backend cron`。

### Windows

在 PowerShell 或命令提示符中执行：

```powershell
cd product-radar
pip install tzdata               # 首次需要：Windows 不自带时区数据
python schedule.py install       # 安装
python schedule.py status        # 确认：显示任务状态、下次运行时间、上次运行结果
python schedule.py uninstall     # 卸载
```

- 任务名为 `ProductRadar`。也可以打开「任务计划程序」（Win+R 输入 `taskschd.msc`），在「任务计划程序库」中找到它，查看历史记录或手动运行。
- 优先使用 `pythonw.exe` 在后台运行，不会弹出命令行窗口；因此没有控制台输出，请查看 `logs/radar.log`。
- 任务在用户登录期间运行；错过的触发（关机、睡眠）会在登录或开机后尽快补跑。使用电池时也会运行。
- 系统时区需与 `config.json` 的 `timezone` 一致（设置 → 时间和语言 → 日期和时间）。

### 通用说明

- 调度配置记录了安装时的 Python 绝对路径和项目路径。**修改 `daily_time`、更换 Python 或移动项目目录后，需重新执行 `install`**；修改其他配置和提示词无需重装。
- `python3 schedule.py prepare` 只在 `scheduler/` 下生成配置文件（plist / systemd 单元 / crontab 行 / 任务 XML）供审阅，不安装。
- 安装前会检查同名任务是否属于当前项目，避免覆盖其他目录的安装。
- 卸载只移除定时任务，数据库、日报和原始采集文件全部保留。
- 断网时不会无限重试；单个来源失败时报告会标明数据不全，下次运行重试。

## 项目结构

```text
radar.py            采集、数据库、报告与命令行入口
analysis.py         大模型归纳、证据补充、深读、复核、综合日报
schedule.py         跨平台定时任务安装器（macOS / Linux / Windows）
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
logs/               运行日志（自动轮转）与调度器输出
scheduler/          schedule.py 生成的调度配置
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
