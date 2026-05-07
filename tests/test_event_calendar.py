import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import ashare_shortline_schema as schema
import ashare_event_calendar as ev

REAL_SHORTLINE_DB = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')


def init_tmp_db(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    assert db_path != REAL_SHORTLINE_DB
    schema.init_db(db_path)
    return db_path


def insert_limitup(conn, code='000001', name='甲股', source='limitup', theme='AI'):
    now = schema.now_iso()
    conn.execute(
        '''INSERT INTO limitup_daily (
            trade_date, code, name, theme, consecutive_board_count, is_broken_board, is_reseal,
            reason, source, raw_json, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, ?, 1, 0, 0, ?, ?, '{}', ?, ?)''',
        (code, name, theme, theme, source, now, now),
    )


def insert_theme(conn, code='000001', name='甲股', theme='AI', score=95, broken_count=0):
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
        ) VALUES ('2026-05-06', ?, ?, ?, ?, '龙头', '{}', 0.9, 'limitup', ?, ?)''',
        (theme_id, theme, code, name, now, now),
    )


def insert_new_high(conn, code='000001', name='甲股', high_type='100日新高'):
    now = schema.now_iso()
    conn.execute(
        '''INSERT INTO new_high_daily (
            trade_date, code, name, high_type, theme_name, source, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, ?, 'AI', 'mock/test', ?, ?)''',
        (code, name, high_type, now, now),
    )


def insert_lhb(conn, code='000001', name='甲股'):
    now = schema.now_iso()
    conn.execute(
        '''INSERT INTO lhb_daily (
            trade_date, code, name, net_buy, institution_net_buy, buy_seats_json, sell_seats_json,
            known_hot_money_flag, quant_flag, interpretation, source, raw_json, created_at, updated_at
        ) VALUES ('2026-05-06', ?, ?, 1000, 0, '[]', '[]', 0, 0, '测试', 'mock/test', '{}', ?, ?)''',
        (code, name, now, now),
    )


def test_normalize_code_accepts_plain_and_prefixed_codes():
    assert ev.normalize_code('000001') == '000001'
    assert ev.normalize_code('SZ000001') == '000001'
    assert ev.normalize_code('sh.600000') == '600000'
    assert ev.normalize_code(None) is None


def test_normalize_event_date_accepts_common_formats_and_datetime():
    assert ev.normalize_event_date('2026-05-06') == '2026-05-06'
    assert ev.normalize_event_date('2026/05/06') == '2026-05-06'
    assert ev.normalize_event_date(datetime(2026, 5, 6, 12, 30)) == '2026-05-06'
    assert ev.normalize_event_date(None) is None


def test_classify_event_type_rules():
    cases = {
        '2026年一季度业绩预告预增': '业绩预告',
        '2025年度报告披露': '财报披露',
        '关于回购公司股份的公告': '回购',
        '控股股东增持计划': '增持',
        '高管减持股份计划': '减持',
        '限售股上市流通暨解禁公告': '解禁',
        '重大事项停牌公告': '停牌',
        '股票复牌公告': '复牌',
        '股票交易异常波动公告': '异常波动',
        '收到监管函并被立案调查': '监管风险',
        '普通董事会决议公告': '其他事件',
    }
    for title, expected in cases.items():
        assert ev.classify_event_type(title) == expected


def test_normalize_event_row_accepts_chinese_aliases():
    row = {'公告日期': '2026/05/06', '股票代码': '000001', '股票简称': '甲股', '公告标题': '关于回购公司股份的公告', '公告类型': '公告'}
    out = ev.normalize_event_row(row, None, 'manual_mock/test')
    assert out['event_date'] == '2026-05-06'
    assert out['code'] == '000001'
    assert out['name'] == '甲股'
    assert out['event_type'] == '回购'
    assert out['title'] == '关于回购公司股份的公告'
    assert out['source'] == 'manual_mock/test'
    assert json.loads(out['raw_json'])['raw']['股票简称'] == '甲股'


def test_normalize_event_row_missing_title_generates_conservative_title_and_records_missing():
    out = ev.normalize_event_row({'日期': '2026-05-06', '证券代码': '600000', '证券简称': '浦发银行', '类型': '公告'}, None, 'manual_mock/test')
    raw = json.loads(out['raw_json'])
    assert out['title'] == '其他事件-浦发银行'
    assert 'title' in raw['missing_fields']


def test_score_event_importance_positive_and_negative_rules():
    assert ev.score_event_importance({'event_type': '回购', 'title': '回购股份'})['expected_impact'] == '正向关注'
    assert ev.score_event_importance({'event_type': '增持', 'title': '股东增持'})['expected_impact'] == '正向关注'
    assert ev.score_event_importance({'event_type': '减持', 'title': '高管减持'})['expected_impact'] == '负向风险'
    assert ev.score_event_importance({'event_type': '监管风险', 'title': '处罚立案'})['expected_impact'] == '负向风险'
    assert ev.score_event_importance({'event_type': '解禁', 'title': '限售股解禁压力'})['expected_impact'] == '负向风险'


def test_collect_event_calendar_data_uses_mocked_sources_and_records_errors(monkeypatch):
    monkeypatch.setattr(ev.ak, 'stock_notice_report', lambda **kwargs: pd.DataFrame([{'公告日期': '2026-05-06', '代码': '000001', '名称': '甲股', '公告标题': '回购公告'}]), raising=False)
    for name in ['stock_yjyg_em', 'stock_hold_management_detail_em', 'stock_repurchase_em', 'stock_restricted_release_summary_em']:
        monkeypatch.setattr(ev.ak, name, lambda **kwargs: (_ for _ in ()).throw(RuntimeError('boom')), raising=False)
    payload = ev.collect_event_calendar_data('2026-05-06')
    assert payload['event_rows'][0]['source'] == 'akshare_notice'
    assert payload['source_errors']


def test_upsert_event_calendar_is_idempotent(tmp_path):
    db_path = init_tmp_db(tmp_path)
    row = ev.normalize_event_row({'日期': '2026-05-06', '代码': '000001', '名称': '甲股', '标题': '回购公告'}, None, 'manual_mock/test')
    with schema.connect(db_path) as conn:
        ev.upsert_event_calendar(conn, [row])
        ev.upsert_event_calendar(conn, [row])
        count = conn.execute('SELECT COUNT(*) FROM event_calendar').fetchone()[0]
    assert count == 1


def test_build_event_summary_detects_all_resonance_and_watchlist(tmp_path):
    db_path = init_tmp_db(tmp_path)
    with schema.connect(db_path) as conn:
        insert_theme(conn)
        insert_limitup(conn)
        insert_new_high(conn)
        insert_lhb(conn)
        ev.upsert_event_calendar(conn, [ev.normalize_event_row({'日期': '2026-05-06', '代码': '000001', '名称': '甲股', '标题': '关于回购公司股份的公告'}, None, 'manual_mock/test')])
        summary = ev.build_event_summary(conn, '2026-05-06')
    assert summary['high_importance_events'][0]['code'] == '000001'
    assert summary['event_theme_resonance'][0]['theme_name'] == 'AI'
    assert summary['event_limitup_resonance'][0]['code'] == '000001'
    assert summary['event_new_high_resonance'][0]['code'] == '000001'
    assert summary['event_lhb_resonance'][0]['code'] == '000001'
    assert summary['tomorrow_watchlist']
    assert not any(word in json.dumps(summary['tomorrow_watchlist'], ensure_ascii=False) for word in ['买入', '必涨', '必做'])


def test_render_event_markdown_sections_source_errors_and_no_deterministic_advice():
    summary = {
        'event_date': '2026-05-06', 'event_count': 0, 'high_importance_events': [],
        'positive_watch_events': [], 'negative_risk_events': [], 'event_theme_resonance': [],
        'event_limitup_resonance': [], 'event_new_high_resonance': [], 'event_lhb_resonance': [],
        'tomorrow_watchlist': [], 'missing_fields': {'event_calendar': ['notice_data']},
        'source_errors': {'akshare_notice': 'timeout'}, 'sources': [], 'generated_at': 'now'
    }
    md = ev.render_event_markdown(summary)
    for section in ['# A 股事件日历 sidecar 复盘', '## 1. 总览', '## 2. 高重要性事件', '## 3. 正向关注事件', '## 4. 负向风险事件', '## 5. 事件 + 主线题材共振', '## 6. 事件 + 涨停 / 新高 / 龙虎榜共振', '## 7. 明日观察清单', '## 8. 数据缺失说明', '## 9. 风险提示']:
        assert section in md
    assert '未获取到有效事件' in md
    assert 'timeout' in md
    assert not any(word in md for word in ['必涨', '必做', '确定性买入'])


def test_run_event_calendar_skip_fetch_missing_data_does_not_crash(tmp_path):
    db_path = init_tmp_db(tmp_path)
    result = ev.run_event_calendar('2026-05-06', db_path=db_path, output_root=tmp_path / 'out', skip_fetch=True, source_errors={'mock': 'empty'})
    assert result['summary']['event_count'] == 0
    assert Path(result['paths']['json_path']).exists()
    assert Path(result['paths']['markdown_path']).exists()
