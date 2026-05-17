# ashare_system

A 股短线盯盘、盘前计划、盘中快照、收盘复盘、持仓账本与候选股跟踪系统。

> 说明：本仓库是从 Hermes 本地自动化环境中导出的代码与技能文档集合，主要面向个人 A 股短线交易复盘/监控工作流。脚本默认使用本机路径 `/home/admin/Notes/market/ashare-monitor` 作为数据与报告目录，使用 `/home/admin/.hermes/cache` 作为行情缓存目录。

## 功能概览

| 模块 | 脚本 | 作用 |
|---|---|---|
| 盘中后台监控 | `scripts/ashare_background_monitor.py` | 采集 A 股实时快照、指数快照、板块快照、关注股快照，写入 SQLite 与当日快照文件，并生成盘中异常摘要。 |
| 盘后补采 | `scripts/ashare_postclose_capture.py` | 收盘后再次调用后台监控采集链路，补齐收盘时点行情/板块/关注股快照。 |
| 收盘摘要 | `scripts/ashare_close_summary.py` | 生成每日收盘复盘、强弱板块、主板短线候选股、风险提示与自我复盘模板。 |
| 盘后持仓/候选分析 | `scripts/ashare_position_watch_analysis.py` | 分析持仓股与候选股的趋势、通道、支撑/压力、盈亏比、明日策略。 |
| 盘前简报 | `scripts/ashare_opening_brief.py` | 生成 09:00 盘前简报，整合前一日复盘、候选股、新闻与 TrendRadar 输出。 |
| 09:26 开盘操作表 | `scripts/ashare_opening_action_table.py` | 根据竞价、开盘、大盘、板块和持仓/候选股生成可执行买卖计划。 |
| 持仓账本 | `scripts/ashare_ledger_lib.py`, `scripts/ashare_ledger_cli.py`, `scripts/ashare_ledger_daily_report.py` | 记录每日买卖、计算持仓、生成 15:05 持仓盈亏表。 |
| 策略评分/跟踪 | `scripts/ashare_strategy_engine.py`, `scripts/ashare_strategy_tracker.py` | 候选股分层、跟踪入选后表现、维护 `strategy_scoreboard.db`。 |
| 短线增强数据层 | `scripts/ashare_shortline_schema.py` | 创建独立 `shortline/shortline_signal.db` schema，用于后续涨停生态、题材、情绪锚点、新高、事件、龙虎榜等 shadow 数据；不抓取真实数据、不接入生产报告。 |
| 数据工具 | `scripts/ashare_data_utils.py` | 行情源降级、缓存、日期校验、代码标准化等公共工具。 |

## 目录结构

```text
ashare_system/
├── README.md
├── .gitignore
├── scripts/
│   ├── ashare_background_monitor.py
│   ├── ashare_close_summary.py
│   ├── ashare_data_utils.py
│   ├── ashare_ledger_cli.py
│   ├── ashare_ledger_daily_report.py
│   ├── ashare_ledger_lib.py
│   ├── ashare_opening_action_table.py
│   ├── ashare_opening_brief.py
│   ├── ashare_position_watch_analysis.py
│   ├── ashare_postclose_capture.py
│   ├── ashare_strategy_engine.py
│   ├── ashare_strategy_tracker.py
│   └── ashare_shortline_schema.py
├── tests/
│   └── test_*.py
└── skills/
    ├── finance/ashare-finance-workflows/
    └── archive-finance/
```

## 运行环境

### Python

建议 Python 3.11+。当前导出环境中使用过 Python 3.12。

### Python 依赖

脚本主要依赖：

```bash
pip install akshare pandas requests numpy easyquotation pytdx tushare pytest
```

可选依赖/外部数据：

- `TrendRadar` 本地输出：默认读取 `/home/admin/Notes/market/trendradar-output`
- TuShare：若使用 TuShare 相关降级数据源，需要配置 `TUSHARE_TOKEN`
- Hermes/Feishu Cron：生产环境中由 Hermes cron 负责定时执行和推送，本仓库只保存脚本与技能文档，不包含 token、webhook、生产数据库。

## 默认数据路径

脚本中默认根目录：

```text
/home/admin/Notes/market/ashare-monitor
```

典型输出结构：

