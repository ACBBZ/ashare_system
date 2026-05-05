---
name: ashare-finance-workflows
description: >
  A 股/中国金融投研与短线交易总控伞形技能。覆盖 AkShare 数据获取与降级、财经新闻、TrendRadar 本地新闻层、个股深度分析、异动监控、主板短线候选池、09:00/09:26 开盘流程、盘中 cron、盘后 Obsidian 复盘与持仓账本。用于任何 A 股盘前/盘中/盘后分析、自动化监控、飞书推送、短线操作表或单票综合研判任务。
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [finance, a-share, akshare, trading, cron, news, obsidian, trendradar]
    related_skills: []
---

# A 股金融投研与交易自动化总控

## Overview

这是 A 股/中国金融任务的伞形入口。不要再为一次盘前简报、一个 AkShare 降级坑、一次 TrendRadar 部署或一只股票研判创建独立技能；把具体经验放到本技能的 `references/`，主流程留在这里。

## When to Use

使用本技能处理：

- A 股、ETF/LOF、指数、板块、资金流、财务、估值、公告、新闻、龙虎榜、研报等数据获取与分析。
- 盘前 09:00 简报、09:26 开盘操作表、盘中静默监控、15:05 持仓盈亏表、17:30 收盘摘要、盘后 Obsidian 复盘。
- 主板短线候选池、强板块识别、单票买卖区间、异常成交/换手/涨跌幅监控。
- AkShare 接口不可用、EastMoney 限速、行情源失效时的数据降级。
- TrendRadar / Google News / 本地 RSS 等新闻层部署与评估。

不要用于：非中国市场的通用金融解释、无需数据的概念问答、纯会计/税务任务。

## Default Market Rules

- 默认聚焦 A 股主板短线体系；创业板/科创板/北交所、ST、可转债除非用户明确要求，否则单独标注或排除。
- ETF/LOF 与股票分开取数、分开评价，不能套用同一技术面阈值。
- 输出交易建议必须包含风险提示；不要把模型判断包装成确定性收益。
- 所有“今天/昨日/最近”类表述在自动化报告中应落到具体交易日。

## Data Layer: AkShare First, Fallback Fast

1. 优先使用 `akshare` 获取实时行情、历史行情、指数/板块、财务、资金流、公告等。
2. AkShare / EastMoney 超时、限速或字段异常时，立即切换降级源：腾讯实时行情、新浪实时、雪球实时、pytdx。
3. 缓存稳定事实（股票名称、代码映射、板块归属），实时价格与成交数据每次重新取。
4. 对脚本输出保留原始 JSON/CSV，报告层只引用加工后的结论。

详细历史经验见：
- `references/akshare-open.md`
- `references/akshare-stock-data.md`
- `references/akshare-fallback-data-fetch.md`

## News Layer: Finance News + TrendRadar

- 盘前/盘中新闻使用多源交叉：Google News、TrendRadar、本地 RSS/网页源、公告/研报接口。
- 新闻进入候选池前必须归类：宏观、政策、行业、公司公告、资金/情绪、突发事件。
- TrendRadar 适合做本地财经新闻聚合层；无 Docker 环境优先验证 uv/Python 本地运行，先跑 doctor，再最小配置，再完整任务。

参考：
- `references/finance-news-cn.md`
- `references/trendradar-ashare-news-assessment.md`
- `references/trendradar-local-finance-deploy.md`

## Single-Stock Analysis Workflow

对单票深度研判按以下顺序：

1. 识别标的：代码、市场、是否 ETF/LOF/ST/非主板。
2. 数据底座：实时/历史行情、指数环境、所属行业/板块、资金流、财务摘要、公告/新闻。
3. 基本面：营收利润、现金流、资产负债、估值、行业位置。
4. 技术面：趋势、量价、支撑压力、通道、均线、换手、波动。
5. 情绪/事件：新闻催化、公告、研报、龙虎榜、同板块联动。
6. 结论分层：短线交易型 vs 研究型；给出观察、买点、止损、失效条件。

参考：
- `references/stock-analysis-cn.md`
- `references/stock-deep-analysis-cn.md`
- `references/stock-monitor-anomaly.md`

## Shortline Mainboard Workflow

完整短线流程：

1. 市场环境：指数趋势、涨跌家数、成交额、强弱板块。
2. 强板块识别：持续性、资金强度、政策/事件催化。
3. 候选池：主板优先，分层标注 A/B/C，不做伪精确排名。
4. 单票分析：趋势、量能、支撑压力、明日计划。
5. 执行表：持仓处理、新买观察、明确买卖点区间、止损/放弃条件。
6. 复盘入库：将结论写入 Obsidian/SQLite，供次日脚本读取。

原总控经验见 `references/shortline-mainboard-workflow.md`。

## Automation Schedule Map

