import json
import sqlite3
from pathlib import Path

import ashare_shortline_schema as schema
import ashare_shortline_signal_validation as val
from test_shortline_daily_report import seed_full_shortline

FORBIDDEN = ['买入', '必涨', '推荐买入', '满仓', '梭哈']


def init_shortline(tmp_path):
    db_path = tmp_path / 'shortline_signal.db'
    schema.init_db(db_path)
    with schema.connect(db_path) as conn:
        seed_full_shortline(conn)
    return db_path


def sample_shortline_context():
    return {
        'trade_date': '2026-05-06',
        'observation_pool': {
            'A': [{'code': '000001', 'name': '甲龙头', 'theme_name': 'AI', 'layer': 'A', 'reasons': ['Top题材AI / 龙头', '涨停', '100/250日新高', '龙虎榜正反馈', '高重要性正向事件']}],
            'B': [{'code': '000005', 'name': '戊补涨', 'theme_name': '机器人', 'layer': 'B', 'reasons': ['Top题材机器人 / 补涨', '60日新高']}],
            'C': [{'code': '000003', 'name': '丙炸板', 'theme_name': 'AI', 'layer': 'C', 'reasons': ['炸板负反馈']}],
        },
        'source_errors': {'mock': 'source failed'},
    }


def write_context(root: Path, trade_date='2026-05-06'):
    day = root / trade_date
    day.mkdir(parents=True)
    p = day / 'shortline-daily-context.json'
    p.write_text(json.dumps(sample_shortline_context(), ensure_ascii=False), encoding='utf-8')
    return p


def test_introspect_strategy_scoreboard_missing_db_does_not_crash(tmp_path):
    result = val.introspect_strategy_scoreboard(tmp_path / 'missing.db')
    assert result['available'] is False
    assert result['forward_return_available'] is False


def test_introspect_strategy_scoreboard_detects_forward_return_fields(tmp_path):
    db = tmp_path / 'strategy_scoreboard.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE candidate_returns (trade_date TEXT, code TEXT, forward_1d REAL, forward_3d REAL, forward_5d REAL, forward_10d REAL, forward_20d REAL, max_adverse REAL)')
    con.commit(); con.close()
    result = val.introspect_strategy_scoreboard(db)
    assert result['available'] is True
    assert result['forward_return_available'] is True
    assert 'candidate_returns' in result['tables']


def test_extract_observation_pool_reads_abc_layers():
    rows = val.extract_observation_pool(sample_shortline_context())
    assert [r['layer'] for r in rows] == ['A', 'B', 'C']
    assert rows[0]['code'] == '000001'


def test_build_signal_matrix_merges_all_sidecar_signals(tmp_path):
    db = init_shortline(tmp_path)
    out = tmp_path / 'out'
    write_context(out)
    inputs = val.load_validation_inputs(db, tmp_path / 'missing_strategy.db', tmp_path / 'missing_market.db', '2026-05-06', output_root=out)
    rows = val.build_signal_matrix(inputs)
    by_code = {r['code']: r for r in rows}
    assert by_code['000001']['is_top_theme'] is True
    assert by_code['000001']['is_limitup'] is True
    assert by_code['000001']['new_high_type'] == '250日新高'
    assert by_code['000001']['lhb_net_buy_positive'] is True
    assert by_code['000001']['positive_event'] is True
    assert by_code['000001']['emotion_anchor'] is True


def test_attach_forward_returns_without_returns_does_not_crash():
    rows = [{'code': '000001', 'trade_date': '2026-05-06'}]
    attached = val.attach_forward_returns(rows, {'forward_return_available': False}, {'available': False})
    assert attached[0]['has_forward_return'] is False


def test_summarize_layer_performance_outputs_abc_stats():
    rows = [
        {'layer': 'A', 'forward_1d': 0.03, 'has_forward_return': True},
        {'layer': 'A', 'forward_1d': -0.01, 'has_forward_return': True},
        {'layer': 'B', 'has_forward_return': False},
        {'layer': 'C', 'forward_1d': -0.05, 'has_forward_return': True},
    ]
    perf = val.summarize_layer_performance(rows)
    assert perf['A']['sample_count'] == 2
    assert perf['A']['win_rate_1d'] == 0.5
    assert perf['B']['note'] == '暂无足够后续表现数据'


def test_summarize_combo_performance_outputs_required_combos(tmp_path):
    db = init_shortline(tmp_path); out = tmp_path / 'out'; write_context(out)
    inputs = val.load_validation_inputs(db, tmp_path / 's.db', tmp_path / 'm.db', '2026-05-06', output_root=out)
    combos = val.summarize_combo_performance(val.build_signal_matrix(inputs))
    for key in ['Top题材+涨停', 'Top题材+100/250日新高', 'Top题材+龙虎榜', 'Top题材+正向事件', '涨停+新高', '涨停+龙虎榜', '新高+龙虎榜', '事件+龙虎榜', '情绪锚点+Top题材']:
        assert key in combos
    assert combos['Top题材+涨停']['sample_count'] >= 1


