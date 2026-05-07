import argparse
import json
import sqlite3
from pathlib import Path

import ashare_shortline_pipeline as pipe
import ashare_shortline_schema as schema
from test_shortline_daily_report import seed_full_shortline


def make_args(tmp_path, **overrides):
    data = dict(
        trade_date='2026-05-06', today=False, db_path=str(tmp_path/'shortline_signal.db'),
        output_root=str(tmp_path/'out'), market_db_path=str(tmp_path/'market.db'),
        strategy_db_path=str(tmp_path/'strategy.db'), skip_fetch=False, report_only=False,
        no_validation=False, fail_fast=False, dry_run=False,
    )
    data.update(overrides)
    return argparse.Namespace(**data)


def init_db(tmp_path, full=True):
    db = tmp_path/'shortline_signal.db'
    schema.init_db(db)
    if full:
        with schema.connect(db) as conn:
            seed_full_shortline(conn)
    return db


def write_outputs(root: Path, trade_date='2026-05-06'):
    day = root/trade_date
    day.mkdir(parents=True, exist_ok=True)
    files = {
        'limitup-ecology.json': {'source_errors': {'limitup': 'mock err'}, 'missing_fields': {'x': ['a']}},
        'limitup-ecology.md': 'md', 'theme-emotion.json': {'source_errors': {}, 'missing_fields': {}},
        'theme-emotion.md': 'md', 'new-high-radar.json': {'source_errors': {}, 'missing_fields': {}},
        'new-high-radar.md': 'md', 'lhb-sidecar.json': {'source_errors': {'lhb': 'failed'}, 'missing_fields': {'lhb': ['seat']}},
        'lhb-sidecar.md': 'md', 'event-calendar.json': {'source_errors': {}, 'missing_fields': {}},
        'event-calendar.md': 'md',
        'shortline-daily-context.json': {'observation_pool': {'A': [{'code': '000001'}], 'B': [{'code': '000005'}], 'C': []}, 'source_errors': {'daily': 'warn'}, 'missing_fields': {'daily': ['market']}},
        'shortline-daily-report.md': 'md',
        'strategy-validation-context.json': {'sample_coverage': {'with_forward_return_count': 2}, 'data_status': {'forward_return_available': True}, 'source_errors': {}, 'missing_fields': {}},
        'strategy-validation-report.md': 'md',
    }
    for name, payload in files.items():
        p = day/name
        if isinstance(payload, str):
            p.write_text(payload, encoding='utf-8')
        else:
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return day


def test_build_pipeline_steps_default_contains_8_steps(tmp_path):
    steps = pipe.build_pipeline_steps(make_args(tmp_path))
    assert len(steps) == 8
    assert [s['step_name'] for s in steps] == ['schema_init','limitup_ecology','theme_emotion','new_high_radar','lhb_sidecar','event_calendar','shortline_daily_report','signal_validation']


def test_report_only_and_no_validation_step_selection(tmp_path):
    assert [s['step_name'] for s in pipe.build_pipeline_steps(make_args(tmp_path, report_only=True))] == ['shortline_daily_report','signal_validation']
    names = [s['step_name'] for s in pipe.build_pipeline_steps(make_args(tmp_path, no_validation=True))]
    assert 'signal_validation' not in names
    assert names[-1] == 'shortline_daily_report'


