# A 股短线系统升级设计：市场环境 + 涨停生态 + 题材主线 + 情绪锚点 + 候选执行 + 策略验证

> 阶段 0 产物。本文只做代码审计与升级设计，不修改生产脚本、不修改生产 Cron、不修改生产数据库结构、不触发飞书推送。

## 1. 目标与边界

### 1.1 长期目标

把当前系统从“主板低位趋势候选 + 盘前/盘中/盘后复盘系统”，逐步升级为更接近成熟 A 股短线交易复盘框架的系统：

1. 市场环境：指数、量能、涨跌家数、涨跌停、连板高度、亏钱效应。
2. 涨停生态：涨停板、首板、连板、炸板、回封、封单、涨停时间、最大封板资金。
3. 题材主线：题材/行业/概念、股票池、龙头、中军、补涨、跟风、负反馈。
4. 情绪锚点：空间板、核心龙头、趋势中军、同题材大面、跌停/大跌反馈。
5. 候选执行：候选股分层、仓位、买点、二次加仓条件、放弃条件、止损条件。
6. 策略验证：候选入选后 1/3/5/20 日收益、最大回撤、胜率、分层有效性。

### 1.2 阶段 0 边界

本阶段只新增本文档：

```text
/home/admin/ashare_system/skills/finance/ashare-finance-workflows/references/shortline-system-upgrade-plan.md
```

明确不做：

- 不修改 `/home/admin/.hermes/scripts/` 生产脚本。
- 不修改 `/home/admin/ashare_system/scripts/` 任何生产逻辑脚本。
- 不修改生产 Cron。
- 不修改飞书推送逻辑。
- 不修改 `/home/admin/Notes/market/ashare-monitor/*.db` 生产数据库。
- 不新增生产数据库表。
- 不输出确定性投资建议。

## 2. 当前系统能力地图

### 2.1 代码与数据路径

仓库路径：

```text
/home/admin/ashare_system
```

生产数据只读审计路径：

```text
/home/admin/Notes/market/ashare-monitor
```

主要生产输出结构：

```text
/home/admin/Notes/market/ashare-monitor/
├── ashare_monitor.db
├── ledger/ashare_ledger.db
├── strategy/strategy_scoreboard.db
└── YYYY-MM-DD/
    ├── snapshots.jsonl
    ├── latest-summary.json
    ├── close-summary.md
    ├── close-summary-context.json
    ├── opening-brief.md
    ├── opening-brief-context.json
    ├── opening-action-table-0926.md
    ├── opening-action-table-0926-context.json
    ├── position-watch-analysis.md
    ├── position-watch-analysis-context.json
    ├── holding-pnl-1505.md
    └── holding-pnl-1505-context.json
```

### 2.2 哪些脚本写 `ashare_monitor.db`

| 脚本 | 写入内容 | 审计结论 |
|---|---|---|
| `scripts/ashare_background_monitor.py` | `capture_runs`、`stock_snapshots`、`index_snapshots`、`sector_snapshots`、`watchlist_snapshots`、`sector_constituent_snapshots` | 主写入脚本；同时写 `snapshots.jsonl` 与 `latest-summary.json`。 |
| `scripts/ashare_postclose_capture.py` | 通过导入 `ashare_background_monitor.py` 调用同一套 `insert_db()` 写入上述表 | 盘后补采写入；当前导入路径指向生产 `.hermes/scripts`，迁移时需要注意。 |

当前 `ashare_monitor.db` 生产库只读审计到的表：

| 表 | 现有行数（审计时） | 作用 |
|---|---:|---|
| `capture_runs` | 279 | 每次采集运行元信息。 |
| `stock_snapshots` | 1,218,831 | 个股快照。 |
| `index_snapshots` | 911 | 指数快照。 |
| `sector_snapshots` | 632 | 行业/板块快照。 |
| `sector_constituent_snapshots` | 103,794 | 板块成分股快照。 |
| `watchlist_snapshots` | 552 | 持仓/候选关注股快照。 |

### 2.3 哪些脚本读 `ashare_monitor.db`

