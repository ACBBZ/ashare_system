import json
import sqlite3
from pathlib import Path

import pandas as pd

import ashare_shortline_schema as schema
import ashare_lhb_sidecar as lhb

REAL_SHORTLINE_DB = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')


def init_tmp_db(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    return db_path


def insert_limitup(conn, code='000001', name='甲股', source='limitup', theme='AI', broken=0):
    now = schema.now_iso()
    conn.execute(
        '''INSERT INTO limitup_daily (
            trade_date, code, name, theme, consecutive_board_count, is_broken_board, is_reseal,
            reason, source, raw_json, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, ?, 1, ?, 0, ?, ?, '{}', ?, ?)''',
        (code, name, theme, broken, theme, source, now, now),
    )


def insert_theme(conn, code='000001', name='甲股', theme='AI', score=90, broken_count=0, source='limitup'):
    now = schema.now_iso()
    theme_id = 'theme_' + theme
    conn.execute(
        '''INSERT INTO theme_daily (
            trade_date, theme_id, theme_name, status, score, limitup_count, broken_count,
            evidence_json, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, '主升', ?, 1, ?, '{}', ?, ?)
        ON CONFLICT(trade_date, theme_id) DO UPDATE SET score=excluded.score, broken_count=excluded.broken_count''',
        (theme_id, theme, score, broken_count, now, now),
    )
    conn.execute(
        '''INSERT INTO theme_stock_map (
            trade_date, theme_id, theme_name, code, name, role, evidence, confidence, source, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, ?, ?, '龙头', '{}', 0.9, ?, ?, ?)''',
        (theme_id, theme, code, name, source, now, now),
    )


def insert_new_high(conn, code='000001', name='甲股', high_type='100日新高'):
    now = schema.now_iso()
    conn.execute(
        '''INSERT INTO new_high_daily (
            trade_date, code, name, high_type, theme_name, source, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, ?, 'AI', 'mock/test', ?, ?)''',
        (code, name, high_type, now, now),
    )


def test_safe_float_money_parses_units_and_empty_values():
    assert lhb.safe_float_money('1.23亿') == 123000000.0
    assert lhb.safe_float_money('1234万') == 12340000.0
    assert lhb.safe_float_money(12345678) == 12345678.0
    assert lhb.safe_float_money(None) is None
    assert lhb.safe_float_money('') is None


def test_normalize_lhb_row_accepts_chinese_aliases_and_seats():
    row = {
        '股票代码': '000001',
        '股票简称': '平安银行',
        '龙虎榜净买额': '1234万',
        '机构买入净额': '100万',
        '上榜原因': '日涨幅偏离值达7%',
        '买入营业部': '机构专用',
        '卖出营业部': '华鑫证券上海分公司',
    }
    normalized = lhb.normalize_lhb_row(row, '2026-05-06', 'akshare_lhb_detail')
    assert normalized['code'] == '000001'
    assert normalized['name'] == '平安银行'
    assert normalized['net_buy'] == 12340000.0
    assert normalized['institution_net_buy'] == 1000000.0
    assert json.loads(normalized['buy_seats_json'])[0]['seat_name'] == '机构专用'
    assert normalized['quant_flag'] == 1
    assert normalized['interpretation'] == '日涨幅偏离值达7%'


def test_normalize_lhb_row_writes_none_for_missing_fields():
    normalized = lhb.normalize_lhb_row({'证券代码': '600000'}, '2026-05-06', 'mock/test')
    assert normalized['code'] == '600000'
    assert normalized['name'] is None
    assert normalized['net_buy'] is None
    assert normalized['institution_net_buy'] is None
    assert normalized['buy_seats_json'] == '[]'
    assert normalized['raw_json']


def test_classify_seat_type_recognizes_institution_quant_and_hot_money():
    assert lhb.classify_seat_type('机构专用')['institution_flag'] is True
    assert lhb.classify_seat_type('华鑫证券上海分公司')['quant_flag'] is True
    assert lhb.classify_seat_type('上海溧阳路')['known_hot_money_flag'] is True


def test_collect_lhb_data_uses_mocked_akshare_and_records_source_errors(monkeypatch):
    monkeypatch.setattr(lhb.ak, 'stock_lhb_detail_em', lambda date: pd.DataFrame([{'代码': '000001', '名称': '甲股', '净买额': '1万'}]))
    monkeypatch.setattr(lhb.ak, 'stock_lhb_stock_detail_em', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(lhb.ak, 'stock_lhb_stock_statistic_em', lambda **kwargs: pd.DataFrame())
    payload = lhb.collect_lhb_data('2026-05-06')
    assert payload['lhb_rows'][0]['source'] == 'akshare_lhb_detail'
    assert payload['source_errors']


def test_upsert_lhb_daily_is_idempotent(tmp_path):
    db_path = init_tmp_db(tmp_path)
    row = lhb.normalize_lhb_row({'代码': '000001', '名称': '甲股', '净买额': 1000}, '2026-05-06', 'mock/test')
    with schema.connect(db_path) as conn:
        lhb.upsert_lhb_daily(conn, [row])
        lhb.upsert_lhb_daily(conn, [row])
        count = conn.execute('SELECT COUNT(*) FROM lhb_daily').fetchone()[0]
    assert count == 1


def test_build_lhb_summary_outputs_net_buy_top(tmp_path):
    db_path = init_tmp_db(tmp_path)
    rows = [
        lhb.normalize_lhb_row({'代码': '000001', '名称': '甲股', '净买额': '2万'}, '2026-05-06', 'mock/test'),
        lhb.normalize_lhb_row({'代码': '000002', '名称': '乙股', '净买额': '-3万'}, '2026-05-06', 'mock/test'),
    ]
    with schema.connect(db_path) as conn:
        lhb.upsert_lhb_daily(conn, rows)
        summary = lhb.build_lhb_summary(conn, '2026-05-06')
    assert summary['net_buy_top'][0]['code'] == '000001'
    assert summary['net_sell_top'][0]['code'] == '000002'


def test_build_lhb_summary_detects_limitup_new_high_theme_and_negative_resonance(tmp_path):
    db_path = init_tmp_db(tmp_path)
    with schema.connect(db_path) as conn:
        insert_limitup(conn, code='000001', source='limitup')
        insert_limitup(conn, code='000002', name='炸板股', source='broken,strong', broken=1)
        insert_limitup(conn, code='000003', name='跌停股', source='downlimit')
        insert_theme(conn, code='000001', theme='AI', score=95)
        insert_theme(conn, code='000002', name='炸板股', theme='AI', score=95, broken_count=1, source='broken')
        insert_theme(conn, code='000003', name='跌停股', theme='风险题材', score=80, broken_count=1, source='downlimit')
        insert_new_high(conn, code='000001', high_type='250日新高')
        lhb.upsert_lhb_daily(conn, [
            lhb.normalize_lhb_row({'代码': '000001', '名称': '甲股', '净买额': '10万'}, '2026-05-06', 'mock/test'),
            lhb.normalize_lhb_row({'代码': '000002', '名称': '炸板股', '净买额': '-20万'}, '2026-05-06', 'mock/test'),
            lhb.normalize_lhb_row({'代码': '000003', '名称': '跌停股', '净买额': '-30万'}, '2026-05-06', 'mock/test'),
        ])
        summary = lhb.build_lhb_summary(conn, '2026-05-06')
    assert summary['lhb_limitup_resonance'][0]['code'] == '000001'
    assert summary['lhb_new_high_resonance'][0]['code'] == '000001'
    assert summary['lhb_theme_resonance'][0]['theme_name'] == 'AI'
    assert any(item['code'] == '000002' and 'broken' in item['reason'] for item in summary['negative_items'])
    assert any(item['code'] == '000003' and 'downlimit' in item['reason'] for item in summary['negative_items'])


def test_render_lhb_markdown_contains_fixed_sections_and_no_deterministic_advice():
    summary = {
        'trade_date': '2026-05-06', 'lhb_count': 1, 'net_buy_top': [], 'net_sell_top': [],
        'institution_net_buy_top': [], 'quant_flag_items': [], 'known_hot_money_items': [],
        'lhb_limitup_resonance': [], 'lhb_new_high_resonance': [], 'lhb_theme_resonance': [],
        'negative_items': [], 'missing_fields': {}, 'source_errors': {'detail': 'timeout'},
        'sources': ['mock/test'], 'generated_at': 'now'
    }
    md = lhb.render_lhb_markdown(summary)
    for section in ['# A 股龙虎榜 sidecar 复盘', '## 1. 总览', '## 2. 净买入 Top', '## 3. 净卖出 / 负反馈 Top', '## 4. 机构 / 量化 / 游资席位弱识别', '## 5. 龙虎榜 + 涨停共振', '## 6. 龙虎榜 + 新高共振', '## 7. 龙虎榜 + 主线题材共振', '## 8. 数据缺失说明', '## 9. 风险提示']:
        assert section in md
    assert 'timeout' in md
    assert '弱规则识别，可能不准确' in md
    assert not any(word in md for word in ['建议买入', '必须买入', '确定性机会', '无脑买入', '满仓'])


def test_run_lhb_sidecar_skip_fetch_missing_data_does_not_crash(tmp_path):
    db_path = init_tmp_db(tmp_path)
    result = lhb.run_lhb_sidecar('2026-05-06', db_path=db_path, output_root=tmp_path / 'out', skip_fetch=True, source_errors={'mock': 'empty'})
    assert result['summary']['lhb_count'] == 0
    assert Path(result['paths']['json_path']).exists()
    assert Path(result['paths']['markdown_path']).exists()
