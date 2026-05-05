import sqlite3
import sys
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ashare_background_monitor as monitor


def _use_temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "ashare_monitor.db"
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.setattr(monitor, "DB_PATH", db_path)
    return db_path


def _stock_df(code="600001", name="样本股", pct_change=1.0):
    return pd.DataFrame(
        [
            {
                "code": code,
                "name": name,
                "latest_price": 10.0,
                "pct_change": pct_change,
                "change_amount": 0.1,
                "volume": 10000,
                "amount": 1000000,
                "amplitude": 2.0,
                "turnover_rate": 3.0,
                "volume_ratio": 1.2,
                "pe_dynamic": 10.0,
                "pb": 1.0,
                "market_value": 1000000000,
                "circulating_market_value": 800000000,
                "is_main_board": True,
            }
        ]
    )


def _insert_capture(
    captured_at,
    trade_date,
    sector_name="目标板块",
    code="600001",
    pct_change=1.0,
    monkeypatch=None,
    tmp_path=None,
):
    summary = {
        "captured_at": captured_at,
        "trade_date": trade_date,
        "source": "pytest",
        "fetch_method": "pytest",
        "total_stocks": 1,
        "market_count": 1,
        "total_count": 1,
        "up_count": 1,
        "down_count": 0,
        "flat_count": 0,
        "strong_up_count": 0,
        "strong_down_count": 0,
        "big_drop_count": 0,
        "sector_constituent_cache": [
            {
                "sector_name": sector_name,
                "code": code,
                "name": f"{sector_name}成分",
                "latest_price": 10.0,
                "pct_change": pct_change,
                "amount": 1000000,
                "turnover_rate": 3.0,
                "role": "龙头",
                "is_sector_leader": 1,
                "raw": {"tag": sector_name},
            }
        ],
    }
    index_items = [
        {
            "index_code": "sh000001",
            "index_name": "上证指数",
            "latest_value": 3000,
            "pct_change": 0.5,
            "amount": 100000000,
            "high": 3010,
            "low": 2990,
            "raw": {"tag": sector_name},
        }
    ]
    sector_items = [
        {
            "sector_name": sector_name,
            "pct_change": pct_change,
            "up_count": 10,
            "down_count": 2,
            "leader_name": f"{sector_name}成分",
            "leader_code": code,
            "net_inflow": 10000000,
            "net_inflow_pct": 1.5,
            "turnover_rate": 3.0,
            "raw": {"tag": sector_name},
        }
    ]
    watchlist_items = [
        {
            "code": code,
            "name": f"{sector_name}观察",
            "source_group": "pytest",
            "latest_price": 10.0,
            "pct_change": pct_change,
            "volume_ratio": 1.2,
            "turnover_rate": 3.0,
            "near_support_flag": 1,
            "near_resistance_flag": 0,
            "intraday_note": "pytest",
            "raw": {"tag": sector_name},
        }
    ]
    return monitor.insert_db(summary, _stock_df(code=code, pct_change=pct_change), index_items, sector_items, watchlist_items)


def test_insert_db_writes_capture_run_date_and_snapshot_run_ids(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    run_id = _insert_capture("2026-05-04T09:25:00+08:00", "2026-05-04")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    capture = conn.execute("SELECT * FROM capture_runs WHERE id = ?", (run_id,)).fetchone()
    assert capture["trade_date"] == "2026-05-04"
    assert capture["captured_at"] == "2026-05-04T09:25:00+08:00"

    for table in [
        "index_snapshots",
        "sector_snapshots",
        "stock_snapshots",
        "watchlist_snapshots",
        "sector_constituent_snapshots",
    ]:
        rows = conn.execute(
            f"SELECT run_id, trade_date, captured_at FROM {table}"
        ).fetchall()
        assert rows, table
        assert {row["run_id"] for row in rows} == {run_id}
        assert {row["trade_date"] for row in rows} == {"2026-05-04"}
        assert {row["captured_at"] for row in rows} == {"2026-05-04T09:25:00+08:00"}
    conn.close()


def test_latest_query_reads_only_target_date(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    _insert_capture("2026-05-04T09:25:00+08:00", "2026-05-04", sector_name="目标日板块", code="600001")
    _insert_capture("2026-05-05T09:25:00+08:00", "2026-05-05", sector_name="未来日板块", code="600002")

    code_map, groups, stage_map, meta = monitor.load_recent_sector_context_from_db(
        target_date="2026-05-04",
        asof_time="2026-05-04T09:26:00+08:00",
    )

    assert code_map["600001"]["sector_name"] == "目标日板块"
    assert "600002" not in code_map
    assert set(groups) == {"目标日板块"}
    assert set(meta) == {"目标日板块"}


def test_latest_query_reads_only_captured_at_at_or_before_asof(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    _insert_capture("2026-05-04T09:30:00+08:00", "2026-05-04", sector_name="未来快照", code="600003")

    code_map, groups, stage_map, meta = monitor.load_recent_sector_context_from_db(
        target_date="2026-05-04",
        asof_time="2026-05-04T09:26:00+08:00",
    )

    assert code_map == {}
    assert groups == {}
    assert stage_map == {}
    assert meta == {}


def test_latest_query_does_not_treat_previous_trade_date_as_today(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    _insert_capture("2026-05-01T14:55:00+08:00", "2026-05-01", sector_name="前日板块", code="600004")

    code_map, groups, stage_map, meta = monitor.load_recent_sector_context_from_db(
        target_date="2026-05-04",
        asof_time="2026-05-04T09:26:00+08:00",
    )

    assert code_map == {}
    assert groups == {}
    assert stage_map == {}
    assert meta == {}


def test_same_target_date_uses_latest_snapshot_before_asof(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    _insert_capture("2026-05-04T09:20:00+08:00", "2026-05-04", sector_name="0920板块", code="600020", pct_change=1.0)
    _insert_capture("2026-05-04T09:25:00+08:00", "2026-05-04", sector_name="0925板块", code="600025", pct_change=2.0)
    _insert_capture("2026-05-04T09:30:00+08:00", "2026-05-04", sector_name="0930板块", code="600030", pct_change=3.0)

    code_map, groups, stage_map, meta = monitor.load_recent_sector_context_from_db(
        target_date="2026-05-04",
        asof_time="2026-05-04T09:26:00+08:00",
    )

    assert set(code_map) == {"600025"}
    assert code_map["600025"]["sector_name"] == "0925板块"
    assert set(groups) == {"0925板块"}
    assert meta["0925板块"]["pct_change"] == 2.0
