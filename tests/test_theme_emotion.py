import json
import sqlite3
from pathlib import Path

import pytest

import ashare_shortline_schema as schema
import ashare_theme_emotion as te


REAL_SHORTLINE_DB = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')


def init_tmp_db(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    return db_path


def insert_limitup_rows(db_path, rows):
    with schema.connect(db_path) as conn:
        now = schema.now_iso()
        for row in rows:
            payload = {
                'trade_date': row.get('trade_date', '2026-05-06'),
                'code': row['code'],
                'name': row.get('name'),
                'theme': row.get('theme'),
                'first_limit_time': row.get('first_limit_time'),
                'last_limit_time': row.get('last_limit_time'),
                'open_count': row.get('open_count'),
                'seal_amount': row.get('seal_amount'),
                'seal_ratio': row.get('seal_ratio'),
                'turnover_rate': row.get('turnover_rate'),
                'amount': row.get('amount'),
                'consecutive_board_count': row.get('consecutive_board_count'),
                'is_broken_board': row.get('is_broken_board', 0),
                'is_reseal': row.get('is_reseal', 0),
                'reason': row.get('reason'),
                'source': row.get('source', 'limitup'),
                'raw_json': json.dumps(row, ensure_ascii=False),
                'created_at': now,
                'updated_at': now,
            }
            conn.execute(
                '''INSERT INTO limitup_daily (
                    trade_date, code, name, theme, first_limit_time, last_limit_time,
                    open_count, seal_amount, seal_ratio, turnover_rate, amount,
                    consecutive_board_count, is_broken_board, is_reseal, reason, source,
                    raw_json, created_at, updated_at
                ) VALUES (
                    :trade_date, :code, :name, :theme, :first_limit_time, :last_limit_time,
                    :open_count, :seal_amount, :seal_ratio, :turnover_rate, :amount,
                    :consecutive_board_count, :is_broken_board, :is_reseal, :reason, :source,
                    :raw_json, :created_at, :updated_at
                )''',
                payload,
            )
        conn.commit()


def test_identifies_theme_from_theme_field(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [{'code': '000001', 'name': '甲', 'theme': '机器人', 'reason': '其他'}])

    with schema.connect(db_path) as conn:
        rows = te.load_limitup_rows(conn, '2026-05-06')
        themes = te.build_theme_stock_records(rows)

    assert themes[0]['theme_name'] == '机器人'
    assert themes[0]['confidence'] == 0.90


def test_identifies_theme_from_reason_field(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [{'code': '000001', 'name': '甲', 'reason': 'AI+算力/数据中心'}])

    with schema.connect(db_path) as conn:
        records = te.build_theme_stock_records(te.load_limitup_rows(conn, '2026-05-06'))

    names = {record['theme_name'] for record in records}
    assert {'AI', '算力', '数据中心'} <= names
    assert all(record['confidence'] == 0.75 for record in records)


def test_missing_theme_and_reason_falls_back_to_uncategorized(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [{'code': '000001', 'name': '甲'}])

    with schema.connect(db_path) as conn:
        records = te.build_theme_stock_records(te.load_limitup_rows(conn, '2026-05-06'))

    assert records[0]['theme_name'] == '未归类题材'
    assert records[0]['confidence'] == 0.30


def test_leading_role_uses_highest_consecutive_board(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [
        {'code': '000001', 'name': '一板股', 'theme': 'AI', 'consecutive_board_count': 1, 'source': 'limitup'},
        {'code': '000002', 'name': '三板股', 'theme': 'AI', 'consecutive_board_count': 3, 'source': 'limitup'},
    ])

    with schema.connect(db_path) as conn:
        records = te.assign_theme_roles(te.build_theme_stock_records(te.load_limitup_rows(conn, '2026-05-06')))

    by_code = {record['code']: record for record in records}
    assert by_code['000002']['role'] == '龙头'


def test_middle_role_uses_high_amount_stock(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [
        {'code': '000001', 'name': '龙头', 'theme': '芯片', 'consecutive_board_count': 3, 'amount': 1000, 'source': 'limitup'},
        {'code': '000002', 'name': '大成交', 'theme': '芯片', 'consecutive_board_count': 1, 'amount': 999999, 'source': 'strong'},
    ])

    with schema.connect(db_path) as conn:
        records = te.assign_theme_roles(te.build_theme_stock_records(te.load_limitup_rows(conn, '2026-05-06')))

    by_code = {record['code']: record for record in records}
    assert by_code['000002']['role'] == '中军'


def test_negative_feedback_role_for_broken_or_downlimit(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [
        {'code': '000001', 'name': '炸板', 'theme': 'AI', 'source': 'broken,strong', 'is_broken_board': 1},
        {'code': '000002', 'name': '跌停', 'theme': 'AI', 'source': 'downlimit'},
    ])

    with schema.connect(db_path) as conn:
        records = te.assign_theme_roles(te.build_theme_stock_records(te.load_limitup_rows(conn, '2026-05-06')))

    assert {record['role'] for record in records} == {'负反馈'}


def test_theme_daily_generates_score_and_status(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [
        {'code': '000001', 'name': '龙头', 'theme': 'AI', 'consecutive_board_count': 3, 'seal_amount': 10000000, 'amount': 20000000, 'source': 'limitup'},
        {'code': '000002', 'name': '补涨', 'theme': 'AI', 'consecutive_board_count': 1, 'source': 'limitup'},
        {'code': '000003', 'name': '炸板', 'theme': 'AI', 'source': 'broken', 'is_broken_board': 1},
    ])

    result = te.analyze_theme_emotion('2026-05-06', db_path=db_path, output_root=tmp_path / 'out')

    assert result['theme_daily']
    theme = result['theme_daily'][0]
    assert theme['score'] > 0
    assert theme['status'] in {'主升', '修复', '轮动', '分歧', '退潮', '未确认'}


def test_emotion_anchors_generate_space_board_and_broken_feedback(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [
        {'code': '000001', 'name': '空间板', 'theme': 'AI', 'consecutive_board_count': 5, 'seal_amount': 1000, 'source': 'limitup'},
        {'code': '000002', 'name': '炸板股', 'theme': 'AI', 'amount': 9000, 'source': 'broken', 'is_broken_board': 1},
    ])

    result = te.analyze_theme_emotion('2026-05-06', db_path=db_path, output_root=tmp_path / 'out')
    anchors = {(row['anchor_type'], row['code']) for row in result['emotion_anchors']}

    assert ('空间板', '000001') in anchors
    assert ('炸板负反馈', '000002') in anchors


def test_repeated_run_does_not_duplicate_rows(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [
        {'code': '000001', 'name': '空间板', 'theme': 'AI', 'consecutive_board_count': 5, 'source': 'limitup'},
        {'code': '000002', 'name': '炸板股', 'theme': 'AI', 'source': 'broken', 'is_broken_board': 1},
    ])

    te.analyze_theme_emotion('2026-05-06', db_path=db_path, output_root=tmp_path / 'out')
    te.analyze_theme_emotion('2026-05-06', db_path=db_path, output_root=tmp_path / 'out')

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM theme_stock_map').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM theme_daily').fetchone()[0] == 1
        anchor_count = conn.execute('SELECT COUNT(*) FROM emotion_anchors').fetchone()[0]
        assert anchor_count >= 2
    finally:
        conn.close()


