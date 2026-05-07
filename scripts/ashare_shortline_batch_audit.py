#!/usr/bin/env python3
"""Stage 6C multi-day shortline shadow pipeline audit dashboard.

Scope guard:
- shadow batch audit only;
- default audit-only, no live fetch unless user explicitly passes --run;
- no cron / Feishu / production report integration;
- writes only batch audit files under shadow output-root.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
DEFAULT_MARKET_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
DEFAULT_STRATEGY_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/strategy/strategy_scoreboard.db')
CST = timezone(timedelta(hours=8))
CALENDAR_FALLBACK_USED = False
SHADOW_TABLES = {
    'limitup_daily': 'trade_date',
    'theme_daily': 'trade_date',
    'theme_stock_map': 'trade_date',
    'emotion_anchors': 'trade_date',
    'new_high_daily': 'trade_date',
    'lhb_daily': 'trade_date',
    'event_calendar': 'event_date',
}
MODULES = ['limitup', 'theme', 'emotion', 'new_high', 'lhb', 'event', 'daily_report', 'validation']


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def load_json_safe(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as exc:
        return {'_json_error': str(exc)}


def get_trade_calendar_dates(start_date=None, end_date=None) -> list[str]:
    """Best-effort local/AkShare calendar adapter; callers can monkeypatch in tests."""
    try:
        import ashare_data_utils as adu  # type: ignore
        for name in ['get_trade_dates', 'trade_dates_between', 'get_recent_trade_dates']:
            fn = getattr(adu, name, None)
            if callable(fn):
                try:
                    vals = fn(start_date, end_date) if start_date or end_date else fn()
                    if vals:
                        return sorted(str(x)[:10] for x in vals)
                except TypeError:
                    continue
    except Exception:
        pass
    return []


def _weekday_dates(start: date, end: date) -> list[str]:
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def resolve_trade_dates(start_date=None, end_date=None, last_n=None) -> list[str]:
    global CALENDAR_FALLBACK_USED
    CALENDAR_FALLBACK_USED = False
    today = datetime.now(tz=CST).date()
    if last_n is None and not (start_date and end_date):
        last_n = 20
    if start_date or end_date:
        start = date.fromisoformat(start_date or end_date)
        end = date.fromisoformat(end_date or start_date)
        cal = get_trade_calendar_dates(start.isoformat(), end.isoformat())
        vals = [d for d in cal if start.isoformat() <= d <= end.isoformat()]
        if vals:
            return vals
        CALENDAR_FALLBACK_USED = True
        return _weekday_dates(start, end)
    # last_n mode: ask a broad enough range; fallback to weekdays.
    n = int(last_n or 20)
    start = today - timedelta(days=max(40, n * 3))
    cal = get_trade_calendar_dates(start.isoformat(), today.isoformat())
    if cal:
        return cal[-n:]
    CALENDAR_FALLBACK_USED = True
    return _weekday_dates(start, today)[-n:]


def build_batch_plan(trade_dates, args) -> list[dict]:
    plan = []
    for d in trade_dates:
        argv = ['--trade-date', d, '--db-path', str(args.db_path), '--output-root', str(args.output_root), '--market-db-path', str(args.market_db_path), '--strategy-db-path', str(args.strategy_db_path)]
        if getattr(args, 'skip_fetch', False):
            argv.append('--skip-fetch')
        if getattr(args, 'report_only', False):
            argv.append('--report-only')
        if getattr(args, 'dry_run', False):
            argv.append('--dry-run')
        command = 'python scripts/ashare_shortline_pipeline.py ' + ' '.join(argv)
        plan.append({'trade_date': d, 'argv': argv, 'command': command})
    return plan


def run_one_pipeline(step: dict, args) -> dict:
    import ashare_shortline_pipeline as pipeline
    rc = pipeline.main(step['argv'])
    return {'ok': rc == 0, 'return_code': rc}


def run_batch_plan(plan, args) -> list[dict]:
    results = []
    for step in plan:
        started = now_iso()
        t0 = datetime.now(tz=CST)
        res = {'trade_date': step['trade_date'], 'command': step['command'], 'started_at': started, 'ended_at': None, 'duration_seconds': 0, 'ok': False, 'skipped': False, 'error': None}
        if getattr(args, 'dry_run', False):
            res.update({'ok': True, 'skipped': True, 'error': 'dry-run'})
        else:
            try:
                payload = run_one_pipeline(step, args)
                res.update(payload if isinstance(payload, dict) else {'ok': bool(payload)})
                res['ok'] = bool(res.get('ok'))
            except Exception as exc:
                res['ok'] = False
                res['error'] = f'{type(exc).__name__}: {exc}'
                res['traceback'] = traceback.format_exc(limit=5)
        ended = datetime.now(tz=CST)
        res['ended_at'] = ended.isoformat(timespec='seconds')
        res['duration_seconds'] = round((ended - t0).total_seconds(), 3)
        results.append(res)
        if getattr(args, 'fail_fast', False) and not res.get('ok'):
            break
    return results


def load_pipeline_context(output_root, trade_date) -> dict:
    return load_json_safe(Path(output_root) / trade_date / 'pipeline-run.json')


def load_daily_context(output_root, trade_date) -> dict:
    return load_json_safe(Path(output_root) / trade_date / 'shortline-daily-context.json')


def load_validation_context(output_root, trade_date) -> dict:
    return load_json_safe(Path(output_root) / trade_date / 'strategy-validation-context.json')


def count_shadow_rows_by_date(conn, trade_date) -> dict:
    out = {}
    for table, field in SHADOW_TABLES.items():
        try:
            out[table] = int(conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {field}=?', (trade_date,)).fetchone()[0])
        except Exception:
            out[table] = 0
    return out


def _pool_counts(ctx: dict) -> dict:
    pool = ctx.get('observation_pool') or {}
    if not isinstance(pool, dict):
        return {'A_count': 0, 'B_count': 0, 'C_count': 0, 'total_observation_count': 0}
    res = {f'{k}_count': len(pool.get(k) or []) for k in ['A', 'B', 'C']}
    res['total_observation_count'] = sum(res.values())
    return res


def _score_counts(ctx: dict) -> dict:
    counts = {'A_shadow_count': 0, 'B_shadow_count': 0, 'C_shadow_count': 0, 'Risk_count': 0}
    for row in ctx.get('shadow_score_table') or []:
        lvl = row.get('score_level')
        key = f'{lvl}_count' if lvl in ['A_shadow', 'B_shadow', 'C_shadow'] else 'Risk_count' if lvl == 'Risk' else None
        if key:
            counts[key] += 1
    return counts


def _module_flags(row_counts: dict, daily: dict, validation: dict) -> dict:
    return {
        'limitup': row_counts.get('limitup_daily', 0) > 0,
        'theme': row_counts.get('theme_daily', 0) > 0 or row_counts.get('theme_stock_map', 0) > 0,
        'emotion': row_counts.get('emotion_anchors', 0) > 0,
        'new_high': row_counts.get('new_high_daily', 0) > 0,
        'lhb': row_counts.get('lhb_daily', 0) > 0,
        'event': row_counts.get('event_calendar', 0) > 0,
        'daily_report': bool(daily),
        'validation': bool(validation),
    }


def compute_module_availability(daily_audits) -> dict:
    n = len(daily_audits) or 1
    out = {}
    for m in MODULES:
        available = sum(1 for d in daily_audits if (d.get('modules') or {}).get(m))
        out[m] = {'available_days': available, 'missing_days': len(daily_audits) - available, 'availability_rate': round(available / n, 4)}
    return out


def build_multi_day_audit(trade_dates, db_path, output_root) -> dict:
    db_path = Path(db_path)
    output_root = Path(output_root)
    daily_audits = []
    if db_path.exists():
        conn_ctx = sqlite3.connect(db_path)
    else:
        conn_ctx = sqlite3.connect(':memory:')
    with conn_ctx as conn:
        for d in trade_dates:
            pipeline = load_pipeline_context(output_root, d)
            daily = load_daily_context(output_root, d)
            validation = load_validation_context(output_root, d)
            rows = count_shadow_rows_by_date(conn, d)
            p_status = 'missing' if not pipeline else 'failed' if pipeline.get('ok') is False else 'ok'
            missing_files = ((pipeline.get('data_quality_audit') or {}).get('missing_files') or []) if pipeline else ['pipeline-run.json']
            source_errors = {}
            missing_fields = {}
            for name, payload in [('pipeline', pipeline), ('daily_report', daily), ('validation', validation)]:
                if payload.get('source_errors'):
                    source_errors[name] = payload.get('source_errors')
                if payload.get('missing_fields'):
                    missing_fields[name] = payload.get('missing_fields')
                if payload.get('_json_error'):
                    source_errors[name] = {'json_error': payload.get('_json_error')}
            if (pipeline.get('data_quality_audit') or {}).get('source_errors'):
                source_errors['pipeline_audit'] = (pipeline.get('data_quality_audit') or {}).get('source_errors')
            if (pipeline.get('data_quality_audit') or {}).get('missing_fields'):
                missing_fields['pipeline_audit'] = (pipeline.get('data_quality_audit') or {}).get('missing_fields')
            coverage = _pool_counts(daily)
            sc = validation.get('sample_coverage') or {}
            forward = {
                'forward_return_count': sc.get('with_forward_return_count', 0),
                'with_forward_return_count': sc.get('with_forward_return_count', 0),
                'without_forward_return_count': sc.get('without_forward_return_count', 0),
                'coverage_only_validation': bool(((validation.get('data_quality_audit') or {}).get('data_readiness') or {}).get('coverage_only_validation') or ((validation.get('data_status') or {}).get('forward_return_available') is False and validation)),
                'validation_with_forward_return': bool(sc.get('with_forward_return_count', 0)),
            }
            daily_audits.append({
                'trade_date': d, 'pipeline_exists': bool(pipeline), 'pipeline_status': p_status,
                'pipeline_ok': pipeline.get('ok') if pipeline else False,
                'total_steps': pipeline.get('total_steps', 0), 'successful_steps': pipeline.get('successful_steps', 0),
                'failed_steps': pipeline.get('failed_steps', 0), 'skipped_steps': pipeline.get('skipped_steps', 0),
                'missing_files': missing_files, 'source_errors': source_errors, 'missing_fields': missing_fields,
                'row_counts': rows, 'modules': _module_flags(rows, daily, validation),
                'observation_pool': coverage, 'forward_return': forward, 'shadow_score': _score_counts(validation),
            })
    module_availability = compute_module_availability(daily_audits)
    obs = {d['trade_date']: d['observation_pool'] for d in daily_audits}
    forward_with = [d['trade_date'] for d in daily_audits if d['forward_return']['validation_with_forward_return']]
    forward_without = [d['trade_date'] for d in daily_audits if not d['forward_return']['validation_with_forward_return']]
    coverage_only = [d['trade_date'] for d in daily_audits if d['forward_return']['coverage_only_validation']]
    score_totals = {'A_shadow_count': 0, 'B_shadow_count': 0, 'C_shadow_count': 0, 'Risk_count': 0}
    for d in daily_audits:
        for k in score_totals:
            score_totals[k] += d['shadow_score'].get(k, 0)
    audit = {
        'trade_dates': list(trade_dates), 'date_range': {'start': trade_dates[0] if trade_dates else None, 'end': trade_dates[-1] if trade_dates else None},
        'daily_audits': daily_audits,
        'pipeline_complete_days': sum(1 for d in daily_audits if d['pipeline_status'] == 'ok'),
        'daily_report_complete_days': sum(1 for d in daily_audits if d['modules']['daily_report']),
        'validation_complete_days': sum(1 for d in daily_audits if d['modules']['validation']),
        'module_availability': module_availability,
        'observation_pool_coverage': obs,
        'observation_pool_stable_days': sum(1 for d in daily_audits if d['observation_pool']['total_observation_count'] > 0),
        'forward_return_coverage': {'with_forward_return_dates': forward_with, 'without_forward_return_dates': forward_without, 'coverage_only_validation_dates': coverage_only, 'validation_with_forward_return_dates': forward_with},
        'shadow_score_coverage': score_totals,
        'source_error_days': [d['trade_date'] for d in daily_audits if d['source_errors']],
    }
    audit['stage7_readiness'] = evaluate_stage7_readiness(audit)
    return audit


def evaluate_stage7_readiness(audit_context) -> dict:
    dates = audit_context.get('trade_dates') or []
    n = len(dates)
    mods = audit_context.get('module_availability') or {}
    forward_days = len((audit_context.get('forward_return_coverage') or {}).get('with_forward_return_dates') or [])
    score = audit_context.get('shadow_score_coverage') or {}
    def item(ok, evidence, risk):
        return {'ok': bool(ok), 'evidence': evidence, 'missing_or_risk': risk}
    checklist = {
        'pipeline_20_days': item(n >= 20 and audit_context.get('pipeline_complete_days', 0) >= 20, f"{audit_context.get('pipeline_complete_days', 0)}/{n} pipeline 完整", '需要连续 20 个交易日 pipeline-run.json 存在'),
        'core_tables_quality': item(all((mods.get(m) or {}).get('availability_rate', 0) >= 0.8 for m in ['limitup','theme','new_high']), {m: (mods.get(m) or {}).get('availability_rate', 0) for m in ['limitup','theme','new_high']}, '核心表 limitup/theme/new_high 缺失率需可控'),
        'observation_pool_stable': item(audit_context.get('observation_pool_stable_days', 0) >= min(n, 20) and n >= 20, f"{audit_context.get('observation_pool_stable_days', 0)}/{n} 有 observation_pool", 'A/B/C 观察池还需稳定生成'),
        'forward_return_stable': item(forward_days >= min(n, 20) and n >= 20, f"{forward_days}/{n} 有 forward return", '需要稳定 forward return 覆盖'),
        'shadow_score_separation': item(all(score.get(k, 0) > 0 for k in ['A_shadow_count','B_shadow_count','C_shadow_count','Risk_count']) and n >= 20, score, '样本不足，不能判断 A/B/C/Risk 区分度'),
        'negative_feedback_validated': item(score.get('Risk_count', 0) >= 5 and n >= 20, f"Risk 样本 {score.get('Risk_count', 0)}", '负反馈扣分项需要更多样本验证'),
        'source_errors_controlled': item(len(audit_context.get('source_error_days') or []) <= max(1, n * 0.2) and n >= 20, f"source_error_days={audit_context.get('source_error_days')}", 'source_errors 仍需连续观察'),
        'user_field_confirmation': item(False, '尚未进行阶段 7 字段确认', '需要用户确认哪些 shadow 字段可进入正式报告'),
    }
    return {'overall_ready': all(v['ok'] for v in checklist.values()), 'checklist': checklist}


def render_batch_audit_markdown(context) -> str:
    audit = context.get('audit') or {}
    readiness = audit.get('stage7_readiness') or {}
    lines = ['# A 股短线 Shadow Pipeline 多日样本审计报告', '']
    lines += ['## 1. 总览', f"- 审计日期范围: {(audit.get('date_range') or {}).get('start')} ~ {(audit.get('date_range') or {}).get('end')}", f"- 交易日数量: {len(audit.get('trade_dates') or [])}", f"- pipeline 完整日期数: {audit.get('pipeline_complete_days', 0)}", f"- daily report 完整日期数: {audit.get('daily_report_complete_days', 0)}", f"- validation 完整日期数: {audit.get('validation_complete_days', 0)}", f"- forward return 覆盖日期数: {len((audit.get('forward_return_coverage') or {}).get('with_forward_return_dates') or [])}", f"- 是否满足阶段 7 前置条件: {readiness.get('overall_ready')}", f"- calendar_fallback: {context.get('calendar_fallback')}", '']
    lines += ['## 2. 每日运行状态', '| trade_date | pipeline 状态 | successful_steps | failed_steps | missing_files | source_errors 摘要 |', '|---|---:|---:|---:|---|---|']
    for d in audit.get('daily_audits') or []:
        lines.append(f"| {d['trade_date']} | {d['pipeline_status']} | {d.get('successful_steps',0)} | {d.get('failed_steps',0)} | {len(d.get('missing_files') or [])} | {list((d.get('source_errors') or {}).keys())} |")
    lines += ['', '## 3. 核心表行数统计', '| trade_date | limitup_daily | theme_daily | theme_stock_map | emotion_anchors | new_high_daily | lhb_daily | event_calendar |', '|---|---:|---:|---:|---:|---:|---:|---:|']
    for d in audit.get('daily_audits') or []:
        r = d.get('row_counts') or {}
        lines.append(f"| {d['trade_date']} | {r.get('limitup_daily',0)} | {r.get('theme_daily',0)} | {r.get('theme_stock_map',0)} | {r.get('emotion_anchors',0)} | {r.get('new_high_daily',0)} | {r.get('lhb_daily',0)} | {r.get('event_calendar',0)} |")
    lines += ['', '## 4. 模块可用性与缺失率']
    for m, v in (audit.get('module_availability') or {}).items():
        lines.append(f"- {m}: available_days={v.get('available_days')} missing_days={v.get('missing_days')} availability_rate={v.get('availability_rate')}")
    lines += ['', '## 5. Observation Pool 覆盖']
    for d, v in (audit.get('observation_pool_coverage') or {}).items():
        lines.append(f"- {d}: A={v.get('A_count',0)} B={v.get('B_count',0)} C={v.get('C_count',0)} total={v.get('total_observation_count',0)}")
    lines += ['', '## 6. Forward Return 覆盖']
    fr = audit.get('forward_return_coverage') or {}
    lines += [f"- 有 forward return 的日期: {fr.get('with_forward_return_dates')}", f"- 无 forward return 的日期: {fr.get('without_forward_return_dates')}", f"- coverage_only_validation 日期: {fr.get('coverage_only_validation_dates')}", f"- validation_with_forward_return 日期: {fr.get('validation_with_forward_return_dates')}"]
    lines += ['', '## 7. Shadow Score 分层覆盖']
    sc = audit.get('shadow_score_coverage') or {}
    lines += [f"- A_shadow: {sc.get('A_shadow_count',0)}", f"- B_shadow: {sc.get('B_shadow_count',0)}", f"- C_shadow: {sc.get('C_shadow_count',0)}", f"- Risk: {sc.get('Risk_count',0)}"]
    if len(audit.get('trade_dates') or []) < 20:
        lines.append('- 样本不足，不能判断。')
    lines += ['', '## 8. 阶段 7 前置条件 Checklist']
    for name, item in (readiness.get('checklist') or {}).items():
        lines.append(f"- {name}: 当前状态={item.get('ok')}；证据={item.get('evidence')}；还缺什么={item.get('missing_or_risk')}")
    lines += ['', '## 9. 建议']
    if readiness.get('overall_ready'):
        lines.append('- 可以考虑进入阶段 7 的方案讨论，但仍需用户确认正式报告字段。')
    else:
        need = max(0, 20 - len(audit.get('trade_dates') or []))
        lines.append(f'- 暂不建议进入阶段 7；建议继续积累至少 {need} 个交易日或补齐缺失模块。')
    lines.append('- 暂不应进入正式报告的信号：样本不足、字段不稳定、弱规则识别、缺失率高的信号。')
    lines += ['', '## 10. 风险提示', '- 当前仍是 shadow audit。', '- 不接入生产报告。', '- 不构成投资建议。', '- 历史表现不代表未来收益。', '- 数据源可能延迟、缺失或字段变化。', '']
    return '\n'.join(lines)


def write_batch_audit_outputs(context, output_root) -> dict:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    jp = root / 'batch-audit-context.json'
    mp = root / 'batch-audit-report.md'
    jp.write_text(json.dumps(context, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    mp.write_text(render_batch_audit_markdown(context), encoding='utf-8')
    return {'json_path': str(jp), 'markdown_path': str(mp)}


def run_batch_audit(args) -> dict:
    trade_dates = resolve_trade_dates(args.start_date, args.end_date, args.last_n)
    plan = build_batch_plan(trade_dates, args)
    batch_results = run_batch_plan(plan, args) if args.run or args.dry_run else []
    audit = build_multi_day_audit(trade_dates, args.db_path, args.output_root)
    context = {'ok': True, 'generated_at': now_iso(), 'calendar_fallback': CALENDAR_FALLBACK_USED, 'trade_dates': trade_dates, 'run_mode': {'run': args.run, 'audit_only': not args.run, 'skip_fetch': args.skip_fetch, 'report_only': args.report_only, 'dry_run': args.dry_run, 'fail_fast': args.fail_fast}, 'batch_plan': plan, 'batch_results': batch_results, 'audit': audit}
    context['paths'] = write_batch_audit_outputs(context, args.output_root)
    write_batch_audit_outputs(context, args.output_root)
    return context


def build_parser():
    p = argparse.ArgumentParser(description='Multi-day shortline shadow pipeline batch audit dashboard')
    p.add_argument('--last-n', type=int, default=None)
    p.add_argument('--start-date')
    p.add_argument('--end-date')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--audit-only', action='store_true', default=True)
    mode.add_argument('--run', action='store_true')
    p.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    p.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    p.add_argument('--market-db-path', default=str(DEFAULT_MARKET_DB_PATH))
    p.add_argument('--strategy-db-path', default=str(DEFAULT_STRATEGY_DB_PATH))
    p.add_argument('--skip-fetch', action='store_true')
    p.add_argument('--report-only', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--fail-fast', action='store_true')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    # argparse default=True on audit-only keeps both audit_only/run states simple.
    if args.run:
        args.audit_only = False
    context = run_batch_audit(args)
    print(json.dumps({'ok': context['ok'], 'paths': context['paths'], 'trade_dates': context['trade_dates']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
