#!/usr/bin/env python3
"""Stage 6B shortline shadow pipeline runner and data quality audit.

Scope guard:
- shadow pipeline only;
- no cron / Feishu / production report integration;
- writes only shadow shortline output and shadow DB selected by --db-path;
- optional market/strategy DBs are passed to downstream readers as read-only inputs.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import ashare_shortline_schema as schema

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
DEFAULT_MARKET_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
DEFAULT_STRATEGY_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/strategy/strategy_scoreboard.db')
CST = timezone(timedelta(hours=8))
SHADOW_TABLES = {
    'limitup_daily': 'trade_date',
    'theme_daily': 'trade_date',
    'theme_stock_map': 'trade_date',
    'emotion_anchors': 'trade_date',
    'new_high_daily': 'trade_date',
    'lhb_daily': 'trade_date',
    'event_calendar': 'event_date',
}
EXPECTED_FILES = [
    'limitup-ecology.json', 'limitup-ecology.md',
    'theme-emotion.json', 'theme-emotion.md',
    'new-high-radar.json', 'new-high-radar.md',
    'lhb-sidecar.json', 'lhb-sidecar.md',
    'event-calendar.json', 'event-calendar.md',
    'shortline-daily-context.json', 'shortline-daily-report.md',
    'strategy-validation-context.json', 'strategy-validation-report.md',
]
JSON_SOURCE_NAMES = {
    'limitup-ecology.json': 'limitup',
    'theme-emotion.json': 'theme_emotion',
    'new-high-radar.json': 'new_high',
    'lhb-sidecar.json': 'lhb',
    'event-calendar.json': 'event_calendar',
    'shortline-daily-context.json': 'shortline_daily',
    'strategy-validation-context.json': 'strategy_validation',
}


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def today_str() -> str:
    return datetime.now(tz=CST).strftime('%Y-%m-%d')


def _trade_date(args) -> str:
    return today_str() if getattr(args, 'today', False) or not getattr(args, 'trade_date', None) else args.trade_date


def _script_callable(module_name: str, argv: list[str]):
    def _run(args):
        mod = __import__(module_name)
        rc = mod.main(argv)
        if rc not in (0, None):
            raise RuntimeError(f'{module_name}.main returned {rc}')
        return {'callable': f'{module_name}.main', 'argv': argv}
    return _run


def _schema_init(args):
    return schema.init_db(args.db_path)


def _daily_report(args):
    import ashare_shortline_daily_report as rpt
    return rpt.run_shortline_daily_report(_trade_date(args), db_path=args.db_path, market_db_path=args.market_db_path, output_root=args.output_root)


def _validation(args):
    import ashare_shortline_signal_validation as val
    return val.run_strategy_validation(_trade_date(args), db_path=args.db_path, strategy_db_path=args.strategy_db_path, market_db_path=args.market_db_path, output_root=args.output_root)


def build_pipeline_steps(args) -> list[dict]:
    trade_date = _trade_date(args)
    db = str(args.db_path)
    out = str(args.output_root)
    # With --skip-fetch, do not call AkShare-backed collectors. Keep schema + reports/validation.
    fetch_steps = [
        {'step_name': 'limitup_ecology', 'description': '涨停生态', 'command': f'python scripts/ashare_limitup_collector.py --trade-date {trade_date} --db-path {db} --output-root {out}', 'callable': _script_callable('ashare_limitup_collector', ['--trade-date', trade_date, '--db-path', db, '--output-root', out])},
        {'step_name': 'theme_emotion', 'description': '题材图谱与情绪锚点', 'command': f'python scripts/ashare_theme_emotion.py --trade-date {trade_date} --db-path {db} --output-root {out}', 'callable': _script_callable('ashare_theme_emotion', ['--trade-date', trade_date, '--db-path', db, '--output-root', out])},
        {'step_name': 'new_high_radar', 'description': '百日新高雷达', 'command': f'python scripts/ashare_new_high_radar.py --trade-date {trade_date} --db-path {db} --output-root {out}', 'callable': _script_callable('ashare_new_high_radar', ['--trade-date', trade_date, '--db-path', db, '--output-root', out])},
        {'step_name': 'lhb_sidecar', 'description': '龙虎榜 sidecar', 'command': f'python scripts/ashare_lhb_sidecar.py --trade-date {trade_date} --db-path {db} --output-root {out}' + (' --skip-fetch' if args.skip_fetch else ''), 'callable': _script_callable('ashare_lhb_sidecar', ['--trade-date', trade_date, '--db-path', db, '--output-root', out] + (['--skip-fetch'] if args.skip_fetch else []))},
        {'step_name': 'event_calendar', 'description': '事件日历 sidecar', 'command': f'python scripts/ashare_event_calendar.py --event-date {trade_date} --db-path {db} --output-root {out}' + (' --skip-fetch' if args.skip_fetch else ''), 'callable': _script_callable('ashare_event_calendar', ['--event-date', trade_date, '--db-path', db, '--output-root', out] + (['--skip-fetch'] if args.skip_fetch else []))},
    ]
    report_steps = [
        {'step_name': 'shortline_daily_report', 'description': '短线综合日报', 'command': f'python scripts/ashare_shortline_daily_report.py --trade-date {trade_date} --db-path {db} --market-db-path {args.market_db_path} --output-root {out}', 'callable': _daily_report},
    ]
    validation_step = {'step_name': 'signal_validation', 'description': '策略验证与 shadow score', 'command': f'python scripts/ashare_shortline_signal_validation.py --trade-date {trade_date} --db-path {db} --strategy-db-path {args.strategy_db_path} --market-db-path {args.market_db_path} --output-root {out}', 'callable': _validation}
    if args.report_only:
        steps = report_steps + ([] if args.no_validation else [validation_step])
    else:
        steps = [{'step_name': 'schema_init', 'description': '初始化 shadow schema', 'command': f'python scripts/ashare_shortline_schema.py init --db-path {db}', 'callable': _schema_init}]
        if not args.skip_fetch:
            steps.extend(fetch_steps)
        else:
            steps.extend([{**s, 'skip_reason': '--skip-fetch: 跳过可能访问实时 AkShare 的采集步骤'} for s in fetch_steps[:3]])
            # lhb/event support skip-fetch and can regenerate reports from DB.
            steps.extend(fetch_steps[3:])
        steps.extend(report_steps)
        if not args.no_validation:
            steps.append(validation_step)
    return steps


def run_pipeline_step(step: dict, args) -> dict:
    started = now_iso()
    t0 = datetime.now(tz=CST)
    res = {
        'step_name': step.get('step_name'), 'description': step.get('description'),
        'command': step.get('command') or step.get('callable'), 'started_at': started,
        'ended_at': None, 'duration_seconds': None, 'ok': False, 'skipped': False,
        'error': None, 'output_paths': {},
    }
    if step.get('skip_reason'):
        ended = datetime.now(tz=CST)
        res.update({'ended_at': ended.isoformat(timespec='seconds'), 'duration_seconds': round((ended - t0).total_seconds(), 3), 'ok': True, 'skipped': True, 'error': step.get('skip_reason')})
        return res
    try:
        payload = step['callable'](args)
        if isinstance(payload, dict):
            paths = payload.get('paths') or payload.get('output_paths') or {}
            for key in ['json_path', 'markdown_path', 'daily_json_path', 'daily_markdown_path']:
                if key in payload:
                    paths[key] = payload[key]
            res['output_paths'] = paths
        res['ok'] = True
    except Exception as exc:
        res['ok'] = False
        res['error'] = f'{type(exc).__name__}: {exc}'
        res['traceback'] = traceback.format_exc(limit=5)
    ended = datetime.now(tz=CST)
    res['ended_at'] = ended.isoformat(timespec='seconds')
    res['duration_seconds'] = round((ended - t0).total_seconds(), 3)
    return res


def load_json_safe(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as exc:
        return {'_json_error': str(exc)}


def count_table_rows(conn, table_name: str, date_field: str, trade_date: str) -> int:
    try:
        cur = conn.execute(f'SELECT COUNT(*) FROM {table_name} WHERE {date_field}=?', (trade_date,))
        return int(cur.fetchone()[0])
    except Exception:
        return 0


def _pool_counts(payload: dict) -> dict:
    pool = payload.get('observation_pool') or {}
    if not isinstance(pool, dict):
        return {'A': 0, 'B': 0, 'C': 0}
    return {k: len(pool.get(k) or []) for k in ['A', 'B', 'C']}


def build_data_quality_audit(db_path, output_dir, trade_date) -> dict:
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    table_counts = {}
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            for table, date_field in SHADOW_TABLES.items():
                table_counts[table] = count_table_rows(conn, table, date_field, trade_date)
    else:
        table_counts = {t: 0 for t in SHADOW_TABLES}
    empty_tables = [t for t, c in table_counts.items() if c == 0]
    file_exists = {name: (output_dir / name).exists() for name in EXPECTED_FILES}
    missing_files = [name for name, ok in file_exists.items() if not ok]
    source_errors, missing_fields = {}, {}
    json_payloads = {}
    for fname, source in JSON_SOURCE_NAMES.items():
        payload = load_json_safe(output_dir / fname)
        json_payloads[source] = payload
        if payload.get('source_errors'):
            source_errors[source] = payload.get('source_errors')
        if payload.get('missing_fields'):
            missing_fields[source] = payload.get('missing_fields')
        if payload.get('_json_error'):
            source_errors[source] = {'json_error': payload['_json_error']}
    daily_ctx = json_payloads.get('shortline_daily') or {}
    validation_ctx = json_payloads.get('strategy_validation') or {}
    pool_counts = _pool_counts(daily_ctx)
    sample_coverage = {
        **{f'{t}_rows': table_counts.get(t, 0) for t in SHADOW_TABLES},
        'observation_pool_A': pool_counts.get('A', 0),
        'observation_pool_B': pool_counts.get('B', 0),
        'observation_pool_C': pool_counts.get('C', 0),
        'forward_return_count': ((validation_ctx.get('sample_coverage') or {}).get('with_forward_return_count') or 0),
    }
    has_core = table_counts.get('limitup_daily', 0) > 0 and table_counts.get('theme_daily', 0) > 0 and table_counts.get('new_high_daily', 0) > 0
    pool_non_empty = sum(pool_counts.values()) > 0
    forward_available = bool((validation_ctx.get('data_status') or {}).get('forward_return_available') or sample_coverage['forward_return_count'] > 0)
    readiness_reasons = []
    if not has_core:
        readiness_reasons.append('limitup/theme/new_high 核心 shadow 信号不足')
    if not pool_non_empty:
        readiness_reasons.append('shortline-daily-context 缺失或 observation_pool 为空')
    if not forward_available:
        readiness_reasons.append('暂无 forward return，仅可做覆盖率验证')
    data_readiness = {
        'ready_for_daily_review': bool(has_core),
        'ready_for_strategy_validation': bool(pool_non_empty),
        'validation_with_forward_return': bool(forward_available),
        'coverage_only_validation': bool(pool_non_empty and not forward_available),
        'reasons': readiness_reasons,
    }
    # Simple field missing-rate MVP: count missing code/name among rows where possible.
    key_field_missing_rate = {}
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            for table, date_field in SHADOW_TABLES.items():
                try:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
                    rates = {}
                    for col in ['code', 'name']:
                        if col in cols and table_counts.get(table, 0):
                            miss = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {date_field}=? AND ({col} IS NULL OR TRIM(CAST({col} AS TEXT))='')", (trade_date,)).fetchone()[0]
                            rates[col] = round(miss / table_counts[table], 4)
                    if rates:
                        key_field_missing_rate[table] = rates
                except Exception:
                    continue
    return {
        'trade_date': trade_date, 'db_path': str(db_path), 'output_dir': str(output_dir),
        'table_counts': table_counts, 'file_exists': file_exists, 'empty_tables': empty_tables,
        'missing_files': missing_files, 'source_errors': source_errors, 'missing_fields': missing_fields,
        'key_field_missing_rate': key_field_missing_rate, 'sample_coverage': sample_coverage,
        'data_readiness': data_readiness,
    }


def run_pipeline(args) -> dict:
    trade_date = _trade_date(args)
    output_dir = Path(args.output_root) / trade_date
    output_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(tz=CST)
    steps = []
    planned = build_pipeline_steps(args)
    if args.dry_run:
        for step in planned:
            steps.append({'step_name': step['step_name'], 'description': step.get('description'), 'command': step.get('command'), 'started_at': None, 'ended_at': None, 'duration_seconds': 0, 'ok': True, 'skipped': True, 'error': 'dry-run', 'output_paths': {}})
    else:
        for step in planned:
            res = run_pipeline_step(step, args)
            steps.append(res)
            if args.fail_fast and not res.get('ok'):
                break
    audit = build_data_quality_audit(args.db_path, output_dir, trade_date)
    ended = datetime.now(tz=CST)
    context = {
        'ok': all(s.get('ok') for s in steps), 'trade_date': trade_date, 'generated_at': ended.isoformat(timespec='seconds'),
        'db_path': str(args.db_path), 'output_root': str(args.output_root),
        'run_mode': {'skip_fetch': args.skip_fetch, 'report_only': args.report_only, 'no_validation': args.no_validation, 'fail_fast': args.fail_fast, 'dry_run': args.dry_run},
        'steps': steps, 'total_steps': len(steps), 'successful_steps': sum(1 for s in steps if s.get('ok') and not s.get('skipped')),
        'failed_steps': sum(1 for s in steps if not s.get('ok')), 'skipped_steps': sum(1 for s in steps if s.get('skipped')),
        'duration_seconds': round((ended - started).total_seconds(), 3), 'data_quality_audit': audit,
    }
    paths = write_pipeline_outputs(context, output_dir)
    context['paths'] = paths
    # Rewrite once with paths included.
    write_pipeline_outputs(context, output_dir)
    return context


def render_pipeline_markdown(context: dict) -> str:
    audit = context.get('data_quality_audit') or {}
    lines = ['# A 股短线增强 Shadow Pipeline 运行报告', '']
    lines += ['## 1. 总览', f"- trade_date: {context.get('trade_date')}", f"- generated_at: {context.get('generated_at')}", f"- db_path: {context.get('db_path')}", f"- output_root: {context.get('output_root')}", f"- run_mode: {context.get('run_mode')}", f"- 总步骤数: {context.get('total_steps')}", f"- 成功步骤数: {context.get('successful_steps')}", f"- 失败步骤数: {context.get('failed_steps')}", f"- 跳过步骤数: {context.get('skipped_steps')}", f"- 总耗时: {context.get('duration_seconds')} 秒", '']
    lines += ['## 2. 步骤执行结果']
    for step in context.get('steps') or []:
        status = '跳过' if step.get('skipped') else ('成功' if step.get('ok') else '失败')
        lines.append(f"- {step.get('step_name')}: {status}；耗时 {step.get('duration_seconds')} 秒；输出文件 {step.get('output_paths') or {}}；错误摘要 {step.get('error') or '无'}")
    lines.append('')
    lines += ['## 3. 数据质量审计', f"- 各 shadow 表行数: {audit.get('table_counts')}", f"- 各 JSON/Markdown 是否存在: {audit.get('file_exists')}", f"- empty tables: {audit.get('empty_tables')}", f"- missing files: {audit.get('missing_files')}", f"- source_errors: {audit.get('source_errors')}", f"- missing_fields: {audit.get('missing_fields')}", f"- 关键字段缺失率: {audit.get('key_field_missing_rate')}", '']
    sc = audit.get('sample_coverage') or {}
    lines += ['## 4. 样本覆盖情况']
    for key in ['limitup_daily_rows','theme_daily_rows','theme_stock_map_rows','emotion_anchors_rows','new_high_daily_rows','lhb_daily_rows','event_calendar_rows','observation_pool_A','observation_pool_B','observation_pool_C','forward_return_count']:
        lines.append(f'- {key}: {sc.get(key, 0)}')
    lines.append('')
    dr = audit.get('data_readiness') or {}
    lines += ['## 5. 可用于后续验证的样本状态', f"- 是否具备阶段 6 策略验证条件: {dr.get('ready_for_strategy_validation')}", f"- 是否有 forward return: {dr.get('validation_with_forward_return')}", f"- 是否有足够 sidecar 信号: {dr.get('ready_for_daily_review')}", f"- coverage_only_validation: {dr.get('coverage_only_validation')}", f"- 当前距离“20 个交易日样本”还缺什么: {dr.get('reasons')}", '']
    lines += ['## 6. 风险提示', '- 当前仍是 shadow pipeline。', '- 不接入生产报告。', '- 不构成投资建议。', '- 数据源可能延迟、缺失或字段变化。', '']
    return '\n'.join(lines)


def write_pipeline_outputs(context: dict, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / 'pipeline-run.json'
    md_path = output_dir / 'pipeline-run.md'
    json_path.write_text(json.dumps(context, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    md_path.write_text(render_pipeline_markdown(context), encoding='utf-8')
    return {'json_path': str(json_path), 'markdown_path': str(md_path)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Run shortline shadow sidecar pipeline and data quality audit')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--today', action='store_true')
    g.add_argument('--trade-date')
    p.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    p.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    p.add_argument('--market-db-path', default=str(DEFAULT_MARKET_DB_PATH))
    p.add_argument('--strategy-db-path', default=str(DEFAULT_STRATEGY_DB_PATH))
    p.add_argument('--skip-fetch', action='store_true')
    p.add_argument('--report-only', action='store_true')
    p.add_argument('--no-validation', action='store_true')
    p.add_argument('--fail-fast', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    context = run_pipeline(args)
    print(json.dumps({'ok': context.get('ok'), 'paths': context.get('paths'), 'failed_steps': context.get('failed_steps')}, ensure_ascii=False, indent=2))
    return 0 if context.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