```text
/home/admin/Notes/market/ashare-monitor/
├── ashare_monitor.db
├── ledger/
│   └── ashare_ledger.db
├── strategy/
│   └── strategy_scoreboard.db
├── shortline/
│   └── shortline_signal.db
└── YYYY-MM-DD/
    ├── snapshots.jsonl
    ├── latest-summary.json
    ├── close-summary.md
    ├── close-summary-context.json
    ├── position-watch-analysis.md
    ├── position-watch-analysis-context.json
    ├── opening-brief.md
    ├── opening-brief-context.json
    ├── opening-action-table-0926.md
    ├── opening-action-table-0926-context.json
    ├── holding-pnl-1505.md
    └── holding-pnl-1505-context.json
```

缓存默认路径：

```text
/home/admin/.hermes/cache/ashare-data-utils
/home/admin/.hermes/cache/ashare-opening-brief
```

其中 `ASHARE_CACHE_ROOT` 可覆盖 `ashare_data_utils.py` 的行情缓存根目录：

```bash
export ASHARE_CACHE_ROOT=/tmp/ashare-cache
```

## 数据库说明

### `ashare_monitor.db`

由 `ashare_background_monitor.py` 与相关报告脚本读取/写入，保存盘中采集快照。核心表包括：

| 表 | 内容 |
|---|---|
| `capture_runs` | 每次采集运行的元信息：`run_id`、`trade_date`、`captured_at`、涨跌家数、数据源等。 |
| `stock_snapshots` | 个股实时快照：代码、名称、最新价、涨跌幅、成交额、换手率、量比、市值等。 |
| `index_snapshots` | 指数快照：上证、深成指、创业板、科创50、沪深300、中证1000 等。 |
| `sector_snapshots` | 板块快照：板块名称、涨跌幅、领涨股、成交额等。 |
| `sector_constituent_snapshots` | 板块成分快照：板块与个股映射、个股在板块中的表现。 |
| `watchlist_snapshots` | 持仓/关注名单快照。 |

### `ledger/ashare_ledger.db`

由持仓账本模块维护，保存人工或自然语言录入的买卖记录、持仓快照与每日盈亏报告。

### `strategy/strategy_scoreboard.db`

由策略评分/候选股跟踪模块维护，记录候选股入选、分层、盈亏比、后续 1/3/5/20 日表现等。

### `shortline/shortline_signal.db`

由 `ashare_shortline_schema.py` 初始化的短线增强 shadow 数据库。阶段 1 只创建 schema，不抓取真实数据、不生成报告、不接入 Cron/飞书、不修改 `ashare_monitor.db` 或 `strategy_scoreboard.db`。

当前规划表包括：

| 表 | 内容 |
|---|---|
| `limitup_daily` | 涨停生态：涨停时间、开板次数、封单、连板、炸板/回封标记、原因与原始数据。 |
| `theme_daily` | 题材日度状态：题材分数、涨停/炸板数量、龙头/中军/负反馈锚点。 |
| `theme_stock_map` | 题材与股票映射：角色、证据、置信度与来源。 |
| `emotion_anchors` | 情绪锚点：空间板、核心龙头、趋势中军、负反馈等。 |
| `new_high_daily` | 新高数据：20/60/100 日相对位置、题材/板块归属。 |
| `event_calendar` | 事件日历：公告、业绩、政策、会议等事件与预期影响。 |
| `lhb_daily` | 龙虎榜摘要：净买入、机构净买、席位 JSON、游资/量化标记与解读。 |

初始化与查看：

```bash
python scripts/ashare_shortline_schema.py init
python scripts/ashare_shortline_schema.py show-tables
```

可通过 `--db-path /tmp/shortline_signal.db` 指向临时库，便于测试与回滚。

## 报告说明

| 报告文件 | 生成脚本 | 内容 |
|---|---|---|
| `latest-summary.json` | `ashare_background_monitor.py` | 最新盘中快照摘要，供后续盘前/盘后报告读取。 |
| `snapshots.jsonl` | `ashare_background_monitor.py` | 盘中每次采集的 JSON Lines 历史记录。 |
| `close-summary.md` | `ashare_close_summary.py` | 收盘复盘：指数、板块、异常、候选股、风控、自我复盘区。 |
| `close-summary-context.json` | `ashare_close_summary.py` | 收盘摘要结构化上下文。 |
| `position-watch-analysis.md` | `ashare_position_watch_analysis.py` | 持仓股/候选股走势、支撑压力、盈亏比、明日策略。 |
| `opening-brief.md` | `ashare_opening_brief.py` | 09:00 盘前市场环境、新闻、候选股和执行计划。 |
| `opening-action-table-0926.md` | `ashare_opening_action_table.py` | 09:26 开盘操作表，给出具体买卖点/仓位/观察条件。 |
| `holding-pnl-1505.md` | `ashare_ledger_daily_report.py` | 15:05 持仓盈亏表。 |