| 脚本 | 读取内容 | 当前用途 |
|---|---|---|
| `ashare_close_summary.py` | `capture_runs`、`index_snapshots`、`sector_snapshots`、`sector_constituent_snapshots`、`watchlist_snapshots`、`stock_snapshots` | 收盘摘要：市场总览、板块分析、候选股、盘中关注股、涨跌停 fallback。 |
| `ashare_opening_brief.py` | `sector_snapshots`、`sector_constituent_snapshots`，并读取前一日复盘/TrendRadar | 盘前简报：前一日板块上下文、新闻、候选池、今日计划。 |
| `ashare_opening_action_table.py` | `sector_snapshots`，并读取盘前简报/收盘摘要/账本 | 09:26 开盘操作表：强板块、持仓、候选股执行模板。 |
| `ashare_position_watch_analysis.py` | `capture_runs`、`sector_snapshots`、`sector_constituent_snapshots` | 盘后持仓与候选分析：补充板块/角色/阶段上下文。 |
| `ashare_background_monitor.py` | `sector_snapshots`、`sector_constituent_snapshots` | 不只是写入脚本，也会读取近期板块成分缓存来推断板块上下文。 |

### 2.4 哪些脚本生成核心报告

| 报告 | 生成脚本 | 输出 |
|---|---|---|
| 盘中最新快照 | `ashare_background_monitor.py` | `YYYY-MM-DD/latest-summary.json`、`snapshots.jsonl`、stdout JSON。 |
| 盘后补采快照 | `ashare_postclose_capture.py` | 同上，状态为 `captured_postclose`。 |
| 收盘摘要 | `ashare_close_summary.py` | `close-summary.md`、`close-summary-context.json`。 |
| 盘后持仓/候选分析 | `ashare_position_watch_analysis.py` | `position-watch-analysis.md`、context JSON。 |
| 盘前简报 | `ashare_opening_brief.py` | `opening-brief.md`、`opening-brief-context.json`。 |
| 09:26 开盘操作表 | `ashare_opening_action_table.py` | `opening-action-table-0926.md`、context JSON。 |
| 15:05 持仓盈亏表 | `ashare_ledger_daily_report.py` + `ashare_ledger_lib.py` | `holding-pnl-1505.md`、context JSON、账本 DB。 |

### 2.5 `strategy_scoreboard.db` 当前记录了什么

当前由以下脚本维护：

- `ashare_strategy_engine.py`
- `ashare_strategy_tracker.py`
- `ashare_close_summary.py` 中候选池保存/评分逻辑会调用 strategy engine。

当前表：

| 表 | 当前作用 |
|---|---|
| `candidate_tracking` | 候选股入选日、代码、名称、板块、阶段、角色、A/B/C 分层、盈亏比、收盘价、计划买入区间、后续 1/3/5 日收盘/高低点/收益、20 日最佳/最差表现、状态、元数据。 |
| `candidate_tracking_daily` | 候选股入选后按 horizon day 记录价格交易日、收盘价、高低价、收盘收益、最佳/最差收益。 |

现有能力价值：已经具备“候选 -> 后续表现”的基础闭环，但尚未把“涨停生态 / 题材主线 / 情绪环境”作为候选入选和策略验证的结构化特征写入。

## 3. 当前已有但未充分使用的能力

### 3.1 涨停池

已有能力：

- `ashare_close_summary.py:get_limit_stats()` 已调用：
  - `ak.stock_zt_pool_em(date=...)`
  - `ak.stock_zt_pool_dtgc_em(date=...)`
  - `ak.stock_zt_pool_strong_em(date=...)`
  - `ak.stock_zt_pool_zbgc_em(date=...)`
- 报告中已使用：涨停数量、跌停数量、最高连板、市场环境硬规则。
- `ashare_strategy_engine.py:classify_market_hard()` 已把 `zt_count`、`dt_count`、`max_lb` 纳入市场环境评分。

不足：

- 涨停明细没有入库。
- 首板/连板/空间板没有结构化沉淀。
- 涨停时间、封单、封板资金、换手、流通市值等字段没有统一归一化。
- 没有按题材统计涨停强度。
- 没有和候选股入选、次日表现做关联验证。

### 3.2 炸板池 / 回封 / 负反馈

已有能力：

