# 收盘摘要 DB latest 日期/时间边界修复模式

Session: 2026-05-04, 任务 3B。

## Problem Pattern

A 股自动化报告中凡是从 SQLite 读取“latest / 最近一条”的 SQL，都可能产生时间穿越：

- `ORDER BY captured_at DESC LIMIT 1` 只按全库最新取数。
- 只过滤 `trade_date` 但不限制 `captured_at <= asof_time`，会读取报告生成时点之后的同日快照。
- 第一段 SQL 选对 run_id 后，第二段 `SELECT * WHERE run_id = ?` 没有 `trade_date` 防御，可能混入同 run_id 下其他日期的脏数据。
- 当目标日无数据时静默 fallback 到前一交易日/全库 latest，会让报告误称“今日”。

## Proven Minimal Fix

For close-summary scripts, keep business/report logic unchanged and only tighten DB access boundaries.

1. Make the report anchor explicit:
   - `target_date = TODAY`
   - `asof_time = datetime.now().astimezone()`
   - pass both into DB helper functions from `main()`.
2. Normalize `asof_time` to comparable ISO text:
   - if datetime-like, use `.isoformat()`; otherwise `str(asof_time)`.
3. For `capture_runs` latest query:
   ```sql
   SELECT * FROM capture_runs
   WHERE trade_date = ? AND captured_at <= ?
   ORDER BY captured_at DESC
   LIMIT 1
   ```
   Return `None` if no row; do not fallback to previous trade date or global latest.
4. For layer snapshot latest run query:
   ```sql
   SELECT run_id FROM <snapshot_table>
   WHERE trade_date = ? AND captured_at <= ?
   ORDER BY captured_at DESC
   LIMIT 1
   ```
   Then defensive second-stage query:
   ```sql
   SELECT * FROM <snapshot_table>
   WHERE run_id = ? AND trade_date = ?
   ORDER BY <stable_order>
   ```
   Return `[]` if no compliant run.
5. For stock amount aggregation:
   - choose `capture_runs.id` with `trade_date = ? AND captured_at <= ?`;
   - aggregate `stock_snapshots` with both `run_id = ?` and `trade_date = ?`;
   - return `None` if no target-date capture.
6. For limit stats DB fallback:
   - do not change AkShare logic;
   - when fallback reads `stock_snapshots`, add `AND trade_date = ?` if `target_date` is available.
7. For trade date selection:
   - never treat DB global latest as report today;
   - return `today = target_date/TODAY`;
   - only use DB/AkShare/filesystem to identify `prev_date < target_date`.

## Regression Tests to Add

Use a temporary SQLite DB and monkeypatch the script-level `DB_PATH`. Cover:

- `read_latest_capture()` chooses only `target_date` when multiple trade dates exist.
- target date absent returns `None` instead of previous-day fallback.
- same `target_date` with multiple `captured_at` values chooses the latest `<= asof_time`.
- `load_latest_layer_rows()` chooses the target-date/asof-compliant run_id.
- second-stage `SELECT *` does not include rows from other `trade_date` even with same run_id.
- `get_db_total_amount()` aggregates only target-date snapshots for the chosen run_id.
- DB helpers return `None`/`[]` when target-date data is missing.
- metadata/report-structure tests still pass.

## Verification Commands Used

```bash
python -m py_compile /home/admin/.hermes/scripts/ashare_close_summary.py
python -m pytest /home/admin/.hermes/scripts/tests/test_close_summary_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_close_summary_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_analysis_strict_today.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_brief_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_table.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_window.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_report_metadata.py -q
```

## Pitfalls

- If the scripts directory is not under git, create a timestamped backup before editing, e.g. `ashare_close_summary.py.bak-YYYYMMDD-HHMMSS`.
- Do not fix time-travel by changing Cron, DB schemas, Feishu push, candidate generation, A/B/C tiering, `strategy_scoreboard.db` writes, or report sections unless explicitly requested.
- `capture_runs` uses `captured_at`; do not assume a `created_at` field.
- Keep tests close-summary scoped; do not touch opening-action tests for close-summary changes.