## 基本用法

进入仓库并设置模块路径：

```bash
cd /home/admin/ashare_system
export PYTHONPATH=/home/admin/ashare_system/scripts:$PYTHONPATH
```

### 1. 盘中采集/监控

```bash
python scripts/ashare_background_monitor.py
```

该脚本会：

1. 判断是否为 A 股交易日；
2. 获取实时 A 股行情、指数、板块、关注股；
3. 写入 `ashare_monitor.db`；
4. 写入当日 `snapshots.jsonl` 与 `latest-summary.json`；
5. 输出 JSON 摘要到 stdout。

### 2. 收盘后补采

```bash
python scripts/ashare_postclose_capture.py
```

用于收盘后补齐收盘时点快照。注意：该脚本当前通过固定路径导入生产环境中的 `ashare_background_monitor.py`：

```text
/home/admin/.hermes/scripts/ashare_background_monitor.py
```

若要在独立环境运行，需要先调整该路径或保持同样目录结构。

### 3. 生成收盘摘要

```bash
python scripts/ashare_close_summary.py
```

输出：

```text
YYYY-MM-DD/close-summary.md
YYYY-MM-DD/close-summary-context.json
```

### 4. 生成盘后持仓/候选分析

```bash
python scripts/ashare_position_watch_analysis.py
```

输出：

```text
YYYY-MM-DD/position-watch-analysis.md
YYYY-MM-DD/position-watch-analysis-context.json
```

### 5. 生成盘前简报

```bash
python scripts/ashare_opening_brief.py
```

输出：

```text
YYYY-MM-DD/opening-brief.md
YYYY-MM-DD/opening-brief-context.json
```

### 6. 生成 09:26 开盘操作表

```bash
python scripts/ashare_opening_action_table.py
```

输出：

```text
YYYY-MM-DD/opening-action-table-0926.md
YYYY-MM-DD/opening-action-table-0926-context.json
```

### 7. 持仓账本 CLI

初始化账本数据库：

```bash
python scripts/ashare_ledger_cli.py init
```

录入买入：

```bash
python scripts/ashare_ledger_cli.py add-trade \
  --trade-date 2026-05-05 \
  --trade-time 10:15:00 \
  --symbol 600000 \
  --name 浦发银行 \
  --side buy \
  --quantity 100 \
  --price 10.50 \
  --fees 1.00 \
  --note "示例买入"
```

录入卖出：

```bash
python scripts/ashare_ledger_cli.py add-trade \
  --trade-date 2026-05-05 \
  --trade-time 14:20:00 \
  --symbol 600000 \
  --name 浦发银行 \
  --side sell \
  --quantity 100 \
  --price 10.80 \
  --fees 1.00
```

查看持仓：

```bash
python scripts/ashare_ledger_cli.py show-positions --trade-date 2026-05-05
```

生成持仓盈亏报告：

```bash
python scripts/ashare_ledger_cli.py report --trade-date 2026-05-05
python scripts/ashare_ledger_daily_report.py
```

自然语言解析交易文本：

```bash
python scripts/ashare_ledger_cli.py parse-text \
  --text "今天 10:15 买入 浦发银行 100股 10.50元 手续费1元" \
  --trade-date 2026-05-05
```

### 8. 策略跟踪维护

```bash
python scripts/ashare_strategy_tracker.py
```

该脚本维护候选股后续表现跟踪，写入：

```text
strategy/strategy_scoreboard.db
```

## 推荐自动化执行节奏

生产环境可通过 Hermes cron、系统 cron 或其他调度器执行。推荐节奏如下：

