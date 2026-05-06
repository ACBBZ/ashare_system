import json
import sqlite3
from pathlib import Path

import pandas as pd

import ashare_new_high_radar as radar
import ashare_shortline_schema as schema

REAL_SHORTLINE_DB = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')


def init_tmp_db(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    return db_path


def insert_limitup(conn, code='000001', name='测试股', source='limitup', theme='AI', broken=0, amount=1000):
    now = schema.now_iso()
    conn.execute(
        '''INSERT INTO limitup_daily (
            trade_date, code, name, theme, amount, turnover_rate, consecutive_board_count,
            is_broken_board, is_reseal, reason, source, raw_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)''',
        ('2026-05-06', code, name, theme, amount, 5.5, 2, broken, theme, source, '{}', now, now),
    )


def insert_theme(conn, code='000001', theme='AI', score=80, confidence=0.9, role='龙头'):
    now = schema.now_iso()
    theme_id = 'theme_' + theme
    conn.execute(
        '''INSERT INTO theme_daily (
            trade_date, theme_id, theme_name, status, score, limitup_count, broken_count,
            evidence_json, created_at, updated_at
        ) VALUES (?, ?, ?, '主升', ?, 1, 0, '{}', ?, ?)
        ON CONFLICT(trade_date, theme_id) DO UPDATE SET score=excluded.score''',
        ('2026-05-06', theme_id, theme, score, now, now),
    )
    conn.execute(
        '''INSERT INTO theme_stock_map (
            trade_date, theme_id, theme_name, code, name, role, evidence, confidence, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 'limitup', ?, ?)
        ON CONFLICT(trade_date, theme_id, code) DO UPDATE SET confidence=excluded.confidence''',
        ('2026-05-06', theme_id, theme, code, '测试股', role, confidence, now, now),
    )


def make_hist(days, today_high=None):
    dates = pd.date_range('2025-01-01', periods=days, freq='B')
    highs = list(range(10, 10 + days))
    lows = [h - 5 for h in highs]
    closes = [h - 1 for h in highs]
    if today_high is not None:
        highs[-1] = today_high
        closes[-1] = today_high - 1
    return pd.DataFrame({'date': dates.strftime('%Y-%m-%d'), 'high': highs, 'low': lows, 'close': closes})


def test_calculate_position_normal():
    assert radar.calculate_position(15, 10, 20) == 50.0


def test_calculate_position_zero_denominator_returns_none():
    assert radar.calculate_position(10, 10, 10) is None


def test_detect_high_type_60_day_high():
    hist = make_hist(80, today_high=1000)
    result = radar.detect_high_type(hist, '2026-05-06')
    assert result['high_type'] == '60日新高'
    assert result['high_60'] is True


def test_detect_high_type_100_day_high():
    hist = make_hist(130, today_high=1000)
    result = radar.detect_high_type(hist, '2026-05-06')
    assert result['high_type'] == '100日新高'
    assert result['high_100'] is True


def test_detect_high_type_250_day_high():
    hist = make_hist(280, today_high=1000)
    result = radar.detect_high_type(hist, '2026-05-06')
    assert result['high_type'] == '250日新高'
    assert result['high_250'] is True


def test_detect_high_type_insufficient_history():
    result = radar.detect_high_type(make_hist(30), '2026-05-06')
    assert result['high_type'] == '历史不足'
    assert result['insufficient_history'] is True


def test_load_shortline_universe_from_limitup_and_theme_map(tmp_path):
    db_path = init_tmp_db(tmp_path)
    with schema.connect(db_path) as conn:
        insert_limitup(conn, code='000001')
        insert_theme(conn, code='000002', theme='机器人')
        conn.commit()
        rows = radar.load_shortline_universe(conn, '2026-05-06')
    assert {r['code'] for r in rows} == {'000001', '000002'}


def test_resolve_theme_for_code_chooses_highest_theme_daily_score(tmp_path):
    db_path = init_tmp_db(tmp_path)
    with schema.connect(db_path) as conn:
        insert_theme(conn, code='000001', theme='低分', score=10, confidence=0.99)
        insert_theme(conn, code='000001', theme='高分', score=90, confidence=0.5)
        conn.commit()
        resolved = radar.resolve_theme_for_code(conn, '2026-05-06', '000001')
    assert resolved['theme_name'] == '高分'


def test_upsert_new_high_daily_idempotent(tmp_path):
    db_path = init_tmp_db(tmp_path)
    row = {'trade_date': '2026-05-06', 'code': '000001', 'name': '甲', 'high_type': '100日新高', 'source': 'mock/test'}
    with schema.connect(db_path) as conn:
        radar.upsert_new_high_daily(conn, [row])
        radar.upsert_new_high_daily(conn, [row | {'high_type': '250日新高'}])
        count = conn.execute('SELECT COUNT(*) FROM new_high_daily WHERE trade_date=? AND code=?', ('2026-05-06', '000001')).fetchone()[0]
        high_type = conn.execute('SELECT high_type FROM new_high_daily WHERE code=?', ('000001',)).fetchone()[0]
    assert count == 1
    assert high_type == '250日新高'


def test_build_summary_theme_resonance_and_limitup_new_high(tmp_path):
    db_path = init_tmp_db(tmp_path)
    with schema.connect(db_path) as conn:
        insert_limitup(conn, code='000001', theme='AI', source='limitup')
        insert_theme(conn, code='000001', theme='AI', score=90)
        radar.upsert_new_high_daily(conn, [{'trade_date': '2026-05-06', 'code': '000001', 'name': '甲', 'high_type': '100日新高', 'theme_name': 'AI', 'source': 'mock/test'}])
        summary = radar.build_new_high_summary(conn, '2026-05-06')
    assert summary['theme_resonance'][0]['theme_name'] == 'AI'
    assert summary['theme_resonance'][0]['new_high_count'] == 1
    assert summary['limitup_new_high'][0]['code'] == '000001'


def test_build_summary_detects_broken_risk(tmp_path):
    db_path = init_tmp_db(tmp_path)
    with schema.connect(db_path) as conn:
        insert_limitup(conn, code='000001', source='broken,strong', broken=1)
        radar.upsert_new_high_daily(conn, [{'trade_date': '2026-05-06', 'code': '000001', 'name': '炸板新高', 'high_type': '60日新高', 'theme_name': 'AI', 'source': 'mock/test'}])
        summary = radar.build_new_high_summary(conn, '2026-05-06')
    assert any('新高但炸板' in item['risk'] for item in summary['risk_items'])


def test_render_markdown_contains_sections_and_no_deterministic_advice():
    md = radar.render_new_high_markdown({
        'trade_date': '2026-05-06', 'total_checked': 1, 'new_high_60_count': 1,
        'new_high_100_count': 0, 'new_high_250_count': 0, 'insufficient_history_count': 0,
        'sources': ['mock/test'], 'generated_at': 'now', 'theme_resonance': [],
        'limitup_new_high': [], 'strong_theme_new_high': [], 'risk_items': [],
        'source_errors': {}, 'missing_fields': {},
    })
    for section in ['# A 股百日新高与题材共振雷达', '## 1. 总览', '## 2. 题材共振', '## 3. 新高 + 涨停共振', '## 4. 强题材趋势新高', '## 5. 风险项', '## 6. 数据缺失说明', '## 7. 风险提示']:
        assert section in md
    assert not any(word in md for word in ['建议买入', '必须买入', '确定性机会', '无脑买入', '满仓'])


def test_run_new_high_radar_missing_data_does_not_crash(tmp_path):
    db_path = init_tmp_db(tmp_path)
    result = radar.run_new_high_radar('2026-05-06', db_path=db_path, output_root=tmp_path / 'out', market_db_path=None, hist_fetcher=lambda code, start, end: pd.DataFrame(), source='mock/test')
    assert result['summary']['total_checked'] == 0
    assert Path(result['paths']['json_path']).exists()
