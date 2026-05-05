# 任务 3A-0：09:26 开盘操作表 DB latest 查询盘点

适用文件：`/home/admin/.hermes/scripts/ashare_opening_action_table.py`

本次审计原则：只读检查代码与 SQLite schema；不修改代码、不改数据库结构、不改 Cron、不改飞书推送。

## 直接 SQLite 查询

### `fetch_index_snapshot_from_db(mapping=None)`

数据库：`/home/admin/Notes/market/ashare-monitor/ashare_monitor.db`

表：`index_snapshots`

当前关键 SQL：

```sql
SELECT MAX(trade_date) FROM index_snapshots;
SELECT MAX(captured_at) FROM index_snapshots WHERE trade_date = ?;
SELECT index_code, index_name, latest_value, pct_change, raw_json
FROM index_snapshots
WHERE trade_date = ? AND captured_at = ?
ORDER BY id ASC;
```

风险：`trade_date` 来自 DB 最大交易日，不是 09:26 报告目标日；`captured_at` 不限制在报告生成时间之前，可能读取 09:26 后、午盘或收盘快照。

最小修复：增加 `target_date` 与 `asof_time/report_now` 参数；查询改为 `WHERE trade_date = ? AND captured_at <= ?` 范围内的 `MAX(captured_at)`；没有符合条件的数据则返回 `[]`。

### `fetch_strong_boards_from_db(limit=6)`

数据库：`/home/admin/Notes/market/ashare-monitor/ashare_monitor.db`

表：`sector_snapshots`

当前关键 SQL：

```sql
SELECT MAX(trade_date) FROM sector_snapshots;
SELECT MAX(captured_at) FROM sector_snapshots WHERE trade_date = ?;
SELECT sector_name, pct_change, up_count, down_count, leader_name, raw_json, captured_at
FROM sector_snapshots
WHERE trade_date = ? AND captured_at = ?
ORDER BY id DESC
LIMIT 80;
```

风险：同上。尤其会把目标日之后、或目标日 09:26 之后的强板块用于开盘新候选生成。

最小修复：同指数函数。`ORDER BY id DESC LIMIT 80` 是选中一个快照批次后的行级截取，不是核心 latest 风险；核心风险是 `MAX(trade_date)` 与不带上限的 `MAX(captured_at)`。

## 间接 ledger latest 风险

### `load_ledger_holdings()`

调用：

```python
rows = ledger.load_snapshot_rows(TODAY)
...
latest_date, latest_rows = ledger.load_latest_snapshot_rows()
```

风险：`load_latest_snapshot_rows()` 是裸 latest fallback。09:26 使用前一复盘日持仓有业务合理性，但不应使用任意 latest。

建议：不要改 `ashare_ledger_lib.py`；在本脚本中限制为 `TODAY` 或 `preferred_review_day_dir().name`。

### `main()` 中 `ledger.latest_report_summary()`

调用：

```python
ledger.latest_report_summary(TODAY) or ledger.latest_report_summary(ledger_trade_date) or ledger.latest_report_summary()
```

风险：无参数调用可能读取任意最新日报，与持仓日期错配或读到未来日。

建议：去掉无参数 fallback，或只允许显式日期。

## 相关表结构结论

无需改 schema。

| 表 | trade_date | captured_at | created_at | run_id/capture_id | 结论 |
|---|---:|---:|---:|---:|---|
| `index_snapshots` | 有 | 有 | 有 | `run_id` 有 | 可直接按日期+时间过滤 |
| `sector_snapshots` | 有 | 有 | 有 | `run_id` 有 | 可直接按日期+时间过滤 |
| `daily_position_snapshots` | 有 | 无 | 有 | 无 | 可按日期过滤 |
| `daily_reports` | 有，主键 | 无 | 有 | 无 | 可按日期过滤 |

## 推荐测试

新增：`tests/test_opening_action_db_date_filter.py`

覆盖：
1. 两个交易日快照时只读取 `target_date`。
2. 只有前一交易日快照时返回空，不静默使用。
3. `target_date` 有数据但 `captured_at > asof_time` 时返回空。
4. 同日多批快照时取 `asof_time` 前最近一批。
5. `index_snapshots` 与 `sector_snapshots` 均覆盖。
6. 原有开盘操作表、运行窗口 warning、元信息块测试仍通过。

## 建议分批

第一批只修：
- `fetch_index_snapshot_from_db()`
- `fetch_strong_boards_from_db()`

暂缓：
- `load_ledger_holdings()`
- `ledger.latest_report_summary()`
- 文件型 `latest_*_before_today()` helpers