- `ashare_close_summary.py:get_limit_stats()` 已尝试读取 `stock_zt_pool_zbgc_em`，但主要作为 dataframe 暂存在 `limit_stats`。

不足：

- 未结构化保存炸板/曾涨停数据。
- 未区分“炸板未回封”“炸板回封”“午后炸板”“尾盘炸板”。
- 未统计同题材炸板率与负反馈。
- 未纳入情绪锚点或候选降权。

### 3.3 板块成分

已有能力：

- `ashare_background_monitor.py` 已写 `sector_snapshots` 与 `sector_constituent_snapshots`。
- `ashare_close_summary.py:get_sector_constituents()` 可抓取板块成分。
- 多个报告可从 DB 或复盘文本中解析 `sector -> code -> role/stage`。

不足：

- 当前“板块”更偏行业板块，尚未形成“题材/概念主线图谱”。
- 龙头/中军/补涨角色主要由成交额、涨幅或文本规则推断，缺少涨停生态、连板高度、题材地位加权。
- 缺少“题材内部负反馈”：同题材大跌、炸板、跌停、核心断板等。

### 3.4 龙虎榜

已有能力：

- 当前仓库脚本未发现正式龙虎榜采集/入库/报告链路。
- `ashare-finance-workflows` 总控 skill 把龙虎榜列为金融投研可扩展方向。

不足：

- 无 `lhrb/lhb/dragon tiger` 数据结构。
- 无机构/游资/量化席位分类。
- 无净买入、次日反馈、同题材影响分析。

### 3.5 公告 / 新闻 / 事件

已有能力：

- `ashare_opening_brief.py` 已读取 Google News RSS。
- `ashare_opening_brief.py` 已读取本地 TrendRadar `news_items` / `rss_items` SQLite。
- `ashare_opening_brief.py:collect_notice_map()` 已具备公告查询雏形，使用 `ak.stock_notice_report`。
- 新闻已粗分宏观、A 股、港股、TrendRadar 等来源，并做关键词匹配。

不足：

- 新闻/公告未入结构化 DB。
- 缺少事件日历：业绩、解禁、会议、政策窗口、重要海外数据。
- 新闻与题材、个股的映射主要是关键词，缺少可追踪的事件 ID 与影响评分。
- 缺少“新闻催化 -> 题材加强/回流/退潮 -> 候选表现”的验证闭环。

### 3.6 候选跟踪

已有能力：

- `strategy_scoreboard.db` 已记录候选分层、入选日、板块、角色、盈亏比、后续收益。
- `ashare_strategy_tracker.py` 可做后台维护。

不足：

- 候选记录未包含入选时的涨停生态特征：当日涨停家数、题材涨停数、候选是否同题材空间板/中军/补涨。
- 未包含情绪锚点特征：空间板高度、亏钱效应、炸板率、跌停数、核心题材负反馈。
- 未能回答“哪类市场环境 + 哪类题材阶段 + 哪类角色”的候选更有效。

## 4. 缺失能力地图

| 能力 | 当前状态 | 缺口 | 建议承载层 |
|---|---|---|---|
| 涨停明细 | 仅临时 dataframe / 报告摘要 | 未入库、未按题材统计、未跟踪回封与封单 | 新增 sidecar 脚本 + shadow DB。 |
| 连板生态 | 只取 `max_lb` | 无空间板列表、断板、晋级率 | `limit_up_daily` + `emotion_anchors_daily`。 |
| 炸板/回封 | 有 `zbgc_df` 但未使用充分 | 无炸板率、同题材负反馈 | `limit_break_daily`。 |
| 题材图谱 | 行业板块成分已有 | 缺概念/题材层、题材强度、角色归因 | `theme_daily` + `theme_stock_daily`。 |
| 情绪锚点 | 市场环境硬规则 | 缺空间板、核心龙头、亏钱效应、同题材大面 | `emotion_anchors_daily`。 |
| 主线状态 | 板块阶段：主升/修复/轮动/退潮 | 缺“加强/分歧/回流/退潮/高低切”的多日状态机 | `theme_state_daily`。 |
| 板块资金连续性 | 有当日行业资金流 | 缺 3/5/10 日连续性 | `sector_flow_daily`。 |
| 龙虎榜 | 暂无正式链路 | 缺席位、净买入、次日反馈 | `lhb_daily` + `lhb_seat_daily`。 |
| 百日新高 | 暂无正式链路 | 缺新高共振/趋势强度 | `new_high_daily`。 |
| 事件日历 | 新闻/公告散落在盘前脚本 | 缺事件表与题材映射 | `event_calendar` + `event_stock_map`。 |
| 策略验证 | 候选后续收益已有 | 缺环境/题材/情绪特征关联 | 扩展 shadow scoreboard 或新增 feature snapshot。 |

