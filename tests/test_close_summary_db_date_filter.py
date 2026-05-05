import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ashare_close_summary as close_summary


def _init_db(path: Path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE capture_runs (
            id INTEGER PRIMARY KEY,
            trade_date TEXT,
            captured_at TEXT,
            summary_json TEXT,
            strong_up_count INTEGER,
            strong_down_count INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE index_snapshots (
            id INTEGER PRIMARY KEY,
            run_id INTEGER,
            trade_date TEXT,
            captured_at TEXT,
            index_name TEXT,
            pct_change REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE stock_snapshots (
            id INTEGER PRIMARY KEY,
            run_id INTEGER,
            trade_date TEXT,
            captured_at TEXT,
            code TEXT,
            amount REAL,
            pct_change REAL
        )
        """
    )
    conn.commit()
    conn.close()


def _use_temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "ashare_monitor.db"
    _init_db(db_path)
    monkeypatch.setattr(close_summary, "DB_PATH", db_path)
    return db_path


def _insert_capture(db_path, run_id, trade_date, captured_at, summary_json="{}"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO capture_runs (id, trade_date, captured_at, summary_json) VALUES (?, ?, ?, ?)",
        (run_id, trade_date, captured_at, summary_json),
    )
    conn.commit()
    conn.close()


def test_read_latest_capture_reads_only_target_date(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    _insert_capture(db_path, 1, "2026-05-01", "2026-05-01T15:00:00+08:00", '{"tag":"old"}')
    _insert_capture(db_path, 2, "2026-05-04", "2026-05-04T15:00:00+08:00", '{"tag":"target"}')

    row = close_summary.read_latest_capture(
        target_date="2026-05-04",
        asof_time="2026-05-04T15:30:00+08:00",
    )

    assert row is not None
    assert row["id"] == 2
    assert row["trade_date"] == "2026-05-04"
    assert row["summary"]["tag"] == "target"


def test_read_latest_capture_returns_none_when_only_previous_trade_date(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    _insert_capture(db_path, 1, "2026-05-01", "2026-05-01T15:00:00+08:00")

    row = close_summary.read_latest_capture(
        target_date="2026-05-04",
        asof_time="2026-05-04T15:30:00+08:00",
    )

    assert row is None


def test_read_latest_capture_uses_latest_capture_before_asof(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    _insert_capture(db_path, 1, "2026-05-04", "2026-05-04T14:00:00+08:00", '{"tag":"early"}')
    _insert_capture(db_path, 2, "2026-05-04", "2026-05-04T15:00:00+08:00", '{"tag":"selected"}')
    _insert_capture(db_path, 3, "2026-05-04", "2026-05-04T16:00:00+08:00", '{"tag":"future"}')

    row = close_summary.read_latest_capture(
        target_date="2026-05-04",
        asof_time="2026-05-04T15:30:00+08:00",
    )

    assert row is not None
    assert row["id"] == 2
    assert row["summary"]["tag"] == "selected"


def test_load_latest_layer_rows_uses_target_date_and_asof_run_id(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO index_snapshots (run_id, trade_date, captured_at, index_name, pct_change) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "2026-05-01", "2026-05-01T15:00:00+08:00", "old", -1.0),
            (2, "2026-05-04", "2026-05-04T14:00:00+08:00", "early", 1.0),
            (3, "2026-05-04", "2026-05-04T15:00:00+08:00", "selected-a", 2.0),
            (3, "2026-05-04", "2026-05-04T15:00:00+08:00", "selected-b", 2.1),
            (4, "2026-05-04", "2026-05-04T16:00:00+08:00", "future", 3.0),
        ],
    )
    conn.commit()
    conn.close()

    rows = close_summary.load_latest_layer_rows(
        "index_snapshots",
        "2026-05-04",
        "id ASC",
        asof_time="2026-05-04T15:30:00+08:00",
    )

    assert [r["index_name"] for r in rows] == ["selected-a", "selected-b"]
    assert {r["run_id"] for r in rows} == {3}
    assert {r["trade_date"] for r in rows} == {"2026-05-04"}


def test_load_latest_layer_rows_second_select_filters_trade_date(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO index_snapshots (run_id, trade_date, captured_at, index_name, pct_change) VALUES (?, ?, ?, ?, ?)",
        [
            (5, "2026-05-04", "2026-05-04T15:00:00+08:00", "target", 1.0),
            (5, "2026-05-01", "2026-05-04T15:00:00+08:00", "same-run-old-date", 9.0),
        ],
    )
    conn.commit()
    conn.close()

    rows = close_summary.load_latest_layer_rows(
        "index_snapshots",
        "2026-05-04",
        "id ASC",
        asof_time="2026-05-04T15:30:00+08:00",
    )

    assert [r["index_name"] for r in rows] == ["target"]


def test_get_db_total_amount_aggregates_only_target_date_run_snapshots(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    _insert_capture(db_path, 10, "2026-05-04", "2026-05-04T15:00:00+08:00")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO stock_snapshots (run_id, trade_date, captured_at, code, amount, pct_change) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (10, "2026-05-04", "2026-05-04T15:00:00+08:00", "600001", 100.0, 1.0),
            (10, "2026-05-04", "2026-05-04T15:00:00+08:00", "600002", 200.0, 2.0),
            (10, "2026-05-01", "2026-05-04T15:00:00+08:00", "600003", 999.0, 9.0),
        ],
    )
    conn.commit()
    conn.close()

    total = close_summary.get_db_total_amount(
        "2026-05-04",
        asof_time="2026-05-04T15:30:00+08:00",
    )

    assert total == 300.0


def test_get_limit_stats_db_fallback_filters_stock_snapshots_by_target_date(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO stock_snapshots (run_id, trade_date, captured_at, code, amount, pct_change) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (8, "2026-05-04", "2026-05-04T15:00:00+08:00", "600001", 100.0, 9.6),
            (8, "2026-05-04", "2026-05-04T15:00:00+08:00", "600002", 100.0, -1.0),
            (8, "2026-05-01", "2026-05-01T15:00:00+08:00", "600003", 100.0, -6.0),
        ],
    )
    conn.commit()
    conn.close()

    def _raise(*args, **kwargs):
        raise RuntimeError("akshare unavailable")

    monkeypatch.setattr(close_summary.adu, "ak_call", _raise)

    stats = close_summary.get_limit_stats(
        "20260504",
        latest_capture={},
        run_id=8,
        target_date="2026-05-04",
    )

    assert stats["zt_count"] == 1
    assert stats["dt_count"] == 0
    assert stats["source"] == "db_capture_fallback"


def test_db_helpers_do_not_fallback_when_target_date_snapshot_missing(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    _insert_capture(db_path, 1, "2026-05-01", "2026-05-01T15:00:00+08:00")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO index_snapshots (run_id, trade_date, captured_at, index_name, pct_change) VALUES (?, ?, ?, ?, ?)",
        (1, "2026-05-01", "2026-05-01T15:00:00+08:00", "old", -1.0),
    )
    conn.execute(
        "INSERT INTO stock_snapshots (run_id, trade_date, captured_at, code, amount, pct_change) VALUES (?, ?, ?, ?, ?, ?)",
        (1, "2026-05-01", "2026-05-01T15:00:00+08:00", "600001", 999.0, -1.0),
    )
    conn.commit()
    conn.close()

    asof = "2026-05-04T15:30:00+08:00"
    assert close_summary.read_latest_capture(target_date="2026-05-04", asof_time=asof) is None
    assert close_summary.load_latest_layer_rows("index_snapshots", "2026-05-04", asof_time=asof) == []
    assert close_summary.get_db_total_amount("2026-05-04", asof_time=asof) is None
