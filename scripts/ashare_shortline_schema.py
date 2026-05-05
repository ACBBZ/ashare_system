#!/usr/bin/env python3
"""Shortline enhanced signal schema utilities.

This module only owns the sidecar/shadow schema for the shortline upgrade.
It does not fetch market data, render reports, send Feishu messages, or modify
legacy production databases such as ``ashare_monitor.db`` and
``strategy_scoreboard.db``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
SHORTLINE_DIR = ROOT / 'shortline'
DB_PATH = SHORTLINE_DIR / 'shortline_signal.db'

TABLES: dict[str, str] = {
    'limitup_daily': '''
        CREATE TABLE IF NOT EXISTS limitup_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            theme TEXT,
            first_limit_time TEXT,
            last_limit_time TEXT,
            open_count INTEGER,
            seal_amount REAL,
            seal_ratio REAL,
            turnover_rate REAL,
            amount REAL,
            consecutive_board_count INTEGER,
            is_broken_board INTEGER,
            is_reseal INTEGER,
            reason TEXT,
            source TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        )
    ''',
    'theme_daily': '''
        CREATE TABLE IF NOT EXISTS theme_daily (
            trade_date TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            theme_name TEXT NOT NULL,
            parent_theme TEXT,
            status TEXT,
            score REAL,
            limitup_count INTEGER,
            broken_count INTEGER,
            leading_stock_code TEXT,
            leading_stock_name TEXT,
            middle_stock_code TEXT,
            middle_stock_name TEXT,
            negative_stock_code TEXT,
            negative_stock_name TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, theme_id)
        )
    ''',
    'theme_stock_map': '''
        CREATE TABLE IF NOT EXISTS theme_stock_map (
            trade_date TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            theme_name TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            role TEXT,
            evidence TEXT,
            confidence REAL,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, theme_id, code)
        )
    ''',
    'emotion_anchors': '''
        CREATE TABLE IF NOT EXISTS emotion_anchors (
            trade_date TEXT NOT NULL,
            anchor_type TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            theme_name TEXT,
            status TEXT,
            impact_score REAL,
            note TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, anchor_type, code)
        )
    ''',
    'new_high_daily': '''
        CREATE TABLE IF NOT EXISTS new_high_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            high_type TEXT NOT NULL,
            theme_name TEXT,
            sector_name TEXT,
            amount REAL,
            turnover_rate REAL,
            position_20d REAL,
            position_60d REAL,
            position_100d REAL,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code, high_type)
        )
    ''',
    'event_calendar': '''
        CREATE TABLE IF NOT EXISTS event_calendar (
            event_date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            code TEXT,
            name TEXT,
            theme_name TEXT,
            title TEXT NOT NULL,
            importance REAL,
            expected_impact TEXT,
            source TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (event_date, event_type, title, code)
        )
    ''',
    'lhb_daily': '''
        CREATE TABLE IF NOT EXISTS lhb_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            net_buy REAL,
            institution_net_buy REAL,
            buy_seats_json TEXT,
            sell_seats_json TEXT,
            known_hot_money_flag INTEGER,
            quant_flag INTEGER,
            interpretation TEXT,
            source TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        )
    ''',
}

INDEXES: tuple[str, ...] = (
    'CREATE INDEX IF NOT EXISTS idx_limitup_daily_trade_date ON limitup_daily(trade_date)',
    'CREATE INDEX IF NOT EXISTS idx_limitup_daily_theme ON limitup_daily(theme)',
    'CREATE INDEX IF NOT EXISTS idx_theme_daily_trade_date ON theme_daily(trade_date)',
    'CREATE INDEX IF NOT EXISTS idx_theme_stock_map_code ON theme_stock_map(code)',
    'CREATE INDEX IF NOT EXISTS idx_emotion_anchors_trade_date ON emotion_anchors(trade_date)',
    'CREATE INDEX IF NOT EXISTS idx_new_high_daily_trade_date ON new_high_daily(trade_date)',
    'CREATE INDEX IF NOT EXISTS idx_event_calendar_event_date ON event_calendar(event_date)',
    'CREATE INDEX IF NOT EXISTS idx_lhb_daily_trade_date ON lhb_daily(trade_date)',
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path).expanduser() if db_path is not None else DB_PATH


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path | None = None) -> dict:
    """Create shortline sidecar tables and indexes idempotently."""
    path = resolve_db_path(db_path)
    with connect(path) as conn:
        cur = conn.cursor()
        for create_sql in TABLES.values():
            cur.execute(create_sql)
        for index_sql in INDEXES:
            cur.execute(index_sql)
        conn.commit()
    return {
        'ok': True,
        'db_path': str(path),
        'tables': list(TABLES.keys()),
        'initialized_at': now_iso(),
    }


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [row['name'] for row in conn.execute(f'PRAGMA table_info({table_name})')]


def show_tables(db_path: str | Path | None = None) -> dict[str, list[str]]:
    """Return a table -> columns summary for the shortline sidecar database."""
    path = resolve_db_path(db_path)
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return {row['name']: _table_columns(conn, row['name']) for row in rows}


def print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Shortline enhanced signal schema utilities')
    parser.add_argument(
        '--db-path',
        default=str(DB_PATH),
        help=f'Shortline sidecar SQLite path. Default: {DB_PATH}',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('init', help='Create shortline sidecar tables idempotently')
    subparsers.add_parser('show-tables', help='Show existing shortline sidecar tables and columns')
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == 'init':
        print_json(init_db(args.db_path))
        return 0
    if args.command == 'show-tables':
        print_json({'db_path': str(resolve_db_path(args.db_path)), 'tables': show_tables(args.db_path)})
        return 0
    parser.error(f'unknown command: {args.command}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