## 5. 分阶段升级路线

> 原则：每个阶段只做一个可回滚、可单独运行、可测试的小闭环。所有新能力先进入 shadow mode，不接管生产 Cron，不覆盖生产报告。

### 阶段 0：代码审计与升级设计（本阶段）

- 新增本文档。
- 不改生产逻辑。
- 不改 DB。
- 不跑会写生产库的脚本。

### 阶段 1：新增 shadow 数据库与只读 legacy reader

目标：建立旁路数据底座，不影响生产。

建议新增：

- `scripts/ashare_shadow_store.py`
- `scripts/ashare_legacy_readers.py`
- `tests/test_shadow_store_schema.py`
- `tests/test_legacy_readers_readonly.py`

输出：

```text
/home/admin/Notes/market/ashare-monitor-shadow/ashare_shadow.db
```

只读输入：

- `ashare_monitor.db`
- `strategy_scoreboard.db`
- 当日/近几日 Markdown 报告。

不做：

- 不采集实时数据。
- 不生成交易建议。
- 不修改生产 DB。

### 阶段 2：涨停生态 sidecar 采集与归一化

目标：将涨停、炸板、强势股等数据结构化保存到 shadow DB。

建议新增：

- `scripts/ashare_limit_ecology_shadow.py`
- `tests/test_limit_ecology_normalize.py`
- `tests/test_limit_ecology_shadow_db.py`

数据源：

- `ak.stock_zt_pool_em`
- `ak.stock_zt_pool_dtgc_em`
- `ak.stock_zt_pool_strong_em`
- `ak.stock_zt_pool_zbgc_em`

产物：

- `limit_up_daily`
- `limit_break_daily`
- `market_emotion_daily`

验收：

- 可指定 `--trade-date YYYY-MM-DD`。
- 可指定 `--db-path /tmp/...` 运行测试。
- 默认写 shadow DB，不写生产 DB。
- 数据源失败时输出 warnings，不中断后续阶段。

### 阶段 3：题材/主线图谱 v0

目标：用行业板块 + 涨停池 + 现有板块成分构造可解释题材图谱 v0。

建议新增：

- `scripts/ashare_theme_graph_shadow.py`
- `tests/test_theme_graph_shadow.py`

输入：

- `sector_snapshots`
- `sector_constituent_snapshots`
- `limit_up_daily`
- `limit_break_daily`
- `opening/close context JSON`

输出：

- `theme_daily`
- `theme_stock_daily`
- `theme_state_daily`

阶段限制：

- v0 不追求完美题材命名；先以“行业/概念近似题材”跑通。
- 明确标注“题材映射置信度”。

### 阶段 4：情绪锚点 v0

目标：沉淀空间板、核心龙头、趋势中军、亏钱效应、同题材大面。

建议新增：

- `scripts/ashare_emotion_anchors_shadow.py`
- `tests/test_emotion_anchors_shadow.py`

输出：

- `emotion_anchors_daily`
- `negative_feedback_daily`

报告字段：

- 空间板及所属题材。
- 核心龙头是否晋级/断板/炸板。
- 跌停数、大跌数、炸板率。
- 同题材大面股票列表。

### 阶段 5：shadow 收盘复盘 v0

目标：不改生产 `close-summary.md`，生成旁路新版复盘。

建议新增：

- `scripts/ashare_close_summary_shadow_v2.py`
- `tests/test_close_summary_shadow_v2_sections.py`

输出：

```text
/home/admin/Notes/market/ashare-monitor-shadow/YYYY-MM-DD/close-summary-v2-shadow.md
/home/admin/Notes/market/ashare-monitor-shadow/YYYY-MM-DD/close-summary-v2-shadow-context.json
```

