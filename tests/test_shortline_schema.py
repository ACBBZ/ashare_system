import sqlite3
from pathlib import Path

import ashare_shortline_schema as schema


EXPECTED_TABLE_COLUMNS = {
    'limitup_daily': {
        'trade_date', 'code', 'name', 'theme', 'first_limit_time', 'last_limit_time',
        'open_count', 'seal_amount', 'seal_ratio', 'turnover_rate', 'amount',
        'consecutive_board_count', 'is_broken_board', 'is_reseal', 'reason', 'source',
        'raw_json', 'created_at', 'updated_at',
    },
    'theme_daily': {
        'trade_date', 'theme_id', 'theme_name', 'parent_theme', 'status', 'score',
        'limitup_count', 'broken_count', 'leading_stock_code', 'leading_stock_name',
        'middle_stock_code', 'middle_stock_name', 'negative_stock_code',
        'negative_stock_name', 'evidence_json', 'created_at', 'updated_at',
    },
    'theme_stock_map': {
        'trade_date', 'theme_id', 'theme_name', 'code', 'name', 'role', 'evidence',
        'confidence', 'source', 'created_at', 'updated_at',
    },
    'emotion_anchors': {
        'trade_date', 'anchor_type', 'code', 'name', 'theme_name', 'status',
        'impact_score', 'note', 'source', 'created_at', 'updated_at',
    },
    'new_high_daily': {
        'trade_date', 'code', 'name', 'high_type', 'theme_name', 'sector_name',
        'amount', 'turnover_rate', 'position_20d', 'position_60d', 'position_100d',
        'source', 'created_at', 'updated_at',
    },
    'event_calendar': {
        'event_date', 'event_type', 'code', 'name', 'theme_name', 'title', 'importance',
        'expected_impact', 'source', 'raw_json', 'created_at', 'updated_at',
    },
    'lhb_daily': {
        'trade_date', 'code', 'name', 'net_buy', 'institution_net_buy', 'buy_seats_json',
        'sell_seats_json', 'known_hot_money_flag', 'quant_flag', 'interpretation',
        'source', 'raw_json', 'created_at', 'updated_at',
    },
}


def table_names(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def column_names(db_path: Path, table_name: str):
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table_name})')}
    finally:
        conn.close()


def test_init_db_creates_database_at_temporary_path(tmp_path):
    db_path = tmp_path / 'nested' / 'shortline_signal.db'

    result = schema.init_db(db_path)

    assert db_path.exists()
    assert result['db_path'] == str(db_path)
    assert set(result['tables']) == set(EXPECTED_TABLE_COLUMNS)


def test_init_db_creates_all_required_tables_and_columns(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'

    schema.init_db(db_path)

    assert table_names(db_path) == set(EXPECTED_TABLE_COLUMNS)
    for table_name, expected_columns in EXPECTED_TABLE_COLUMNS.items():
        assert expected_columns <= column_names(db_path, table_name)


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'

    first = schema.init_db(db_path)
    second = schema.init_db(db_path)

    assert first['db_path'] == second['db_path'] == str(db_path)
    assert table_names(db_path) == set(EXPECTED_TABLE_COLUMNS)


def test_show_tables_returns_schema_summary(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    schema.init_db(db_path)

    summary = schema.show_tables(db_path)

    assert set(summary) == set(EXPECTED_TABLE_COLUMNS)
    assert 'trade_date' in summary['limitup_daily']
    assert 'theme_id' in summary['theme_daily']
    assert 'event_date' in summary['event_calendar']