| 时间 | 任务 | 脚本 |
|---|---|---|
| 09:00 | 盘前简报 | `ashare_opening_brief.py` |
| 09:26 | 开盘操作表 | `ashare_opening_action_table.py` |
| 盘中每 1 分钟 | 静默盘中采集 | `ashare_background_monitor.py` |
| 15:05 | 持仓盈亏表 | `ashare_ledger_daily_report.py` |
| 15:10-15:30 | 盘后补采 | `ashare_postclose_capture.py` |
| 17:30 | 收盘摘要 | `ashare_close_summary.py` |
| 17:40 | 持仓/候选分析 | `ashare_position_watch_analysis.py` |
| 盘后 | 候选股跟踪维护 | `ashare_strategy_tracker.py` |

## Cron 自动化任务配置

本系统通过 Hermes cron 实现全自动化运行，所有任务均设定为交易日元重复执行。以下是当前已配置的任务清单：

| Job ID | 任务名称 | 调度规则 | 交付方式 | 关联脚本 |
|---|---|---|---|---|
| `148743328343` | ashare-close-summary-feishu | `0 17 * * 1-5`（每个交易日 17:00） | 飞书 DM | `ashare_close_summary.py` |
| `5ec9dd7c6fdb` | ashare-position-watch-analysis | `30 17 * * 1-5`（每个交易日 17:30） | 飞书 DM | `ashare_position_watch_analysis.py` |
| `d90ce37282ec` | ashare-opening-brief-feishu | `0 9 * * 1-5`（每个交易日 09:00） | 飞书 DM | `ashare_opening_brief.py` |
| `6c91e3fd8797` | ashare-opening-action-table | `26 9 * * 1-5`（每个交易日 09:26） | 飞书 DM | `ashare_opening_action_table.py` |
| `431e87e2c088` | ashare-strategy-tracker-local | `50 16 * * 1-5`（每个交易日 16:50） | 本地落盘 | `ashare_strategy_tracker.py` |
| `2b38f8b7f143` | ashare-sector-multi-day | `5 17 * * 1-5`（每个交易日 17:05） | 本地落盘 | `ashare_sector_multi_day.py` |
| `3a248b37c2db` | ashare-ledger-daily-pnl-feishu | `5 15 * * 1-5`（每个交易日 15:05） | 飞书 DM | `ashare_ledger_daily_report.py` |

### Cron 任务详情

**盘中静默采集（无 agent 交互，no_agent 模式）：**

系统盘中每分钟通过后台脚本持续采集行情数据，不向飞书推送任何消息，仅写入本地数据库和快照文件：

```text
*/1 9-15 * * 1-5  /home/admin/.hermes/hermes-agent/venv/bin/python3 /home/admin/.hermes/scripts/ashare_background_monitor.py
```

> 注意：盘中静默采集由系统 cron 或 Hermes 后台调度执行，不在本仓库的 Hermes cron 管理范围内。

**各任务职责说明：**

- **ashare-opening-brief-feishu（09:00）**：整合 TrendRadar 输出、前日复盘、新闻与候选股，生成盘前简报，明确当天市场环境与操作意向（买入/卖出/空仓等）及建议仓位。
- **ashare-opening-action-table（09:26）**：读取持仓/候选股竞价与开盘数据、大盘与板块情况，给出具体买卖点、仓位比例、分步动作（开盘前 5 分钟/开盘后 15 分钟）。
- **ashare-ledger-daily-pnl-feishu（15:05）**：生成当日持仓盈亏表，通过飞书发送；由用户通过飞书输入当日操作（买入/卖出/止损），系统自动入账。
- **ashare-sector-multi-day（17:05）**：本地多日板块联动分析，写入本地文件，不推飞书。
- **ashare-strategy-tracker-local（16:50）**：候选股跟踪维护，评估入选后表现，写入 `strategy_scoreboard.db`。
- **ashare-close-summary-feishu（17:00）**：生成收盘复盘摘要（指数、板块梯队、候选股、风控），通过飞书发送。
- **ashare-position-watch-analysis（17:30）**：持仓股/候选股的今日走势分析、通道判断、支撑/压力位、盈亏比与明日策略，通过飞书发送。

### 管理命令

```bash
# 查看所有任务
hermes cron list

# 运行指定任务（手动触发）
hermes cron run <job_id>

# 暂停/恢复任务
hermes cron pause <job_id>
hermes cron resume <job_id>

# 删除任务
hermes cron remove <job_id>
```

## 测试

运行全部测试：