def test_summarize_negative_feedback_detects_risk_types(tmp_path):
    db = init_shortline(tmp_path); out = tmp_path / 'out'; write_context(out)
    inputs = val.load_validation_inputs(db, tmp_path / 's.db', tmp_path / 'm.db', '2026-05-06', output_root=out)
    neg = val.summarize_negative_feedback(val.build_signal_matrix(inputs), inputs)
    assert neg['炸板']['sample_count'] >= 1
    assert neg['跌停']['sample_count'] >= 1
    assert neg['负向事件']['sample_count'] >= 1
    assert neg['龙虎榜净卖出']['sample_count'] >= 1


def test_compute_shadow_score_adds_positive_factors():
    candidate = {'is_top_theme': True, 'role': '龙头', 'is_limitup': True, 'consecutive_board_count': 4, 'new_high_type': '250日新高', 'lhb_net_buy_positive': True, 'institution_net_buy_positive': True, 'positive_event': True, 'emotion_anchor': True}
    scored = val.compute_shadow_score(candidate, {})
    assert scored['score'] >= 70
    assert scored['score_level'] == 'A_shadow'
    assert any('Top题材' in f for f in scored['factors'])


def test_compute_shadow_score_penalizes_negative_factors_and_major_risk():
    candidate = {'is_broken': True, 'is_downlimit': True, 'negative_event': True, 'lhb_net_sell': True, 'same_theme_big_loss': True}
    scored = val.compute_shadow_score(candidate, {})
    assert scored['score_level'] == 'Risk'
    assert scored['score'] < 30
    assert scored['penalties']


def test_build_shadow_score_table_outputs_score_factors_penalties(tmp_path):
    db = init_shortline(tmp_path); out = tmp_path / 'out'; write_context(out)
    inputs = val.load_validation_inputs(db, tmp_path / 's.db', tmp_path / 'm.db', '2026-05-06', output_root=out)
    table = val.build_shadow_score_table(val.build_signal_matrix(inputs))
    assert {'score', 'factors', 'penalties'} <= set(table[0])


def test_render_strategy_validation_markdown_has_fixed_sections_and_no_deterministic_advice(tmp_path):
    db = init_shortline(tmp_path); out = tmp_path / 'out'; write_context(out)
    context = val.build_validation_context(val.load_validation_inputs(db, tmp_path / 's.db', tmp_path / 'm.db', '2026-05-06', output_root=out))
    md = val.render_strategy_validation_markdown(context)
    for section in ['# A 股短线信号验证报告', '## 0. 数据时间信息', '## 1. 样本覆盖情况', '## 2. A/B/C 观察池验证', '## 3. 共振组合分析', '## 4. 负反馈分析', '## 5. Shadow Score 建议', '## 6. 观察池门槛建议', '## 7. 不建议纳入正式策略的信号', '## 8. 后续验证计划', '## 9. 数据缺失说明', '## 10. 风险提示']:
        assert section in md
    assert not any(word in md for word in FORBIDDEN)
    assert '数据缺失说明' in md
    assert 'source failed' in md


def test_run_strategy_validation_writes_parseable_json_and_does_not_modify_db_or_strategy_engine(tmp_path):
    db = init_shortline(tmp_path); out = tmp_path / 'out'; write_context(out)
    strategy_db = tmp_path / 'strategy_scoreboard.db'
    con = sqlite3.connect(strategy_db); con.execute('CREATE TABLE candidate_returns (trade_date TEXT, code TEXT, forward_1d REAL)'); con.commit(); con.close()
    before_strategy_db = strategy_db.stat().st_mtime_ns
    engine_path = Path('/home/admin/ashare_system/scripts/ashare_strategy_engine.py')
    before_engine = engine_path.stat().st_mtime_ns
    result = val.run_strategy_validation('2026-05-06', db_path=db, strategy_db_path=strategy_db, market_db_path=tmp_path / 'm.db', output_root=out)
    assert strategy_db.stat().st_mtime_ns == before_strategy_db
    assert engine_path.stat().st_mtime_ns == before_engine
    payload = json.loads(Path(result['paths']['json_path']).read_text(encoding='utf-8'))
    assert payload['ok'] is True
    assert Path(result['paths']['markdown_path']).exists()


def test_no_akshare_imported_or_network_used():
    assert 'akshare' not in val.__dict__
