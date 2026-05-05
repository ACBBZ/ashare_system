# AShare Background Monitor DB Latest Audit (Task 3C-0)

Status: superseded by the implemented Task 3C fix. For the current repair pattern, exact call-chain propagation, and regression commands, use `references/ashare-background-monitor-db-date-filter.md`.

Session learning: `ashare_background_monitor.py` is primarily a writer of intraday snapshots, but it also has a DB cache read path that can reintroduce time-travel bugs after opening/close summary fixes.

## Files inspected

- `/home/admin/.hermes/scripts/ashare_background_monitor.py`
- `/home/admin/.hermes/scripts/ashare_postclose_capture.py` as a thin wrapper that imports and calls background monitor functions
- `/home/admin/.hermes/scripts/ashare_data_utils.py` for trading-day gate context
- Read-only SQLite PRAGMA against `/home/admin/Notes/market/ashare-monitor/ashare_monitor.db`

## Confirmed write model

`insert_db(summary, df, index_items, sector_items, watchlist_items)` writes one `capture_runs` row, then uses `cur.lastrowid` as `run_id` for all snapshot tables.

The following tables have `trade_date` and `captured_at`; all snapshot tables also have `run_id`:

- `capture_runs`
- `index_snapshots`
- `sector_snapshots`
- `watchlist_snapshots`
- `sector_constituent_snapshots`
- `stock_snapshots`

Practical conclusion: no schema change is needed for strict `target_date/asof_time` filtering. Same-run restoration is available through `run_id`.

## Historical high-risk latest query found in Task 3C-0

The following patterns were the audited pre-fix risk; they should not be reintroduced.

```sql
SELECT sector_name, code, name, latest_price, pct_change, amount, turnover_rate,
       role, is_sector_leader, raw_json
FROM sector_constituent_snapshots
ORDER BY trade_date DESC, captured_at DESC, id DESC
LIMIT 12000
```

```sql
SELECT sector_name, pct_change, up_count, down_count, leader_name, leader_code,
       net_inflow, raw_json
FROM sector_snapshots
ORDER BY trade_date DESC, captured_at DESC, id DESC
LIMIT 600
```

Risk: no `trade_date = target_date`, no `captured_at <= asof_time`; can silently use previous trading day/global latest data.

Call chain:

- `infer_sector_snapshots_from_cache(df, limit=8)` uses this cache when live industry-board fetch fails.
- `fetch_sector_constituent_maps(sector_items)` uses this cache when a sector constituent fetch fails.
- `attach_sector_context_to_anomalies()` and `build_summary()` consume the resulting sector context.

## Implemented 3C repair pattern

Task 3C has been implemented. Current code should keep compatible `target_date` and `asof_time` parameters through the call chain and query only compliant rows:

```sql
WHERE trade_date = ?
  AND captured_at <= ?
ORDER BY captured_at DESC, id DESC
```

No compliant rows should return empty structures (`{}, {}, {}, {}`), not previous-day/global latest data. See `references/ashare-background-monitor-db-date-filter.md` for the implemented two-step `(run_id, captured_at)` selection pattern and tests.

## File-latest caveat

`read_latest_close_summary()` does directory latest over `20*-*-*/close-summary.md`. It is not a DB latest query and feeds watchlist parsing. Treat it as low-risk/observe unless the user explicitly asks to change observation-pool source semantics; do not mix it into DB latest fixes.

## Regression tests from Task 3C-0 audit

During the read-only audit before implementation, the existing A/B tests passed:

```bash
python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_close_summary_db_date_filter.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_db_date_filter.py -q
```

For post-implementation 3C verification, run the command set in `references/ashare-background-monitor-db-date-filter.md`.