```bash
cd /home/admin/ashare_system
export PYTHONPATH=/home/admin/ashare_system/scripts:$PYTHONPATH
python -m pytest tests -q
```

单项测试：

```bash
python -m pytest tests/test_background_monitor_db_date_filter.py -q
python -m pytest tests/test_opening_action_db_date_filter.py -q
python -m pytest tests/test_opening_action_table.py -q
python -m pytest tests/test_opening_action_window.py -q
python -m pytest tests/test_opening_report_metadata.py -q
python -m pytest tests/test_close_summary_db_date_filter.py -q
python -m pytest tests/test_close_summary_report_metadata.py -q
python -m pytest tests/test_data_asof_validation.py -q
python -m pytest tests/test_position_watch_analysis_strict_today.py -q
python -m pytest tests/test_position_watch_report_metadata.py -q
python -m pytest tests/test_opening_brief_report_metadata.py -q
```

语法检查：

```bash
python -m py_compile \
  scripts/ashare_background_monitor.py \
  scripts/ashare_opening_action_table.py \
  scripts/ashare_close_summary.py \
  scripts/ashare_opening_brief.py \
  scripts/ashare_position_watch_analysis.py
```

## 时间穿越/日期过滤规则

本系统中的报告链路对数据日期有严格要求：

1. 盘中快照查询必须限定 `trade_date`；
2. 盘前/盘中报告读取快照时必须限定 `captured_at <= asof_time`；
3. 不允许在无合规数据时静默 fallback 到前一交易日或全库 latest；
4. 无合规数据时应返回空结构，由下游报告显式展示数据缺失/空板块上下文；
5. 候选股和持仓盘后分析必须使用当天数据，主数据源失败时可以切换当天备用源，但不得回退到前一天价格。

相关测试集中在：

- `test_background_monitor_db_date_filter.py`
- `test_opening_action_db_date_filter.py`
- `test_close_summary_db_date_filter.py`
- `test_data_asof_validation.py`
- `test_position_watch_analysis_strict_today.py`

## 使用注意事项

1. **路径是本地化的**：脚本默认写入 `/home/admin/Notes/market/ashare-monitor`，迁移到其他机器前需要调整脚本中的 `ROOT`、`DB_PATH`、`TRENDRADAR_ROOT` 等常量。
2. **不要提交生产数据**：`.db`、`.env`、日志、缓存、报告输出和备份文件应保持在 `.gitignore` 中。
3. **行情源可能限流**：AkShare、东方财富、雪球、pytdx、TuShare 等数据源可能失败或超时，脚本中包含部分降级和缓存逻辑。
4. **报告不是投资建议**：输出仅供个人复盘和交易计划辅助，不构成投资建议。
5. **主板短线偏好**：当前策略主要面向主板短线交易；科创板、创业板、北交所个股通常会被排除，ETF/LOF 可纳入持仓/候选分析。

## Skills 文档

`skills/` 目录保存了与该系统相关的 Hermes skills，包括：

- `skills/finance/ashare-finance-workflows/`：A 股金融工作流总控 skill；
- `skills/archive-finance/ashare-monitor-cron/`：盘中监控与收盘摘要自动化；
- `skills/archive-finance/ashare-opening-brief/`：盘前简报；
- `skills/archive-finance/ashare-opening-action-table-0926/`：09:26 开盘操作表；
- `skills/archive-finance/ashare-position-watch-obsidian/`：盘后持仓/候选分析；
- `skills/archive-finance/shortline-mainboard-workflow/`：主板短线交易体系；
- `skills/archive-finance/akshare-*`：AkShare 数据获取与降级。

这些文档记录了系统设计、验收规则、时间穿越修复、报告模板和运维注意事项。

## 安全与隐私

本仓库不应包含：

- GitHub token；
- Feishu/Lark app secret、webhook token；
- TuShare token；
- 生产 SQLite 数据库；
- 个人交易流水原始数据；
- 未脱敏日志或聊天记录。

推送前可执行基础扫描：

```bash
grep -RInE "(GITHUB_TOKEN|APP_SECRET|APP_ID|SECRET|TOKEN|PASSWORD|api[_-]?key|Authorization:|Bearer )" . \
  --exclude-dir=.git \
  --exclude='README.md'
```

若发现真实凭据，请先删除并轮换密钥。