必备章节：

1. 市场环境。
2. 涨停生态。
3. 主线题材。
4. 情绪锚点。
5. 候选执行复盘。
6. 风险提示。
7. 明日观察计划。

### 阶段 6：候选执行与策略验证增强

目标：把候选入选时的市场/题材/情绪特征保存为特征快照，和后续表现关联。

建议新增：

- `scripts/ashare_candidate_feature_snapshot_shadow.py`
- `scripts/ashare_strategy_validation_shadow.py`
- `tests/test_candidate_feature_snapshot.py`
- `tests/test_strategy_validation_shadow.py`

输出：

- `candidate_feature_snapshots`
- `strategy_validation_daily`

核心问题：

- A/B/C 分层在不同市场环境下是否有效？
- 主升题材 vs 轮动题材候选的次日表现差异？
- 龙头/中军/补涨不同角色的盈亏比分布？
- 涨停生态强/弱时，低位趋势候选是否应降仓？

### 阶段 7：盘前/09:26 报告 shadow 增强

目标：在不动生产报告的前提下，生成带题材/情绪上下文的盘前与开盘操作表 shadow 版本。

建议新增：

- `scripts/ashare_opening_brief_shadow_v2.py`
- `scripts/ashare_opening_action_table_shadow_v2.py`

输出：

- `opening-brief-v2-shadow.md`
- `opening-action-table-0926-v2-shadow.md`

原则：

- 只给“观察/条件/风险/放弃条件”，不输出确定性买入结论。
- 每条候选必须包含数据时间、题材上下文、失效条件。

### 阶段 8：连续 5 个交易日 shadow 对比后再评估接入生产

目标：比较生产报告与 shadow 报告的关键字段，决定是否进入生产替换设计。

建议新增：

- `scripts/ashare_shadow_diff_report.py`
- `tests/test_shadow_diff_report.py`

晋级条件：

- 连续 5 个交易日 shadow 任务成功。
- 无生产文件覆盖。
- 无生产 DB schema 修改。
- 无飞书误推送。
- 市场环境、题材、候选、风险提示差异可解释。

## 6. 新增数据库表设计草案（shadow DB）

建议新库：

```text
/home/admin/Notes/market/ashare-monitor-shadow/ashare_shadow.db
```

统一字段原则：

- 每张日表必须有 `trade_date TEXT NOT NULL`。
- 每条数据必须有 `source TEXT`、`source_updated_at TEXT` 或 `captured_at TEXT`。
- 原始行保留 `raw_json TEXT`，便于追溯。
- 不在 shadow 阶段写入生产 `ashare_monitor.db`。

### 6.1 `limit_up_daily`

```sql
CREATE TABLE IF NOT EXISTS limit_up_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    sector_name TEXT,
    theme_name TEXT,
    limit_type TEXT,              -- first_board / consecutive / strong / unknown
    consecutive_boards INTEGER,
    first_limit_time TEXT,
    last_limit_time TEXT,
    open_count INTEGER,           -- 开板次数/炸板次数，字段按数据源可用性填充
    seal_amount REAL,             -- 封单金额或封板资金
    max_seal_amount REAL,
    latest_price REAL,
    pct_change REAL,
    turnover_rate REAL,
    amount REAL,
    circulation_market_value REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);
```

### 6.2 `limit_break_daily`

```sql
CREATE TABLE IF NOT EXISTS limit_break_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    sector_name TEXT,
    theme_name TEXT,
    break_type TEXT,              -- zbgc / failed_limit / reopened / unknown
    first_limit_time TEXT,
    break_time TEXT,
    final_status TEXT,            -- sealed / failed / unknown
    open_count INTEGER,
    max_pct_change REAL,
    close_pct_change REAL,
    turnover_rate REAL,
    amount REAL,
    negative_feedback_score REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);
```

### 6.3 `market_emotion_daily`

