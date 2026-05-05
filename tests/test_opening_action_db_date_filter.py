import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_opening_action_table.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_opening_action_table', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def make_monitor_db(tmp_path):
    db_path = tmp_path / 'ashare_monitor.db'
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE index_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            index_code TEXT,
            index_name TEXT,
            latest_value REAL,
            pct_change REAL,
            amount REAL,
            high REAL,
            low REAL,
            raw_json TEXT NOT NULL,
            created_at TEXT
        );
        CREATE TABLE sector_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            sector_name TEXT,
            pct_change REAL,
            up_count INTEGER,
            down_count INTEGER,
            leader_name TEXT,
            leader_code TEXT,
            net_inflow REAL,
            raw_json TEXT NOT NULL,
            created_at TEXT,
            net_inflow_pct REAL,
            turnover_rate REAL
        );
        """
    )
    conn.close()
    return db_path


def insert_index_batch(db_path, trade_date, captured_at, base_value):
    rows = [
        ('sh000001', '上证指数', base_value + 1, 1.1),
        ('sz399001', '深证成指', base_value + 2, 2.2),
        ('sz399006', '创业板指', base_value + 3, 3.3),
    ]
    conn = sqlite3.connect(db_path)
    for code, name, latest, pct in rows:
        raw = {'最新价': latest, '今开': latest - 0.5, '昨收': latest - 1, '涨跌幅': pct}
        conn.execute(
            """
            INSERT INTO index_snapshots
            (run_id, captured_at, trade_date, index_code, index_name, latest_value, pct_change, raw_json, created_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (captured_at, trade_date, code, name, latest, pct, json.dumps(raw, ensure_ascii=False), captured_at),
        )
    conn.commit()
    conn.close()


def insert_sector_batch(db_path, trade_date, captured_at, suffix, pct):
    sectors = [
        (f'半导体{suffix}', pct, 80, 10, f'龙头{suffix}', 9.9, 5.5),
        (f'机器人{suffix}', pct - 0.5, 60, 12, f'中军{suffix}', 8.8, 4.4),
    ]
    conn = sqlite3.connect(db_path)
    for name, pct_change, up_count, down_count, leader, leader_pct, turnover in sectors:
        raw = {'领涨股票-涨跌幅': leader_pct, '换手率': turnover}
        conn.execute(
            """
            INSERT INTO sector_snapshots
            (run_id, captured_at, trade_date, sector_name, pct_change, up_count, down_count,
             leader_name, raw_json, created_at, turnover_rate)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (captured_at, trade_date, name, pct_change, up_count, down_count, leader, json.dumps(raw, ensure_ascii=False), captured_at, turnover),
        )
    conn.commit()
    conn.close()


def patch_db_path(monkeypatch, db_path):
    monkeypatch.setattr(mod, 'DB_PATH', db_path)


def test_index_db_reads_only_target_date_when_newer_trade_date_exists(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_index_batch(db_path, '2026-05-06', '2026-05-06T09:25:00+08:00', 1000)
    insert_index_batch(db_path, '2026-05-07', '2026-05-07T09:25:00+08:00', 2000)
    patch_db_path(monkeypatch, db_path)

    rows = mod.fetch_index_snapshot_from_db(
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert len(rows) == 3
    assert rows[0]['latest'] == 1001
    assert rows[0]['pct'] == 1.1


def test_index_db_returns_empty_when_only_previous_trade_date_exists(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_index_batch(db_path, '2026-05-05', '2026-05-05T09:25:00+08:00', 1000)
    patch_db_path(monkeypatch, db_path)

    rows = mod.fetch_index_snapshot_from_db(
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert rows == []


def test_index_db_returns_empty_when_target_snapshot_is_after_asof_time(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_index_batch(db_path, '2026-05-06', '2026-05-06T09:30:00+08:00', 1000)
    patch_db_path(monkeypatch, db_path)

    rows = mod.fetch_index_snapshot_from_db(
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert rows == []


def test_index_db_uses_latest_snapshot_at_or_before_asof_time(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_index_batch(db_path, '2026-05-06', '2026-05-06T09:20:00+08:00', 1000)
    insert_index_batch(db_path, '2026-05-06', '2026-05-06T09:25:00+08:00', 2000)
    insert_index_batch(db_path, '2026-05-06', '2026-05-06T09:30:00+08:00', 3000)
    patch_db_path(monkeypatch, db_path)

    rows = mod.fetch_index_snapshot_from_db(
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert len(rows) == 3
    assert rows[0]['latest'] == 2001


def test_sector_db_reads_only_target_date_when_newer_trade_date_exists(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_sector_batch(db_path, '2026-05-06', '2026-05-06T09:25:00+08:00', '目标日', 3.0)
    insert_sector_batch(db_path, '2026-05-07', '2026-05-07T09:25:00+08:00', '未来日', 9.0)
    patch_db_path(monkeypatch, db_path)

    boards = mod.fetch_strong_boards_from_db(
        limit=2,
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert [b['name'] for b in boards] == ['半导体目标日', '机器人目标日']
    assert boards[0]['pct'] == 3.0


def test_sector_db_returns_empty_when_only_previous_trade_date_exists(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_sector_batch(db_path, '2026-05-05', '2026-05-05T09:25:00+08:00', '前日', 3.0)
    patch_db_path(monkeypatch, db_path)

    boards = mod.fetch_strong_boards_from_db(
        limit=2,
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert boards == []


def test_sector_db_returns_empty_when_target_snapshot_is_after_asof_time(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_sector_batch(db_path, '2026-05-06', '2026-05-06T09:30:00+08:00', '晚于窗口', 3.0)
    patch_db_path(monkeypatch, db_path)

    boards = mod.fetch_strong_boards_from_db(
        limit=2,
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert boards == []


def test_sector_db_uses_latest_snapshot_at_or_before_asof_time(tmp_path, monkeypatch):
    db_path = make_monitor_db(tmp_path)
    insert_sector_batch(db_path, '2026-05-06', '2026-05-06T09:20:00+08:00', '0920', 2.0)
    insert_sector_batch(db_path, '2026-05-06', '2026-05-06T09:25:00+08:00', '0925', 4.0)
    insert_sector_batch(db_path, '2026-05-06', '2026-05-06T09:30:00+08:00', '0930', 8.0)
    patch_db_path(monkeypatch, db_path)

    boards = mod.fetch_strong_boards_from_db(
        limit=2,
        target_date='2026-05-06',
        asof_time=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
    )

    assert [b['name'] for b in boards] == ['半导体0925', '机器人0925']
    assert boards[0]['pct'] == 4.0
