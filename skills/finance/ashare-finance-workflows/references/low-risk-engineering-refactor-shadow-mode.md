# A 股自动化系统低风险工程化重构与 Shadow Mode

## Context

用于已有 A 股 Hermes Agent 自动化生产链路的工程化重构：09:00 盘前简报、09:26 开盘行动表、15:05 持仓盈亏、16:50 候选跟踪、17:00 收盘摘要、17:30 持仓/候选盘后分析。当前生产脚本集中在 `/home/admin/.hermes/scripts/`，输出在 `/home/admin/Notes/market/ashare-monitor/`，核心 DB 为 `ashare_monitor.db`、`strategy_scoreboard.db`、`ashare_ledger.db`。

## Non-Negotiable Safety Rules

1. 不要直接修改现有正在运行的生产脚本。
2. 不要破坏当前 Cron 自动化任务。
3. 不要影响已能生成的盘前简报、开盘行动表、收盘摘要、持仓分析和飞书推送。
4. 新模块先以旁路方式运行（shadow mode），连续对比新旧输出稳定后再考虑替换。
5. 第一阶段只读旧 DB，不改旧 DB schema，不覆盖生产 Markdown，不发送 shadow 飞书。

## Production Chain: Treat as Read-Only During Refactor

- 09:00 盘前简报：`/home/admin/.hermes/scripts/ashare_opening_brief.py`
- 09:26 开盘行动表：`/home/admin/.hermes/scripts/ashare_opening_action_table.py`
- 15:05 持仓盈亏：`/home/admin/.hermes/scripts/ashare_ledger_daily_report.py` + `ashare_ledger_lib.py`
- 16:50 候选跟踪：`/home/admin/.hermes/scripts/ashare_strategy_tracker.py` + `ashare_strategy_engine.py`
- 17:00 收盘摘要：`/home/admin/.hermes/scripts/ashare_close_summary.py`
- 17:30 持仓/候选分析：`/home/admin/.hermes/scripts/ashare_position_watch_analysis.py`
- 数据采集底座：`ashare_background_monitor.py`、`ashare_postclose_capture.py`、`ashare_data_utils.py`
- 主 DB：`/home/admin/Notes/market/ashare-monitor/ashare_monitor.db`
- 策略 DB：`/home/admin/Notes/market/ashare-monitor/strategy/strategy_scoreboard.db`
- 账本 DB：`/home/admin/Notes/market/ashare-monitor/ledger/ashare_ledger.db`
- 生产输出：`/home/admin/Notes/market/ashare-monitor/YYYY-MM-DD/`

## Recommended New Sidecar Structure

Create a sidecar project outside `.hermes/scripts`, e.g. `/home/admin/ashare_agent/`:

```text
ashare_agent/
  configs/              # paths.yaml, data_sources.yaml, strategy_rules.yaml, shadow.yaml
  ashare_agent/core/    # dates, logging, errors, common types
  ashare_agent/data/    # schemas, validators, normalizers, calendar
  ashare_agent/providers/ # provider interface + akshare/eastmoney/tencent/etc.
  ashare_agent/storage/ # sqlite helpers, legacy_readers, shadow_store
  ashare_agent/features/ # technical, volume-price, sector, market env features
  ashare_agent/strategy/ # market phase, scoring, filters, risk control
  ashare_agent/backtest/ # signals, execution, portfolio, evaluator
  ashare_agent/reports/  # structured report models, renderers, diff
  ashare_agent/agents/   # deterministic workflow orchestration
  ashare_agent/compatibility/ # adapters for legacy markdown/db formats
  scripts/              # run_shadow_*.py and compare_shadow_outputs.py
  tests/                # unit and integration tests
  docs/                 # architecture, migration, shadow mode, rollback
```

## Migration Order: Low to High Risk

1. **Extract configs** — new `configs/*.yaml` only for shadow modules; no Cron impact; rollback by ignoring sidecar config.
2. **Extract common tools** — normalize_code, safe_float, is_main_board_code, is_fund_like, date helpers; verify against legacy behavior.
3. **Extract data providers / legacy readers** — first implement read-only legacy DB readers; avoid live refetch in v0 shadow.
4. **Extract strategy rules** — market phase, sector score, stock hard filters, A/B/C tiers in sidecar only; compare against old output.
5. **Extract report templates** — structured data -> Markdown shadow reports; do not overwrite production files.
6. **Add backtest module** — offline only, using historical candidate_tracking and daily data.
7. **Replace old scripts** — last step only after shadow passes; replace one low-risk surface at a time with rollback.

## Shadow Mode Design

Shadow outputs should go to a separate directory, preferably:

```text
/home/admin/Notes/market/ashare-monitor-shadow/YYYY-MM-DD/
```

Typical files:

```text
opening-brief-shadow.md
opening-action-table-0926-shadow.md
holding-pnl-1505-shadow.md
close-summary-shadow.md
position-watch-shadow.md
diff-report.md
diff-report.json
```

Diff by structure, not raw text only:

- Market overview: index pct changes,成交额,涨跌停,上涨/下跌家数 with tolerances.
- Sector analysis: Top 3 overlap, stage conflicts, leader differences.
- Candidate pool: Top 6 overlap, A/B/C tier conflicts, mainboard filter, ETF/LOF typing.
- Holdings: symbol, quantity, cost must match; same-day price required.
- Report shape: required sections, date, data timestamps, risk disclaimer, self-review blank area.

## Promotion Criteria

Only consider replacing a production component after **5 consecutive trading days** of shadow runs with:

- all production Cron jobs OK;
- all shadow jobs OK;
- no blocking issue;
- key fields consistent or differences explainable;
- no production file overwritten;
- no production DB schema modified;
- no shadow Feishu spam.

Blocking issues include using previous-day price as today, wrong holdings, wrong mainboard/ETF classification, unexplained opposite market phase, missing risk disclaimer, shadow timeout, or any production overwrite.

## First Week Safe Tasks

1. Create `/home/admin/ashare_agent/` skeleton and docs.
2. Add sidecar configs: `paths.yaml`, `data_sources.yaml`, `strategy_rules.yaml`, `shadow.yaml`.
3. Implement read-only legacy readers for DB and production Markdown.
4. Generate `close-summary-shadow.md` v0 from old DB.
5. Generate `diff-report.md/json` v0 comparing old and shadow close summary.

Do not modify `.hermes/scripts`, Cron definitions, production DB schema, or Feishu delivery during week one.