```sql
CREATE TABLE IF NOT EXISTS market_emotion_daily (
    trade_date TEXT PRIMARY KEY,
    zt_count INTEGER,
    dt_count INTEGER,
    first_board_count INTEGER,
    consecutive_board_count INTEGER,
    max_consecutive_boards INTEGER,
    break_count INTEGER,
    break_rate REAL,
    big_loss_count INTEGER,
    up_count INTEGER,
    down_count INTEGER,
    total_amount REAL,
    emotion_phase TEXT,           -- 主升 / 修复 / 分歧 / 退潮 / 冰点 / 不确定
    confidence REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL
);
```

### 6.4 `theme_daily`

```sql
CREATE TABLE IF NOT EXISTS theme_daily (
    trade_date TEXT NOT NULL,
    theme_name TEXT NOT NULL,
    theme_source TEXT,            -- industry / concept / news_keyword / manual_map
    sector_name TEXT,
    zt_count INTEGER,
    first_board_count INTEGER,
    consecutive_board_count INTEGER,
    break_count INTEGER,
    top_board_code TEXT,
    top_board_name TEXT,
    top_board_height INTEGER,
    leader_code TEXT,
    leader_name TEXT,
    zhongjun_code TEXT,
    zhongjun_name TEXT,
    net_inflow REAL,
    pct_change REAL,
    strength_score REAL,
    state TEXT,                   -- 加强 / 分歧 / 回流 / 退潮 / 高低切 / 轮动
    confidence REAL,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, theme_name)
);
```

### 6.5 `theme_stock_daily`

```sql
CREATE TABLE IF NOT EXISTS theme_stock_daily (
    trade_date TEXT NOT NULL,
    theme_name TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    role TEXT,                    -- 龙头 / 空间板 / 中军 / 补涨 / 跟风 / 负反馈
    role_confidence REAL,
    is_limit_up INTEGER,
    is_limit_break INTEGER,
    is_big_loss INTEGER,
    consecutive_boards INTEGER,
    pct_change REAL,
    amount REAL,
    turnover_rate REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, theme_name, code)
);
```

### 6.6 `emotion_anchors_daily`

```sql
CREATE TABLE IF NOT EXISTS emotion_anchors_daily (
    trade_date TEXT NOT NULL,
    anchor_type TEXT NOT NULL,     -- space_board / core_leader / trend_zhongjun / big_loss / negative_feedback
    code TEXT NOT NULL,
    name TEXT,
    theme_name TEXT,
    sector_name TEXT,
    anchor_reason TEXT,
    board_height INTEGER,
    status TEXT,                   -- 晋级 / 断板 / 炸板 / 回封 / 大面 / 趋势保持 / 不确定
    pct_change REAL,
    amount REAL,
    risk_flag TEXT,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, anchor_type, code)
);
```

### 6.7 `sector_flow_daily`

```sql
CREATE TABLE IF NOT EXISTS sector_flow_daily (
    trade_date TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    pct_change REAL,
    net_inflow REAL,
    net_inflow_pct REAL,
    turnover_rate REAL,
    rank_today INTEGER,
    rank_3d INTEGER,
    rank_5d INTEGER,
    rank_10d INTEGER,
    inflow_3d REAL,
    inflow_5d REAL,
    inflow_10d REAL,
    continuity_score REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, sector_name)
);
```

### 6.8 `lhb_daily` 与 `lhb_seat_daily`

```sql
CREATE TABLE IF NOT EXISTS lhb_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    reason TEXT,
    buy_amount REAL,
    sell_amount REAL,
    net_buy REAL,
    institution_net_buy REAL,
    hot_money_net_buy REAL,
    quant_net_buy REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, reason)
);

CREATE TABLE IF NOT EXISTS lhb_seat_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    seat_name TEXT NOT NULL,
    side TEXT,                    -- buy / sell
    amount REAL,
    seat_type TEXT,               -- institution / hot_money / quant / unknown
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, seat_name, side)
);
```

### 6.9 `new_high_daily`

```sql
CREATE TABLE IF NOT EXISTS new_high_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    high_window INTEGER,           -- 100 / 60 / 120 等
    is_limit_up INTEGER,
    theme_name TEXT,
    sector_name TEXT,
    close_price REAL,
    high_price REAL,
    pct_change REAL,
    amount REAL,
    trend_score REAL,
    source TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, high_window)
);
```

### 6.10 `event_calendar` 与 `event_stock_map`

