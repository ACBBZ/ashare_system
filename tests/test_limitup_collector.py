import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import ashare_shortline_schema as schema
import ashare_limitup_collector as collector


REAL_SHORTLINE_DB = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')


def test_normalize_limitup_row_accepts_common_chinese_fields():
    row = {
        '代码': '000001',
        '名称': '平安银行',
        '涨停原因类别': '金融科技',
        '首次封板时间': '09:35:00',
        '最后封板时间': '14:50:00',
        '开板次数': '2',
        '封板资金': '123456789',
        '封成比': '12.5',
        '换手率': '8.6',
        '成交额': '987654321',
        '连板数': '3连板',
    }

    normalized = collector.normalize_limitup_row(row, '2026-05-06', 'limitup')

    assert normalized['trade_date'] == '2026-05-06'
    assert normalized['code'] == '000001'
    assert normalized['name'] == '平安银行'
    assert normalized['theme'] == '金融科技'
    assert normalized['reason'] == '金融科技'
    assert normalized['first_limit_time'] == '09:35:00'
    assert normalized['last_limit_time'] == '14:50:00'
    assert normalized['open_count'] == 2
    assert normalized['seal_amount'] == 123456789.0
    assert normalized['seal_ratio'] == 12.5
    assert normalized['turnover_rate'] == 8.6
    assert normalized['amount'] == 987654321.0
    assert normalized['consecutive_board_count'] == 3
    assert normalized['is_broken_board'] == 0
    assert normalized['source'] == 'limitup'
    assert json.loads(normalized['raw_json'])['名称'] == '平安银行'


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('首板', 1),
        ('3连板', 3),
        ('3天3板', 3),
        ('5天4板', 4),
        (None, None),
        ('', None),
    ],
)
def test_parse_consecutive_board_count(value, expected):
    assert collector.parse_consecutive_board_count(value) == expected


def test_normalize_limitup_row_writes_none_for_missing_fields():
    normalized = collector.normalize_limitup_row({'股票代码': '600000'}, '2026-05-06', 'strong')

    assert normalized['code'] == '600000'
    assert normalized['name'] is None
    assert normalized['theme'] is None
    assert normalized['first_limit_time'] is None
    assert normalized['seal_amount'] is None
    assert normalized['consecutive_board_count'] is None
    assert normalized['raw_json']


def test_mock_limitup_dataframe_can_write_limitup_daily(tmp_path, monkeypatch):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)

    monkeypatch.setattr(
        collector.ak,
        'stock_zt_pool_em',
        lambda date: pd.DataFrame([
            {'代码': '000001', '名称': '平安银行', '连板数': '2板', '封板资金': 1000, '涨停原因类别': '银行'},
        ]),
    )
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_dtgc_em', lambda date: pd.DataFrame())
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_zbgc_em', lambda date: pd.DataFrame())
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_strong_em', lambda date: pd.DataFrame())

    payload = collector.collect_limitup_data('2026-05-06')
    with schema.connect(db_path) as conn:
        collector.upsert_limitup_daily(conn, payload['limitup_rows'])
        rows = conn.execute('SELECT * FROM limitup_daily').fetchall()

    assert len(rows) == 1
    assert rows[0]['code'] == '000001'
    assert rows[0]['name'] == '平安银行'
    assert rows[0]['source'] == 'limitup'


def test_upsert_limitup_daily_is_idempotent_for_same_trade_date_and_code(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    row = collector.normalize_limitup_row({'代码': '000001', '名称': '平安银行'}, '2026-05-06', 'limitup')

    with schema.connect(db_path) as conn:
        collector.upsert_limitup_daily(conn, [row])
        collector.upsert_limitup_daily(conn, [row])
        count = conn.execute('SELECT COUNT(*) FROM limitup_daily').fetchone()[0]

    assert count == 1


def test_broken_pool_marks_is_broken_board(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    limitup_row = collector.normalize_limitup_row({'代码': '000001', '名称': '平安银行'}, '2026-05-06', 'limitup')
    broken_row = collector.normalize_limitup_row({'代码': '000001', '名称': '平安银行', '炸板次数': 1}, '2026-05-06', 'broken', broken=True)

    with schema.connect(db_path) as conn:
        collector.upsert_limitup_daily(conn, [limitup_row, broken_row])
        saved = conn.execute('SELECT is_broken_board, source, open_count FROM limitup_daily WHERE code = ?', ('000001',)).fetchone()

    assert saved['is_broken_board'] == 1
    assert 'broken' in saved['source']
    assert saved['open_count'] == 1


def test_build_limitup_summary_outputs_counts_ladder_and_missing_fields(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    rows = [
        collector.normalize_limitup_row({'代码': '000001', '名称': '平安银行', '连板数': '3连板', '封板资金': 2000, '涨停原因类别': '银行'}, '2026-05-06', 'limitup'),
        collector.normalize_limitup_row({'代码': '600000', '名称': '浦发银行', '连板数': '首板', '封板资金': 1000, '涨停原因类别': '银行'}, '2026-05-06', 'limitup'),
        collector.normalize_limitup_row({'代码': '000002', '名称': '万科A', '炸板次数': 2}, '2026-05-06', 'broken', broken=True),
        collector.normalize_limitup_row({'代码': '000003', '名称': '跌停股'}, '2026-05-06', 'downlimit'),
    ]
    with schema.connect(db_path) as conn:
        collector.upsert_limitup_daily(conn, rows)
        summary = collector.build_limitup_summary(
            conn,
            '2026-05-06',
            missing_fields={'limitup': ['first_limit_time']},
            source_errors={'broken': 'timeout'},
        )

    assert summary['zt_count'] == 2
    assert summary['dt_count'] == 1
    assert summary['broken_count'] == 1
    assert summary['max_consecutive_board'] == 3
    assert summary['ladder'][3] == ['平安银行(000001)']
    assert summary['missing_fields']['limitup'] == ['first_limit_time']
    assert summary['source_errors']['broken'] == 'timeout'


def test_render_limitup_markdown_contains_fixed_sections():
    summary = {
        'trade_date': '2026-05-06',
        'zt_count': 1,
        'dt_count': 0,
        'broken_count': 0,
        'max_consecutive_board': 1,
        'ladder': {1: ['平安银行(000001)']},
        'top_seal_amount': [{'code': '000001', 'name': '平安银行', 'seal_amount': 1000}],
        'top_reasons': [{'reason': '银行', 'count': 1, 'stocks': ['平安银行(000001)']}],
        'broken_stocks': [],
        'missing_fields': {},
        'source_errors': {},
        'sources': ['limitup'],
        'generated_at': '2026-05-06T09:40:00+08:00',
    }

    markdown = collector.render_limitup_markdown(summary)

    assert '# A 股涨停生态快照' in markdown
    for section in ['## 1. 总览', '## 2. 连板梯队', '## 3. 最大封板资金 Top 10', '## 4. 涨停原因分类', '## 5. 炸板/负反馈', '## 6. 数据缺失说明', '## 7. 风险提示']:
        assert section in markdown


def test_collect_limitup_data_records_source_errors_without_network(monkeypatch):
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_em', lambda date: pd.DataFrame([{'代码': '000001', '名称': '平安银行'}]))
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_dtgc_em', lambda date: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_zbgc_em', lambda date: pd.DataFrame())
    monkeypatch.setattr(collector.ak, 'stock_zt_pool_strong_em', lambda date: pd.DataFrame())

    payload = collector.collect_limitup_data('2026-05-06')

    assert len(payload['limitup_rows']) == 1
    assert 'downlimit' in payload['source_errors']
    assert 'boom' in payload['source_errors']['downlimit']