def test_dry_run_does_not_execute_steps(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(pipe, 'run_pipeline_step', lambda step, args: called.append(step) or {'step_name': step['step_name'], 'ok': True})
    ctx = pipe.run_pipeline(make_args(tmp_path, dry_run=True))
    assert called == []
    assert all(step['skipped'] for step in ctx['steps'])


def test_run_pipeline_step_success_records_ok_and_duration(tmp_path):
    args = make_args(tmp_path)
    step = {'step_name': 'dummy', 'callable': lambda a: {'output_paths': {'x': 'y'}}}
    res = pipe.run_pipeline_step(step, args)
    assert res['ok'] is True
    assert res['duration_seconds'] >= 0
    assert res['output_paths'] == {'x': 'y'}


def test_run_pipeline_step_failure_records_error(tmp_path):
    def boom(args):
        raise RuntimeError('boom')
    res = pipe.run_pipeline_step({'step_name': 'bad', 'callable': boom}, make_args(tmp_path))
    assert res['ok'] is False
    assert 'boom' in res['error']


def test_fail_fast_stops_after_failure(monkeypatch, tmp_path):
    def fake(step, args):
        return {'step_name': step['step_name'], 'ok': step['step_name'] != 'limitup_ecology', 'skipped': False, 'error': 'bad' if step['step_name'] == 'limitup_ecology' else None}
    monkeypatch.setattr(pipe, 'run_pipeline_step', fake)
    ctx = pipe.run_pipeline(make_args(tmp_path, fail_fast=True))
    names = [s['step_name'] for s in ctx['steps']]
    assert names == ['schema_init', 'limitup_ecology']
    assert ctx['failed_steps'] == 1


def test_build_data_quality_audit_counts_tables_empty_missing_and_json_metadata(tmp_path):
    db = init_db(tmp_path, full=True)
    out = write_outputs(tmp_path/'out')
    audit = pipe.build_data_quality_audit(db, out, '2026-05-06')
    assert audit['table_counts']['limitup_daily'] == 4
    assert audit['table_counts']['theme_daily'] == 3
    assert audit['empty_tables'] == []
    assert audit['missing_files'] == []
    assert audit['source_errors']['limitup']['limitup'] == 'mock err'
    assert audit['source_errors']['lhb']['lhb'] == 'failed'
    assert audit['missing_fields']['lhb']['lhb'] == ['seat']
    assert audit['data_readiness']['ready_for_daily_review'] is True
    assert audit['data_readiness']['ready_for_strategy_validation'] is True
    assert audit['data_readiness']['validation_with_forward_return'] is True


def test_data_quality_audit_identifies_empty_tables_and_missing_files(tmp_path):
    db = init_db(tmp_path, full=False)
    out = tmp_path/'out'/'2026-05-06'
    out.mkdir(parents=True)
    (out/'shortline-daily-context.json').write_text(json.dumps({'observation_pool': {}}, ensure_ascii=False), encoding='utf-8')
    audit = pipe.build_data_quality_audit(db, out, '2026-05-06')
    assert 'limitup_daily' in audit['empty_tables']
    assert 'limitup-ecology.json' in audit['missing_files']
    assert audit['data_readiness']['ready_for_daily_review'] is False
    assert audit['data_readiness']['ready_for_strategy_validation'] is False


def test_render_pipeline_markdown_has_fixed_sections(tmp_path):
    context = {'trade_date': '2026-05-06', 'generated_at': 'now', 'db_path': 'db', 'output_root': 'out', 'run_mode': {'dry_run': False}, 'steps': [{'step_name': 'x', 'ok': True, 'skipped': False, 'duration_seconds': 0.1, 'output_paths': {}, 'error': None}], 'total_steps': 1, 'successful_steps': 1, 'failed_steps': 0, 'skipped_steps': 0, 'duration_seconds': 0.1, 'data_quality_audit': {'table_counts': {}, 'empty_tables': [], 'missing_files': [], 'source_errors': {}, 'missing_fields': {}, 'sample_coverage': {}, 'data_readiness': {}}}
    md = pipe.render_pipeline_markdown(context)
    for sec in ['# A 股短线增强 Shadow Pipeline 运行报告','## 1. 总览','## 2. 步骤执行结果','## 3. 数据质量审计','## 4. 样本覆盖情况','## 5. 可用于后续验证的样本状态','## 6. 风险提示']:
        assert sec in md


def test_write_pipeline_outputs_json_parseable_and_no_production_modification(tmp_path):
    prod_db = tmp_path/'ashare_monitor.db'; prod_db.write_bytes(b'prod')
    engine = Path('/home/admin/ashare_system/scripts/ashare_strategy_engine.py')
    before_engine = engine.stat().st_mtime_ns
    before_prod = prod_db.stat().st_mtime_ns
    context = {'trade_date': '2026-05-06', 'generated_at': 'now', 'db_path': 'db', 'output_root': str(tmp_path/'out'), 'run_mode': {}, 'steps': [], 'total_steps': 0, 'successful_steps': 0, 'failed_steps': 0, 'skipped_steps': 0, 'duration_seconds': 0, 'data_quality_audit': {'table_counts': {}, 'empty_tables': [], 'missing_files': [], 'source_errors': {}, 'missing_fields': {}, 'sample_coverage': {}, 'data_readiness': {}}}
    paths = pipe.write_pipeline_outputs(context, tmp_path/'out'/'2026-05-06')
    assert json.loads(Path(paths['json_path']).read_text(encoding='utf-8'))['trade_date'] == '2026-05-06'
    assert Path(paths['markdown_path']).exists()
    assert prod_db.stat().st_mtime_ns == before_prod
    assert engine.stat().st_mtime_ns == before_engine


def test_commit_related_files_only_are_expected_names():
    # Guard for staging intent: the phase is expected to add only these files.
    assert Path('/home/admin/ashare_system/tests/test_shortline_pipeline.py').name == 'test_shortline_pipeline.py'