```sql
CREATE TABLE IF NOT EXISTS event_calendar (
    event_id TEXT PRIMARY KEY,
    trade_date TEXT,
    event_time TEXT,
    event_type TEXT,              -- notice / earnings / unlock / policy / conference / overseas_macro / commodity
    title TEXT NOT NULL,
    summary TEXT,
    related_theme TEXT,
    related_sector TEXT,
    impact_direction TEXT,         -- positive / negative / mixed / unknown
    confidence REAL,
    source TEXT,
    url TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_stock_map (
    event_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    relation_type TEXT,            -- direct / supply_chain / same_theme / competitor / unknown
    confidence REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, code)
);
```

### 6.11 `candidate_feature_snapshots`

```sql
CREATE TABLE IF NOT EXISTS candidate_feature_snapshots (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    source_report TEXT,
    tier TEXT,
    sector TEXT,
    theme_name TEXT,
    role TEXT,
    stage TEXT,
    rr REAL,
    market_emotion_phase TEXT,
    zt_count INTEGER,
    dt_count INTEGER,
    max_board_height INTEGER,
    theme_state TEXT,
    theme_zt_count INTEGER,
    theme_break_rate REAL,
    is_same_theme_as_space_board INTEGER,
    has_negative_feedback INTEGER,
    feature_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, source_report)
);
```

### 6.12 `strategy_validation_daily`

```sql
CREATE TABLE IF NOT EXISTS strategy_validation_daily (
    trade_date TEXT NOT NULL,
    strategy_key TEXT NOT NULL,    -- e.g. tier=A|theme_state=主升|role=中军
    sample_count INTEGER,
    win_rate_1d REAL,
    avg_ret_1d REAL,
    median_ret_1d REAL,
    avg_best_ret_3d REAL,
    avg_worst_ret_3d REAL,
    max_drawdown_proxy REAL,
    conclusion TEXT,               -- 只允许统计描述，不输出确定性建议
    risk_note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, strategy_key)
);
```

## 7. 每阶段涉及脚本地图

| 阶段 | 新增/读取脚本 | 是否修改生产脚本 | 主要测试 |
|---|---|---|---|
| 0 | 只新增本文档 | 否 | 文件存在、git diff 仅文档。 |
| 1 | `ashare_shadow_store.py`、`ashare_legacy_readers.py` | 否 | schema 创建到临时 DB；legacy reader 只读。 |
| 2 | `ashare_limit_ecology_shadow.py` | 否 | AkShare 返回样例归一化；写临时 shadow DB。 |
| 3 | `ashare_theme_graph_shadow.py` | 否 | 题材聚合、角色归因、置信度。 |
| 4 | `ashare_emotion_anchors_shadow.py` | 否 | 空间板、负反馈、情绪阶段。 |
| 5 | `ashare_close_summary_shadow_v2.py` | 否 | 报告章节完整、风险提示、数据日期。 |
| 6 | `ashare_candidate_feature_snapshot_shadow.py`、`ashare_strategy_validation_shadow.py` | 否 | 特征快照、统计验证、无确定性建议。 |
| 7 | `ashare_opening_brief_shadow_v2.py`、`ashare_opening_action_table_shadow_v2.py` | 否 | 执行模板只输出条件与风险，不覆盖生产报告。 |
| 8 | `ashare_shadow_diff_report.py` | 否 | 生产 vs shadow 关键字段 diff。 |

## 8. Shadow Mode 原则

### 8.1 路径隔离

所有新增产物默认写到：

```text
/home/admin/Notes/market/ashare-monitor-shadow
```

禁止 shadow 阶段写入：

```text
/home/admin/Notes/market/ashare-monitor/ashare_monitor.db
/home/admin/Notes/market/ashare-monitor/strategy/strategy_scoreboard.db
/home/admin/Notes/market/ashare-monitor/ledger/ashare_ledger.db
/home/admin/Notes/market/ashare-monitor/YYYY-MM-DD/*.md
```

### 8.2 参数化与可测试

每个新增脚本必须支持：

```bash
python scripts/<new_script>.py \
  --trade-date YYYY-MM-DD \
  --shadow-db /tmp/ashare_shadow_test.db \
  --output-dir /tmp/ashare-shadow-output
```

