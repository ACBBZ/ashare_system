# A 股时间穿越风险修复阶段总验收（任务 1/2/3/4/5）

Use this reference when asked to review, summarize, or extend the first-stage A 股 “时间穿越” risk fixes. This is a session-specific acceptance summary; keep class-level guidance in `SKILL.md`.

## Scope validated

Completed fixes covered:

1. `ashare_data_utils.py` — shared `target_date` / `asof` validation utilities.
2. `ashare_position_watch_analysis.py` — strict-today position/watch analysis; no previous-trading-day price substitution for technical judgment.
3. `ashare_opening_action_table.py` — 09:26 opening action table DB latest filters, run-window warning, and data-time metadata.
4. `ashare_close_summary.py` — close-summary DB latest filters and data-time metadata.
5. `ashare_background_monitor.py` — background monitor / intraday sector snapshot latest filters.
6. `ashare_opening_brief.py` — 09:00 opening brief data-time metadata.

Related tests:

- `tests/test_data_asof_validation.py`
- `tests/test_position_watch_analysis_strict_today.py`
- `tests/test_position_watch_report_metadata.py`
- `tests/test_opening_action_table.py`
- `tests/test_opening_action_db_date_filter.py`
- `tests/test_opening_action_window.py`
- `tests/test_opening_report_metadata.py`
- `tests/test_close_summary_db_date_filter.py`
- `tests/test_close_summary_report_metadata.py`
- `tests/test_background_monitor_db_date_filter.py`
- `tests/test_opening_brief_report_metadata.py`

## Regression command block

Run from `/home/admin/.hermes/scripts` for review-only validation:

```bash
python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_analysis_strict_today.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_table.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_window.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_close_summary_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_close_summary_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_background_monitor_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_brief_report_metadata.py -q
```

Observed stage acceptance result: **59 passed, 0 failed**.

Syntax check:

```bash
python -m py_compile \
/home/admin/.hermes/scripts/ashare_data_utils.py \
/home/admin/.hermes/scripts/ashare_position_watch_analysis.py \
/home/admin/.hermes/scripts/ashare_opening_action_table.py \
/home/admin/.hermes/scripts/ashare_close_summary.py \
/home/admin/.hermes/scripts/ashare_background_monitor.py \
/home/admin/.hermes/scripts/ashare_opening_brief.py
```

Observed result: passed with exit code 0.

## Acceptance conclusions

First-stage time-travel risk can be marked **stage-complete**, not “risk zero”.

Covered risks:

- Position/watch technical analysis refuses previous-trading-day price substitution; missing today quote renders “今日行情缺失，未生成技术判断”.
- 09:26 opening action table DB fallback for indices / strong sectors is anchored to `target_date` and `captured_at <= asof_time`.
- Close summary `latest_capture`, snapshot layer rows, and DB total amount are anchored to `target_date` / `asof_time`.
- Background monitor `sector_snapshots` and `sector_constituent_snapshots` use two-stage selection: first find compliant `run_id/captured_at`, then read rows from the same `trade_date + run_id + captured_at`.
- Four core reports have data-time metadata: 09:00 opening brief, 09:26 opening action table, 17:00 close summary, 17:30 position/watch analysis.

## Remaining risks to call out

Must-fix / high-priority follow-up:

- `ashare_position_watch_analysis.py` still has board/sector context functions (`find_latest_run_id`, `find_latest_run_id_with_sector_constituents`) that can fallback to global latest when same-day sector constituents are absent. This does **not** re-enable old-price technical analysis, but can contaminate sector/stage/role context. Fix it later using the 3C pattern: no previous-day/global fallback; return empty sector context.

Recommended next fixes:

- Task 3D: opening brief linked-data date filtering for prior summaries/position analysis/candidate sources.
- Task 6: strict timestamp filtering for TrendRadar / Google News / announcements (`published_at <= generated_at`, timezone normalization, missing timestamp warnings).
- Task 7: real report metadata counts (`fallback_count`, `missing_count`, `stale_count`, `empty_context_count`) instead of only qualitative “正常/存在缺失/存在降级”.
- Task 8: candidate-pool effectiveness statistics / lightweight backtest after data-date safety stabilizes.
- Task 9: low-risk engineering refactor only after production observation, preferably sidecar/shadow mode.

## Production observation checklist

After the next real automated run, inspect outputs before doing more code changes:

### Background monitor

- `ashare_monitor.db` writes normally.
- `sector_snapshots.trade_date` and `sector_constituent_snapshots.trade_date` are the target day.
- `run_id`, `trade_date`, `captured_at` are consistent within a snapshot batch.
- Watch for `sector cache unavailable`.
- Watch for large increases in “待补充分板块映射” / “待判断”; this may indicate correct no-fallback behavior plus missing same-day sector cache.

### 09:26 opening action table

- Top metadata appears.
- Run-window warning is correct.
- Indices / strong boards are target-day data.
- If data is empty, report still renders with clear degradation instead of using previous-day strong boards.

### 17:00 close summary

- Top metadata appears.
- `latest_capture` is target day.
- Amount, sectors, limit-up/down stats, candidates, A/B/C tiering, and strategy tracking remain normal.

### 17:30 position/watch analysis

- strict_today remains effective.
- Missing today quote does not output trend/channel/support/resistance/RR/tomorrow range.
- Watch for stale sector context until the remaining sector fallback is fixed.

## Review-only guardrails

For future acceptance checks:

- Do not run production scripts if they write DB/Markdown/JSONL or deliver messages.
- Prefer pytest with monkeypatched paths, `py_compile`, static SQL inspection, cron listing, file mtime checks, and backup-file listing.
- In `/home/admin/.hermes/scripts`, do not claim diff-level proof unless a git repo exists; use mtime + targeted code searches as evidence.