def test_markdown_contains_fixed_sections_and_no_deterministic_recommendation(tmp_path):
    db_path = init_tmp_db(tmp_path)
    insert_limitup_rows(db_path, [{'code': '000001', 'name': '甲', 'theme': 'AI', 'source': 'limitup'}])

    result = te.analyze_theme_emotion('2026-05-06', db_path=db_path, output_root=tmp_path / 'out')
    markdown = Path(result['paths']['markdown_path']).read_text(encoding='utf-8')

    for section in [
        '# A 股题材与情绪锚点快照',
        '## 1. 今日主线题材 Top 5',
        '## 2. 题材股票映射',
        '## 3. 情绪锚点',
        '## 4. 主线状态判断',
        '## 5. 明日观察点',
        '## 6. 数据缺失说明',
        '## 7. 风险提示',
    ]:
        assert section in markdown
    forbidden = ['建议买入', '必须买入', '确定性机会', '无脑买入', '满仓']
    assert not any(word in markdown for word in forbidden)


def test_missing_data_does_not_crash(tmp_path):
    db_path = init_tmp_db(tmp_path)
    result = te.analyze_theme_emotion('2026-05-06', db_path=db_path, output_root=tmp_path / 'out', market_db_path=None)

    assert result['theme_daily'] == []
    assert Path(result['paths']['json_path']).exists()
    assert result['summary']['data_notes']['sector_mapping'] == '板块成分数据不可用'
