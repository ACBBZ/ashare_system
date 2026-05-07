import json
import os
import sqlite3
from pathlib import Path

import ashare_shortline_schema as schema
import ashare_shortline_daily_report as report

REAL_MARKET_DB = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
DETERMINISTIC_FORBIDDEN = ['必涨', '推荐买入', '满仓', '梭哈', '必做']
POOL_FORBIDDEN = ['买入', *DETERMINISTIC_FORBIDDEN]


def init_db(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    schema.init_db(db_path)
    return db_path


def now():
    return schema.now_iso()


def seed_full_shortline(conn):
    n = now()
    # limitup: strong leader, broken risk, downlimit risk
    rows = [
        ('2026-05-06','000001','甲龙头','AI',5,0,0,500000000,'AI+算力','limitup',n,n),
        ('2026-05-06','000002','乙中军','AI',2,0,0,260000000,'AI+应用','limitup',n,n),
        ('2026-05-06','000003','丙炸板','AI',1,1,0,80000000,'AI炸板','broken',n,n),
        ('2026-05-06','000004','丁跌停','光伏',0,0,0,0,'负反馈','downlimit',n,n),
    ]
    conn.executemany('''INSERT INTO limitup_daily (trade_date,code,name,theme,consecutive_board_count,is_broken_board,is_reseal,seal_amount,reason,source,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
    themes = [
        ('2026-05-06','ai','AI','主升',92,2,1,'000001','甲龙头','000002','乙中军','000003','丙炸板','{}',n,n),
        ('2026-05-06','robot','机器人','修复',75,1,0,'000005','戊补涨',None,None,None,None,'{}',n,n),
        ('2026-05-06','solar','光伏','退潮',45,0,3,None,None,None,None,'000004','丁跌停','{}',n,n),
    ]
    conn.executemany('''INSERT INTO theme_daily (trade_date,theme_id,theme_name,status,score,limitup_count,broken_count,leading_stock_code,leading_stock_name,middle_stock_code,middle_stock_name,negative_stock_code,negative_stock_name,evidence_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', themes)
    maps = [
        ('2026-05-06','ai','AI','000001','甲龙头','龙头','{}',0.95,'limitup',n,n),
        ('2026-05-06','ai','AI','000002','乙中军','中军','{}',0.9,'limitup',n,n),
        ('2026-05-06','ai','AI','000003','丙炸板','负反馈','{}',0.8,'broken',n,n),
        ('2026-05-06','robot','机器人','000005','戊补涨','补涨','{}',0.75,'strong',n,n),
        ('2026-05-06','solar','光伏','000004','丁跌停','负反馈','{}',0.8,'downlimit',n,n),
    ]
    conn.executemany('''INSERT INTO theme_stock_map (trade_date,theme_id,theme_name,code,name,role,evidence,confidence,source,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''', maps)
    anchors = [
        ('2026-05-06','空间板','000001','甲龙头','AI','正向',90,'5板空间打开','theme_emotion',n,n),
        ('2026-05-06','炸板负反馈','000003','丙炸板','AI','负向',-70,'炸板负反馈','theme_emotion',n,n),
    ]
    conn.executemany('''INSERT INTO emotion_anchors (trade_date,anchor_type,code,name,theme_name,status,impact_score,note,source,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''', anchors)
    highs = [
        ('2026-05-06','000001','甲龙头','250日新高','AI',100000000,90,'mock',n,n),
        ('2026-05-06','000002','乙中军','100日新高','AI',90000000,85,'mock',n,n),
        ('2026-05-06','000005','戊补涨','60日新高','机器人',70000000,80,'mock',n,n),
        ('2026-05-06','000003','丙炸板','100日新高','AI',60000000,70,'mock',n,n),
    ]
    conn.executemany('''INSERT INTO new_high_daily (trade_date,code,name,high_type,theme_name,amount,position_100d,source,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)''', highs)
    lhbs = [
        ('2026-05-06','000001','甲龙头',30000000,5000000,'[]','[]',0,1,'净买入','mock', '{}',n,n),
        ('2026-05-06','000003','丙炸板',-20000000,-1000000,'[]','[]',1,0,'净卖出','mock', '{}',n,n),
    ]
    conn.executemany('''INSERT INTO lhb_daily (trade_date,code,name,net_buy,institution_net_buy,buy_seats_json,sell_seats_json,known_hot_money_flag,quant_flag,interpretation,source,raw_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', lhbs)
    events = [
        ('2026-05-06','回购','000001','甲龙头','AI','关于回购公司股份的公告',85,'正向关注','manual_mock/test','{}',n,n),
        ('2026-05-06','减持','000004','丁跌停','光伏','股东减持计划',90,'负向风险','manual_mock/test','{}',n,n),
    ]
    conn.executemany('''INSERT INTO event_calendar (event_date,event_type,code,name,theme_name,title,importance,expected_impact,source,raw_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', events)
    conn.commit()


def test_load_shortline_tables_reads_all_shadow_tables(tmp_path):
    db_path = init_db(tmp_path)
    with schema.connect(db_path) as conn:
        seed_full_shortline(conn)
        data = report.load_shortline_tables(conn, '2026-05-06')
    for key in ['limitup_daily','theme_daily','theme_stock_map','emotion_anchors','new_high_daily','lhb_daily','event_calendar']:
        assert key in data
        assert isinstance(data[key], list)
    assert data['limitup_daily']


def test_empty_shadow_tables_do_not_crash_and_status_marks_missing(tmp_path):
    db_path = init_db(tmp_path)
    with schema.connect(db_path) as conn:
        data = report.load_shortline_tables(conn, '2026-05-06')
    status = report.build_data_status(data, {'available': False})
    assert status['涨停生态']['status'] == 'missing'
    assert status['市场快照']['status'] == 'missing'


def test_derive_market_regime_main_rise_divergence_and_ebb():
    assert report.derive_market_regime({'limitup_ecology': {'limitup_count': 45, 'broken_count': 2, 'downlimit_count': 1, 'max_board': 5}, 'themes': [{'score': 92, 'broken_count': 0}], 'new_high': {'new_high_theme_resonance_count': 5}, 'emotion_anchors': [{'impact_score': 80}]})['regime'] == '主升'
    assert report.derive_market_regime({'limitup_ecology': {'limitup_count': 35, 'broken_count': 18, 'downlimit_count': 3, 'max_board': 3}, 'themes': [{'score': 85, 'broken_count': 5}], 'emotion_anchors': [{'impact_score': -40}]})['regime'] == '分歧'
    assert report.derive_market_regime({'limitup_ecology': {'limitup_count': 10, 'broken_count': 20, 'downlimit_count': 12, 'max_board': 1}, 'themes': [{'score': 45, 'broken_count': 8}], 'lhb': {'negative_count': 5}})['regime'] == '退潮'


def test_section_builders_output_required_resonance(tmp_path):
    db_path = init_db(tmp_path)
    with schema.connect(db_path) as conn:
        seed_full_shortline(conn)
        data = report.load_shortline_tables(conn, '2026-05-06')
    assert report.build_limitup_section(data)['limitup_count'] == 2
    assert report.build_limitup_section(data)['broken_count'] == 1
    assert report.build_limitup_section(data)['max_board'] == 5
    assert report.build_theme_section(data)[0]['theme_name'] == 'AI'
    assert report.build_emotion_section(data)[0]['anchor_type'] == '空间板'
    assert report.build_new_high_section(data)['new_high_limitup_resonance']
    assert report.build_lhb_section(data)['lhb_limitup_resonance']
    assert report.build_event_section(data)['event_theme_resonance']


def test_observation_pool_generates_a_b_c_layers_without_forbidden_advice(tmp_path):
    db_path = init_db(tmp_path)
    with schema.connect(db_path) as conn:
        seed_full_shortline(conn)
        data = report.load_shortline_tables(conn, '2026-05-06')
        context = report.build_context(conn, '2026-05-06', data, {'available': False}, db_path, None, {})
        pool = report.build_observation_pool(conn, '2026-05-06', context)
    assert pool['A'] and pool['B'] and pool['C']
    text = json.dumps(pool, ensure_ascii=False)
    assert not any(w in text for w in POOL_FORBIDDEN)


def test_render_markdown_has_fixed_sections_missing_and_source_errors(tmp_path):
    db_path = init_db(tmp_path)
    with schema.connect(db_path) as conn:
        data = report.load_shortline_tables(conn, '2026-05-06')
        context = report.build_context(conn, '2026-05-06', data, {'available': False}, db_path, None, {'mock': 'source failed'})
    md = report.render_shortline_daily_markdown(context)
    for section in ['# A 股短线综合日报', '## 0. 数据时间信息', '## 1. 市场环境', '## 2. 涨停生态', '## 3. 主线题材', '## 4. 情绪锚点', '## 5. 百日新高与趋势共振', '## 6. 龙虎榜与资金席位', '## 7. 事件日历', '## 8. 候选观察池', '## 9. 综合判断', '## 10. 数据缺失说明', '## 11. 风险提示']:
        assert section in md
    assert '数据缺失说明' in md
    assert 'source failed' in md
    assert not any(w in md for w in DETERMINISTIC_FORBIDDEN)


def test_run_report_writes_parseable_json_and_does_not_modify_market_db(tmp_path):
    db_path = init_db(tmp_path)
    market_db = tmp_path / 'ashare_monitor.db'
    con = sqlite3.connect(market_db)
    con.execute('CREATE TABLE index_snapshots (trade_date TEXT, index_name TEXT, pct REAL, amount REAL)')
    con.execute("INSERT INTO index_snapshots VALUES ('2026-05-06','上证指数',1.2,500000000000)")
    con.commit(); con.close()
    before = market_db.stat().st_mtime_ns
    with schema.connect(db_path) as conn:
        seed_full_shortline(conn)
    result = report.run_shortline_daily_report('2026-05-06', db_path=db_path, market_db_path=market_db, output_root=tmp_path / 'out')
    after = market_db.stat().st_mtime_ns
    assert before == after
    payload = json.loads(Path(result['paths']['json_path']).read_text(encoding='utf-8'))
    assert payload['ok'] is True
    assert Path(result['paths']['markdown_path']).exists()
    assert payload['observation_pool']['A']


def test_no_akshare_imported_or_network_used():
    assert 'akshare' not in report.__dict__
