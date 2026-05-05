# AShare Background Monitor DB Date Filter Fix (Task 3C)

Session learning: fixing time-travel risk in `/home/admin/.hermes/scripts/ashare_background_monitor.py` should be done as a narrow DB-read filter change, not as a schema/Cron/report refactor.

## Scope used

Allowed/changed:

- `/home/admin/.hermes/scripts/ashare_background_monitor.py`
- `/home/admin/.hermes/scripts/tests/test_background_monitor_db_date_filter.py`

Not changed:

- `ashare_close_summary.py`, `ashare_opening_action_table.py`, `ashare_opening_brief.py`, `ashare_position_watch_analysis.py`
- `ashare_ledger_lib.py`, `ashare_strategy_engine.py`
- Cron, Feishu push logic, DB schema, candidate generation, A/B/C tiering, `strategy_scoreboard.db` writes
- `ashare_postclose_capture.py` was not needed

## Fix pattern

`load_recent_sector_context_from_db()` now accepts `target_date` and `asof_time` and must only read rows where:

```sql
trade_date = ?
AND captured_at <= ?
```

For each table, first select the latest compliant `(run_id, captured_at)` for the target date/asof, then read only rows from the same run/date/capture:

```sql
SELECT run_id, captured_at
FROM {table_name}
WHERE trade_date = ?
  AND captured_at <= ?
ORDER BY captured_at DESC, id DESC
LIMIT 1
```

```sql
SELECT {columns}
FROM {table_name}
WHERE trade_date = ?
  AND run_id = ?
  AND captured_at = ?
ORDER BY id DESC
LIMIT ?
```

This avoids mixing rows from multiple captures on the same day and avoids same-run rows with wrong `trade_date`.

## Call-chain propagation

Propagate `target_date/asof_time` through cache/fallback readers:

- `infer_sector_snapshots_from_cache(..., target_date=None, asof_time=None)`
- `fetch_sector_snapshots(..., target_date=None, asof_time=None)`
- `fetch_sector_constituent_maps(..., target_date=None, asof_time=None)`
- `attach_sector_context_to_anomalies(..., target_date=None, asof_time=None)`
- `build_summary()` passes `captured_at.date().isoformat()` and `captured_at`
- `main()` passes current `trade_date` and `current` into `fetch_sector_snapshots()`

Keep default parameters for compatibility with thin wrappers and ad-hoc imports.

## Required behavior

- If only previous trade-day data exists, return empty structures, not previous-day data.
- If target-date data exists only after `asof_time`, return empty structures.
- If multiple target-date captures exist, use the latest capture at or before `asof_time`.
- Do not fallback to global latest ordering.
- Keep `insert_db()` run_id/write behavior unchanged.

## Tests added

`/home/admin/.hermes/scripts/tests/test_background_monitor_db_date_filter.py` should cover at least:

- `insert_db()` writes `capture_runs.trade_date/captured_at` correctly.
- `index_snapshots`, `sector_snapshots`, `stock_snapshots`, `watchlist_snapshots`, and `sector_constituent_snapshots` use the same `run_id` as `capture_runs`.
- Latest query reads only `target_date`.
- Latest query reads only `captured_at <= asof_time`.
- Previous trading day data is not treated as today's data.
- Multiple captures on same target date select the latest before asof.

## Verification commands

```bash
python -m py_compile /home/admin/.hermes/scripts/ashare_background_monitor.py
python -m pytest /home/admin/.hermes/scripts/tests/test_background_monitor_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_close_summary_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_db_date_filter.py -q
```

If `ashare_postclose_capture.py` is modified for signature compatibility, also run:

```bash
python -m py_compile /home/admin/.hermes/scripts/ashare_postclose_capture.py
```

Known successful run from the implementation session:

- `test_background_monitor_db_date_filter.py`: `5 passed`
- `test_data_asof_validation.py`: `8 passed`
- `test_close_summary_db_date_filter.py`: `8 passed`
- `test_opening_action_db_date_filter.py`: `8 passed`