- **09:00 开盘前简报**：新闻层 + 前一日复盘 + 持仓/旧候选异动 + 今日关注。
- **09:26 开盘操作表**：集合竞价/开盘数据 + 持仓 + 候选池 + 大盘/强板块，输出可执行操作。
- **盘中静默监控**：采集指数、板块、持仓、候选池、异动；不频繁打扰。
- **15:05 持仓盈亏表**：SQLite 账本 + 收盘/近实时价格，发送简表。
- **17:30 收盘摘要**：盘中采集汇总 + 收盘数据 + 候选池更新 + 明日计划。
- **盘后 Obsidian 分析**：持仓与近 3 个交易日候选池，输出趋势、支撑压力、明日策略。

参考：
- `references/ashare-opening-brief.md`
- `references/ashare-opening-action-table-0926.md`
- `references/ashare-monitor-cron.md`
- `references/ashare-ledger-db-workflow.md`
- `references/ashare-position-watch-obsidian.md`

## Engineering Refactor Safety

For engineering audits, refactors, or architecture cleanup of the existing A 股 automation, default to a **sidecar + shadow mode** migration. Treat current production scripts and Cron jobs as read-only until shadow outputs have matched production for multiple trading days. Do not modify `.hermes/scripts` production scripts, production DB schemas, or Feishu delivery while designing the refactor. First build `/home/admin/ashare_agent/` or another sidecar package that reads legacy DB/Markdown, writes to a separate shadow output directory, and produces structured diff reports. Detailed production chain map, migration order, shadow output layout, promotion criteria, and first-week tasks: `references/low-risk-engineering-refactor-shadow-mode.md`.

### Time-Travel Audit Fixes

When implementing first-batch “时间穿越” fixes, keep the scope narrow: build shared `target_date / asof` validation utilities first, with TDD and timestamped backups if the scripts directory is not under git; do not simultaneously wire them into all cron/report jobs, change schemas, or alter report formats. For the盘后持仓/候选分析 MVP, strict-today means missing today quote must render “今日行情缺失，未生成技术判断” and must not silently reuse the previous trading day’s last bar. For close-summary DB latest queries, anchor `target_date=TODAY` and `asof_time=now`, add `trade_date = ? AND captured_at <= ?` to latest run selection, add defensive `trade_date = ?` to second-stage snapshot reads/aggregations, and return `None`/`[]` when target-date data is missing rather than falling back to previous-day/global latest.

For background monitor / intraday snapshot audits, remember the script is not only a writer: `load_recent_sector_context_from_db()` can read cached `sector_constituent_snapshots` and `sector_snapshots` using global latest ordering. Treat any `ORDER BY trade_date DESC, captured_at DESC ... LIMIT ...` without `WHERE trade_date = ? AND captured_at <= ?` as a high-risk time-travel bug. The existing snapshot schema already has `trade_date`, `captured_at`, and `run_id`, so prefer SQL filter fixes and tests over schema/Cron changes. Keep file-latest watchlist parsing (`read_latest_close_summary`) separate unless the user explicitly asks to change observation-pool semantics.

For review-only acceptance of these fixes, do not rerun the production monitor as a dry-run unless the script has an explicit no-write/no-delivery mode. `ashare_background_monitor.py` writes JSONL, latest markdown, and SQLite in its normal `main()`, so validate via pytest with monkeypatched `ROOT`/`DB_PATH`, `py_compile`, static SQL inspection, cron listing, and file mtime/status checks. In non-git `.hermes/scripts`, state that diff-level scope proof is unavailable and use modification times plus targeted searches as evidence.

Session-specific details and test commands: `references/ashare-time-travel-validation-mvp.md`, `references/ashare-close-summary-db-date-filter.md`, `references/ashare-background-monitor-db-latest-audit.md`, `references/ashare-background-monitor-db-date-filter.md`, `references/ashare-time-travel-stage-acceptance-summary.md`.

For stage-level acceptance after tasks 1/2/3/4/5, treat success as “first-stage complete, not risk zero”: run the 11 focused pytest files plus `py_compile`, summarize covered risks, explicitly call out remaining sector-context fallback in `ashare_position_watch_analysis.py`, and recommend observing one full real automated cycle before higher-risk code changes. The production observation checklist and exact regression commands are preserved in `references/ashare-time-travel-stage-acceptance-summary.md`.

## Storage Conventions

- SQLite：适合持仓账本、交易记录、每日快照、盈亏表。
- Obsidian：适合复盘、人类阅读的策略与候选池。
- JSON/CSV：适合脚本间传递和 cron 上下文注入。
- 飞书/聊天平台：只发摘要和操作表，不发大段原始数据。

## Output Checklist

- [ ] 明确交易日和数据时间戳。
- [ ] 区分股票与 ETF/LOF。
- [ ] 列出数据源及任何降级。
- [ ] 给出支撑、压力、买点、止损、放弃条件。
- [ ] 标注不确定性与风险。
- [ ] 自动化任务写入可复用文件，便于次日引用。
