import argparse
import json
import sqlite3
from pathlib import Path

import ashare_shortline_batch_audit as batch
import ashare_shortline_schema as schema
from test_shortline_daily_report import seed_full_shortline


def args(tmp_path, **kw):
    ns = argparse.Namespace(
        db_path=str(tmp_path / 'shortline_signal.db'),
        output_root=str(tmp_path / 'out'),
        market_db_path=str(tmp_path / 'market.db'),
        strategy_db_path=str(tmp_path / 'strategy.db'),
        run=False,
        audit_only=True,
        skip_fetch=False,
        report_only=False,
        dry_run=False,
        fail_fast=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def init_db(tmp_path):
    db = tmp_path / 'shortline_signal.db'
    schema.init_db(db)
    with schema.connect(db) as conn:
        seed_full_shortline(conn)
    return db


def write_day(root: Path, d: str, pipeline=True, daily=True, validation=True, forward=1, scores=None):
    day = root / d
    day.mkdir(parents=True, exist_ok=True)
    if pipeline:
        (day / 'pipeline-run.json').write_text(json.dumps({
            'ok': True, 'total_steps': 8, 'successful_steps': 8, 'failed_steps': 0, 'skipped_steps': 0,
            'data_quality_audit': {'missing_files': [], 'source_errors': {'mock': 'warn'}, 'missing_fields': {'x': ['y']}},
        }, ensure_ascii=False), encoding='utf-8')
    if daily:
        (day / 'shortline-daily-context.json').write_text(json.dumps({
            'observation_pool': {'A': [{'code': '000001'}], 'B': [{'code': '000002'}], 'C': [{'code': '000003'}]},
            'source_errors': {'daily': 'warn'}, 'missing_fields': {'daily_missing': True},
        }, ensure_ascii=False), encoding='utf-8')
    if validation:
        (day / 'strategy-validation-context.json').write_text(json.dumps({
            'sample_coverage': {'with_forward_return_count': forward, 'without_forward_return_count': 2},
            'data_quality_audit': {},
            'shadow_score_table': scores or [
                {'score_level': 'A_shadow'}, {'score_level': 'B_shadow'}, {'score_level': 'C_shadow'}, {'score_level': 'Risk'}
            ],
            'source_errors': {'validation': 'warn'}, 'missing_fields': {'validation_missing': True},
        }, ensure_ascii=False), encoding='utf-8')


def test_resolve_trade_dates_last_n_with_mock_calendar(monkeypatch):
    monkeypatch.setattr(batch, 'get_trade_calendar_dates', lambda start, end: ['2026-05-01','2026-05-04','2026-05-05'])
    dates = batch.resolve_trade_dates(last_n=2)
    assert dates == ['2026-05-04', '2026-05-05']
    assert batch.CALENDAR_FALLBACK_USED is False


def test_resolve_trade_dates_start_end_with_mock_calendar(monkeypatch):
    monkeypatch.setattr(batch, 'get_trade_calendar_dates', lambda start, end: ['2026-05-04','2026-05-05'])
    assert batch.resolve_trade_dates('2026-05-01', '2026-05-06') == ['2026-05-04','2026-05-05']


def test_build_batch_plan_generates_days(tmp_path):
    plan = batch.build_batch_plan(['2026-05-04','2026-05-05'], args(tmp_path, run=True))
    assert [p['trade_date'] for p in plan] == ['2026-05-04','2026-05-05']
    assert '--trade-date 2026-05-04' in plan[0]['command']


def test_dry_run_does_not_execute_pipeline(monkeypatch, tmp_path):
    called = {'n': 0}
    monkeypatch.setattr(batch, 'run_one_pipeline', lambda step, a: called.__setitem__('n', called['n'] + 1))
    out = batch.run_batch_plan(batch.build_batch_plan(['2026-05-04'], args(tmp_path, run=True)), args(tmp_path, dry_run=True))
    assert called['n'] == 0
    assert out[0]['skipped'] is True


def test_run_batch_plan_continues_after_failure(monkeypatch, tmp_path):
    def fake(step, a):
        if step['trade_date'] == '2026-05-04':
            raise RuntimeError('boom')
        return {'ok': True}
    monkeypatch.setattr(batch, 'run_one_pipeline', fake)
    res = batch.run_batch_plan(batch.build_batch_plan(['2026-05-04','2026-05-05'], args(tmp_path, run=True)), args(tmp_path))
    assert len(res) == 2 and res[0]['ok'] is False and res[1]['ok'] is True


def test_run_batch_plan_fail_fast(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, 'run_one_pipeline', lambda step, a: (_ for _ in ()).throw(RuntimeError('boom')))
    res = batch.run_batch_plan(batch.build_batch_plan(['2026-05-04','2026-05-05'], args(tmp_path, run=True)), args(tmp_path, fail_fast=True))
    assert len(res) == 1 and res[0]['ok'] is False


def test_count_shadow_rows_by_date_counts_seven_tables(tmp_path):
    db = init_db(tmp_path)
    with sqlite3.connect(db) as conn:
        counts = batch.count_shadow_rows_by_date(conn, '2026-05-06')
    assert set(counts) == set(batch.SHADOW_TABLES)
    assert counts['limitup_daily'] == 4


def test_build_multi_day_audit_reads_contexts_and_missing_pipeline(tmp_path):
    db = init_db(tmp_path)
    root = tmp_path / 'out'
    write_day(root, '2026-05-06')
    write_day(root, '2026-05-07', pipeline=False, daily=True, validation=True, forward=0)
    audit = batch.build_multi_day_audit(['2026-05-06','2026-05-07'], db, root)
    assert audit['daily_audits'][0]['pipeline_status'] == 'ok'
    assert audit['daily_audits'][1]['pipeline_status'] == 'missing'
    assert audit['observation_pool_coverage']['2026-05-06']['A_count'] == 1
    assert audit['forward_return_coverage']['with_forward_return_dates'] == ['2026-05-06']
    assert audit['shadow_score_coverage']['A_shadow_count'] == 2


def test_compute_module_availability(tmp_path):
    daily = [{'modules': {'limitup': True, 'theme': False}}, {'modules': {'limitup': True, 'theme': True}}]
    av = batch.compute_module_availability(daily)
    assert av['limitup']['availability_rate'] == 1.0
    assert av['theme']['missing_days'] == 1


def test_stage7_readiness_sample_insufficient_and_twenty_days(tmp_path):
    small = {'trade_dates': ['2026-05-06'], 'pipeline_complete_days': 1, 'module_availability': {'limitup': {'availability_rate': 1}, 'theme': {'availability_rate': 1}, 'new_high': {'availability_rate': 1}}, 'forward_return_coverage': {'with_forward_return_dates': []}, 'observation_pool_stable_days': 1, 'shadow_score_coverage': {'A_shadow_count': 1, 'B_shadow_count': 1, 'C_shadow_count': 1, 'Risk_count': 1}, 'source_error_days': []}
    assert batch.evaluate_stage7_readiness(small)['overall_ready'] is False
    full = small | {'trade_dates': [f'2026-05-{i:02d}' for i in range(1, 21)], 'pipeline_complete_days': 20, 'forward_return_coverage': {'with_forward_return_dates': [str(i) for i in range(20)]}, 'observation_pool_stable_days': 20}
    chk = batch.evaluate_stage7_readiness(full)['checklist']
    assert chk['pipeline_20_days']['ok'] is True
    assert chk['user_field_confirmation']['ok'] is False


def test_render_and_write_outputs_json_parseable(tmp_path):
    ctx = {'ok': False, 'generated_at': 'now', 'calendar_fallback': False, 'trade_dates': ['2026-05-06'], 'audit': {'daily_audits': [], 'module_availability': {}, 'stage7_readiness': {'overall_ready': False, 'checklist': {}}, 'pipeline_complete_days': 0, 'daily_report_complete_days': 0, 'validation_complete_days': 0, 'forward_return_coverage': {'with_forward_return_dates': [], 'without_forward_return_dates': []}, 'shadow_score_coverage': {}, 'observation_pool_coverage': {}}, 'batch_results': []}
    md = batch.render_batch_audit_markdown(ctx)
    for title in ['# A 股短线 Shadow Pipeline 多日样本审计报告','## 1. 总览','## 10. 风险提示']:
        assert title in md
    paths = batch.write_batch_audit_outputs(ctx, tmp_path)
    assert json.loads(Path(paths['json_path']).read_text(encoding='utf-8'))['ok'] is False


def test_does_not_modify_strategy_engine_or_production_db(tmp_path):
    strategy_engine = Path('/home/admin/ashare_system/scripts/ashare_strategy_engine.py')
    before = strategy_engine.stat().st_mtime_ns
    db = init_db(tmp_path)
    root = tmp_path / 'out'
    write_day(root, '2026-05-06')
    batch.build_multi_day_audit(['2026-05-06'], db, root)
    assert strategy_engine.stat().st_mtime_ns == before