建议支持：

```bash
--no-live-fetch       # 只读旧 DB/fixtures，不调用外网
--fixture-dir tests/fixtures
--json                # stdout 输出结构化摘要
```

### 8.3 不触发飞书

shadow 阶段不调用 Feishu webhook，不通过 Hermes `send_message` 直接推送。需要人工查看时只输出本地文件路径。

### 8.4 不输出确定性投资建议

报告措辞使用：

- “观察条件”
- “可能风险”
- “若不满足则放弃”
- “仅用于复盘与计划，不构成买卖建议”

避免：

- “必买”
- “一定上涨”
- “确定性机会”
- “无风险”

## 9. 风险与回滚方案

### 9.1 工程风险

| 风险 | 表现 | 控制方案 | 回滚方案 |
|---|---|---|---|
| 误写生产 DB | shadow 脚本连接生产 DB 并建表/插入 | 强制 `--shadow-db`，默认 shadow 路径；legacy reader 使用只读 URI。 | 删除 shadow DB；生产 DB 不应受影响。 |
| 覆盖生产报告 | 新脚本误写 `YYYY-MM-DD/close-summary.md` | shadow 文件统一带 `-shadow` 或 `v2-shadow` 后缀。 | 删除 shadow 输出。 |
| 误触发飞书 | shadow 阶段调用推送 | 新脚本不包含推送逻辑；文档验收中检查关键词。 | 移除脚本/配置；不接入 Cron。 |
| AkShare 字段变动 | 涨停池字段名变化导致异常 | 增加字段映射层和 `raw_json`，测试覆盖常见字段缺失。 | 保留旧生产报告不受影响。 |
| 执行超时 | 盘中任务无法在窗口完成 | v0 优先只读旧 DB；实时采集单独阶段做缓存。 | 停止 shadow cron/手工运行。 |

### 9.2 投研风险

| 风险 | 表现 | 控制方案 |
|---|---|---|
| 题材映射错误 | 行业板块被误判为题材主线 | 增加 `confidence` 与 `theme_source`，低置信度只做观察。 |
| 情绪锚点误判 | 空间板/核心龙头错配 | 保留原始来源和字段，报告标注“数据源/规则推断”。 |
| 过度拟合 | 少量样本得出确定结论 | 策略验证只输出统计描述、样本数、风险提示。 |
| 候选执行过度激进 | 报告引导每天出手 | 明确市场环境与最大仓位上限，允许空仓/等待。 |

## 10. 验收标准

阶段 0 验收：

- [x] 已阅读 README、scripts 目录、finance workflow 相关文档。
- [x] 已梳理 `ashare_monitor.db` 写入脚本。
- [x] 已梳理 `ashare_monitor.db` 读取脚本。
- [x] 已梳理核心报告生成链路。
- [x] 已梳理 `strategy_scoreboard.db` 当前记录内容。
- [x] 已识别已有但未充分使用能力：涨停池、炸板池、板块成分、龙虎榜、公告/新闻/事件、候选跟踪。
- [x] 已提出分阶段升级路线。
- [x] 已提出新增 shadow DB 表设计草案。
- [x] 已写明风险与回滚方案。
- [x] 已坚持不修改生产 Cron 的 shadow mode 原则。

## 11. 下一阶段建议

建议下一阶段不是直接做涨停生态，而是先执行 **阶段 1：新增 shadow 数据库与只读 legacy reader**。

原因：

1. 先建立可回滚的 shadow DB 与只读 reader，可以避免后续阶段误写生产库。
2. 后续涨停生态、题材图谱、情绪锚点都需要统一 shadow schema。
3. 先做 reader 测试，可以固定“只读旧数据、写旁路输出”的工程边界。

阶段 1 最小任务：

1. 新增 `scripts/ashare_shadow_store.py`：只负责创建 shadow DB schema，不读生产数据。
2. 新增 `scripts/ashare_legacy_readers.py`：只读 `ashare_monitor.db` / `strategy_scoreboard.db`，使用 SQLite read-only URI。
3. 新增测试：临时 DB schema 创建、legacy reader 不写生产 DB、日期过滤正确。
4. 不接入 Cron，不推送飞书。
