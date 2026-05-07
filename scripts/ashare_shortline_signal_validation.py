#!/usr/bin/env python3
"""Stage 6 shadow validation for shortline sidecar signals.

Scope guard:
- shadow analysis only;
- read shadow shortline DB and optional production DBs read-only;
- write only strategy-validation markdown/json under shortline output root;
- do not import AkShare, do not modify production strategy engine or reports.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
DEFAULT_STRATEGY_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/strategy/strategy_scoreboard.db')
DEFAULT_MARKET_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
CST = timezone(timedelta(hours=8))
SHORTLINE_TABLES = [
    'limitup_daily', 'theme_daily', 'theme_stock_map', 'emotion_anchors',
    'new_high_daily', 'lhb_daily', 'event_calendar'
]
FORWARD_FIELDS = ['forward_1d', 'forward_3d', 'forward_5d', 'forward_10d', 'forward_20d', 'max_adverse']
COMBO_DEFINITIONS = [
    ('Top题材+涨停', lambda r: r.get('is_top_theme') and r.get('is_limitup')),
    ('Top题材+100/250日新高', lambda r: r.get('is_top_theme') and r.get('new_high_type') in {'100日新高', '250日新高'}),
    ('Top题材+龙虎榜', lambda r: r.get('is_top_theme') and r.get('has_lhb')),
    ('Top题材+正向事件', lambda r: r.get('is_top_theme') and r.get('positive_event')),
    ('涨停+新高', lambda r: r.get('is_limitup') and bool(r.get('new_high_type'))),
    ('涨停+龙虎榜', lambda r: r.get('is_limitup') and r.get('has_lhb')),
    ('新高+龙虎榜', lambda r: bool(r.get('new_high_type')) and r.get('has_lhb')),
    ('事件+龙虎榜', lambda r: r.get('has_event') and r.get('has_lhb')),
    ('情绪锚点+Top题材', lambda r: r.get('emotion_anchor') and r.get('is_top_theme')),
]


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def today_str() -> str:
    return datetime.now(tz=CST).strftime('%Y-%m-%d')


def _as_path(path: str | Path | None) -> Path | None:
    return Path(path) if path else None


def _connect_readonly(path: str | Path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    uri = f"file:{path.resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _dict_rows(conn: sqlite3.Connection, table: str, date_col: str, trade_date: str | None = None) -> list[dict]:
    conn.row_factory = sqlite3.Row
    try:
        if trade_date:
            cur = conn.execute(f"SELECT * FROM {table} WHERE {date_col}=?", (trade_date,))
        else:
            cur = conn.execute(f"SELECT * FROM {table}")
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def introspect_strategy_scoreboard(strategy_db_path) -> dict:
    path = _as_path(strategy_db_path)
    result = {
        'path': str(path) if path else None,
        'available': False,
        'tables': [],
        'forward_return_available': False,
        'forward_return_table': None,
        'forward_return_fields': [],
        'rows': [],
        'error': None,
    }
    if not path or not path.exists():
        result['error'] = 'strategy_scoreboard.db 不存在'
        return result
    try:
        with _connect_readonly(path) as conn:
            conn.row_factory = sqlite3.Row
            tables = _table_names(conn)
            result['available'] = True
            result['tables'] = tables
            for table in tables:
                cols = _columns(conn, table)
                fields = [f for f in FORWARD_FIELDS if f in cols]
                if {'trade_date', 'code'} <= set(cols) and fields:
                    result['forward_return_available'] = True
                    result['forward_return_table'] = table
                    result['forward_return_fields'] = fields
                    cur = conn.execute(f"SELECT * FROM {table}")
                    result['rows'] = [dict(r) for r in cur.fetchall()]
                    break
    except Exception as exc:
        result['error'] = str(exc)
    return result


def introspect_market_db(market_db_path) -> dict:
    path = _as_path(market_db_path)
    result = {'path': str(path) if path else None, 'available': False, 'tables': [], 'error': None}
    if not path or not path.exists():
        result['error'] = 'ashare_monitor.db 不存在'
        return result
    try:
        with _connect_readonly(path) as conn:
            result['available'] = True
            result['tables'] = _table_names(conn)
    except Exception as exc:
        result['error'] = str(exc)
    return result


def load_shortline_context(output_root: str | Path | None, trade_date: str | None) -> dict:
    root = Path(output_root or DEFAULT_OUTPUT_ROOT)
    paths = []
    if trade_date:
        paths.append(root / trade_date / 'shortline-daily-context.json')
    paths.append(root / 'shortline-daily-context.json')
    for path in paths:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                data['_context_path'] = str(path)
                return data
            except Exception as exc:
                return {'observation_pool': {}, 'source_errors': {'shortline_context': str(exc)}, '_context_path': str(path)}
    return {'observation_pool': {}, 'source_errors': {'shortline_context': '未找到 shortline-daily-context.json'}, '_context_path': None}


def load_validation_inputs(shortline_db_path, strategy_db_path=None, market_db_path=None, trade_date=None, output_root=None) -> dict:
    db_path = Path(shortline_db_path or DEFAULT_DB_PATH)
    data = {t: [] for t in SHORTLINE_TABLES}
    source_errors = {}
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                for table in SHORTLINE_TABLES:
                    date_col = 'event_date' if table == 'event_calendar' else 'trade_date'
                    data[table] = _dict_rows(conn, table, date_col, trade_date)
        except Exception as exc:
            source_errors['shortline_db'] = str(exc)
    else:
        source_errors['shortline_db'] = 'shortline DB 不存在'
    shortline_context = load_shortline_context(output_root, trade_date)
    if shortline_context.get('source_errors'):
        source_errors.update(shortline_context.get('source_errors') or {})
    strategy_data = introspect_strategy_scoreboard(strategy_db_path or DEFAULT_STRATEGY_DB_PATH)
    market_data = introspect_market_db(market_db_path or DEFAULT_MARKET_DB_PATH)
    if strategy_data.get('error'):
        source_errors['strategy_scoreboard'] = strategy_data['error']
    if market_data.get('error'):
        source_errors['market_db'] = market_data['error']
    return {
        'trade_date': trade_date,
        'shortline_db_path': str(db_path),
        'strategy_db_path': str(strategy_db_path or DEFAULT_STRATEGY_DB_PATH),
        'market_db_path': str(market_db_path or DEFAULT_MARKET_DB_PATH),
        'output_root': str(output_root or DEFAULT_OUTPUT_ROOT),
        'shortline_data': data,
        'shortline_context': shortline_context,
        'strategy_data': strategy_data,
        'market_data': market_data,
        'source_errors': source_errors,
    }


def extract_observation_pool(shortline_context: dict) -> list[dict]:
    pool = shortline_context.get('observation_pool') or {}
    rows = []
    if isinstance(pool, list):
        iterable = [('Unknown', pool)]
    else:
        iterable = [(layer, pool.get(layer) or []) for layer in ['A', 'B', 'C']]
    for layer, items in iterable:
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row['layer'] = row.get('layer') or layer
            row['code'] = str(row.get('code') or '').zfill(6)[-6:] if row.get('code') else None
            rows.append(row)
    return rows


def _code(v) -> str | None:
    if v is None:
        return None
    s = ''.join(ch for ch in str(v) if ch.isdigit())
    return s[-6:].zfill(6) if s else None


def _top_themes(theme_rows: list[dict]) -> set[str]:
    sorted_rows = sorted(theme_rows, key=lambda x: float(x.get('score') or 0), reverse=True)
    return {r.get('theme_name') for r in sorted_rows[:5] if r.get('theme_name')}


def build_signal_matrix(inputs: dict) -> list[dict]:
    data = inputs.get('shortline_data') or {}
    trade_date = inputs.get('trade_date')
    candidates = {r.get('code'): dict(r) for r in extract_observation_pool(inputs.get('shortline_context') or {}) if r.get('code')}
    top_themes = _top_themes(data.get('theme_daily') or [])

    def ensure(code, name=None):
        code = _code(code)
        if not code:
            return None
        row = candidates.setdefault(code, {'code': code, 'name': name or code, 'layer': 'Unclassified', 'reasons': []})
        if name and (not row.get('name') or row.get('name') == code):
            row['name'] = name
        row.setdefault('trade_date', trade_date)
        return row

    for r in data.get('theme_stock_map') or []:
        row = ensure(r.get('code'), r.get('name'))
        if not row: continue
        row['theme_name'] = row.get('theme_name') or r.get('theme_name')
        row['role'] = row.get('role') or r.get('role')
        row['is_top_theme'] = bool(r.get('theme_name') in top_themes)
        src = str(r.get('source') or '')
        if 'broken' in src or r.get('role') == '负反馈': row['same_theme_big_loss'] = True
        if 'downlimit' in src: row['is_downlimit'] = True
        if src == 'strong' and not row.get('is_top_theme'): row['only_strong_auxiliary'] = True

    theme_by_name = {r.get('theme_name'): r for r in data.get('theme_daily') or []}
    for r in data.get('limitup_daily') or []:
        row = ensure(r.get('code'), r.get('name'))
        if not row: continue
        row['theme_name'] = row.get('theme_name') or r.get('theme')
        src = str(r.get('source') or '')
        if 'limitup' in src: row['is_limitup'] = True
        if 'broken' in src or r.get('is_broken_board'): row['is_broken'] = True
        if 'downlimit' in src: row['is_downlimit'] = True
        row['consecutive_board_count'] = int(float(r.get('consecutive_board_count') or 0))
        if row.get('theme_name') in top_themes: row['is_top_theme'] = True

    for r in data.get('new_high_daily') or []:
        row = ensure(r.get('code'), r.get('name'))
        if not row: continue
        row['new_high_type'] = r.get('high_type')
        row['theme_name'] = row.get('theme_name') or r.get('theme_name')
        if row.get('theme_name') in top_themes: row['is_top_theme'] = True

    for r in data.get('lhb_daily') or []:
        row = ensure(r.get('code'), r.get('name'))
        if not row: continue
        row['has_lhb'] = True
        net = float(r.get('net_buy') or 0)
        row['lhb_net_buy'] = net
        row['lhb_net_buy_positive'] = net > 0
        row['lhb_net_sell'] = net < 0
        row['institution_net_buy_positive'] = float(r.get('institution_net_buy') or 0) > 0
        row['lhb_quant_flag'] = bool(r.get('quant_flag'))
        row['lhb_hot_money_flag'] = bool(r.get('known_hot_money_flag'))

    for r in data.get('event_calendar') or []:
        row = ensure(r.get('code'), r.get('name'))
        if not row: continue
        row['has_event'] = True
        row['event_type'] = r.get('event_type')
        row['theme_name'] = row.get('theme_name') or r.get('theme_name')
        impact = str(r.get('expected_impact') or '')
        title = str(r.get('title') or '') + str(r.get('event_type') or '')
        if '正向' in impact: row['positive_event'] = True
        if '负向' in impact or any(k in title for k in ['减持', '解禁', '监管']):
            row['negative_event'] = True
            row['event_risk'] = True
        if row.get('theme_name') in top_themes: row['is_top_theme'] = True

    for r in data.get('emotion_anchors') or []:
        row = ensure(r.get('code'), r.get('name'))
        if not row: continue
        row['emotion_anchor'] = True
        row['anchor_type'] = r.get('anchor_type')
        if float(r.get('impact_score') or 0) < 0:
            row['same_theme_big_loss'] = True
        row['theme_name'] = row.get('theme_name') or r.get('theme_name')
        if row.get('theme_name') in top_themes: row['is_top_theme'] = True

    for row in candidates.values():
        theme = theme_by_name.get(row.get('theme_name'))
        if theme and float(theme.get('broken_count') or 0) >= 3:
            row['top_theme_broken_high'] = bool(row.get('is_top_theme'))
        row.setdefault('is_top_theme', row.get('theme_name') in top_themes if row.get('theme_name') else False)
        row.setdefault('trade_date', trade_date)
    return list(candidates.values())


def attach_forward_returns(signal_rows: list[dict], strategy_data: dict, market_data: dict) -> list[dict]:
    rows_by_key = {}
    for r in strategy_data.get('rows') or []:
        c = _code(r.get('code'))
        d = r.get('trade_date') or r.get('date')
        if c and d:
            rows_by_key[(str(d), c)] = r
    out = []
    fields = strategy_data.get('forward_return_fields') or []
    for row in signal_rows:
        item = dict(row)
        perf = rows_by_key.get((str(item.get('trade_date')), _code(item.get('code')))) if rows_by_key else None
        has = False
        if perf:
            for f in fields:
                if f in perf and perf.get(f) is not None:
                    item[f] = perf.get(f)
                    has = True
        item['has_forward_return'] = bool(has)
        out.append(item)
    return out


def _avg(vals):
    vals = [float(v) for v in vals if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def _win_rate(vals):
    vals = [float(v) for v in vals if v is not None]
    return round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else None


def _perf_for(rows: list[dict]) -> dict:
    d = {'sample_count': len(rows), 'with_forward_return_count': sum(1 for r in rows if r.get('has_forward_return'))}
    for f in ['forward_1d', 'forward_3d', 'forward_5d', 'forward_10d', 'forward_20d']:
        d[f'avg_{f}'] = _avg([r.get(f) for r in rows])
        d[f'win_rate_{f[-2:]}'] = _win_rate([r.get(f) for r in rows])
    d['max_adverse'] = min([float(r.get('max_adverse')) for r in rows if r.get('max_adverse') is not None], default=None)
    if d['with_forward_return_count'] == 0:
        d['note'] = '暂无足够后续表现数据'
    return d


def summarize_layer_performance(signal_rows: list[dict]) -> dict:
    result = {}
    for layer in ['A', 'B', 'C', 'Unclassified']:
        result[layer] = _perf_for([r for r in signal_rows if (r.get('layer') or 'Unclassified') == layer])
    return result


def summarize_combo_performance(signal_rows: list[dict]) -> dict:
    total = len(signal_rows) or 1
    result = {}
    for name, pred in COMBO_DEFINITIONS:
        rows = [r for r in signal_rows if pred(r)]
        perf = _perf_for(rows)
        perf['coverage'] = round(len(rows) / total, 4)
        perf['data_quality'] = '有后续表现数据' if perf['with_forward_return_count'] else '暂无足够表现数据，仅验证覆盖率'
        perf['shadow_score_recommendation'] = '建议纳入shadow score' if len(rows) >= 1 else '暂不纳入评分'
        result[name] = perf
    return result


def summarize_negative_feedback(signal_rows: list[dict], inputs: dict | None = None) -> dict:
    definitions = {
        '炸板': lambda r: r.get('is_broken'),
        '跌停': lambda r: r.get('is_downlimit'),
        '同题材大面': lambda r: r.get('same_theme_big_loss'),
        '龙虎榜净卖出': lambda r: r.get('lhb_net_sell'),
        '负向事件': lambda r: r.get('negative_event') or r.get('event_risk'),
        '新高但炸板': lambda r: r.get('new_high_type') and r.get('is_broken'),
        'Top题材broken_count高': lambda r: r.get('top_theme_broken_high'),
    }
    out = {}
    for name, pred in definitions.items():
        rows = [r for r in signal_rows if pred(r)]
        out[name] = {
            'sample_count': len(rows),
            'codes': [r.get('code') for r in rows],
            'suggest_as_penalty': bool(rows) or name in {'炸板', '跌停', '龙虎榜净卖出', '负向事件'},
            'reason': '属于短线风险信号，建议作为shadow扣分项；需继续累计样本验证。',
        }
    return out


def compute_shadow_score(candidate: dict, context: dict) -> dict:
    score = 0
    factors, penalties, notes = [], [], ['评分只用于shadow validation，不是正式策略，不构成投资建议。']
    def add(cond, pts, label):
        nonlocal score
        if cond:
            score += pts; factors.append(f'{label} +{pts}')
    def sub(cond, pts, label):
        nonlocal score
        if cond:
            score -= pts; penalties.append(f'{label} -{pts}')
    role = str(candidate.get('role') or '')
    nh = str(candidate.get('new_high_type') or '')
    add(candidate.get('is_top_theme'), 15, 'Top题材')
    add('龙头' in role, 15, '角色龙头')
    add('中军' in role, 12, '角色中军')
    add('补涨' in role, 6, '角色补涨')
    add(candidate.get('is_limitup'), 12, '涨停')
    add((candidate.get('consecutive_board_count') or 0) >= 3, 10, '连板>=3')
    add(nh == '100日新高', 8, '100日新高')
    add(nh == '250日新高', 12, '250日新高')
    add(candidate.get('lhb_net_buy_positive'), 8, '龙虎榜净买')
    add(candidate.get('institution_net_buy_positive'), 6, '机构净买')
    add(candidate.get('positive_event'), 6, '正向事件')
    add(candidate.get('emotion_anchor'), 10, '情绪锚点')
    sub(candidate.get('is_broken'), 18, '炸板')
    sub(candidate.get('is_downlimit'), 25, '跌停')
    sub(candidate.get('same_theme_big_loss'), 15, '同题材大面')
    sub(candidate.get('lhb_net_sell'), 10, '龙虎榜净卖出')
    sub(candidate.get('negative_event') or candidate.get('event_risk'), 12, '减持/解禁/监管风险')
    sub(candidate.get('data_insufficient'), 8, '数据不足')
    sub(candidate.get('only_strong_auxiliary') and not (candidate.get('is_limitup') or candidate.get('new_high_type') or candidate.get('is_top_theme')), 8, '仅strong辅助信号')
    score = max(0, min(100, score))
    major = any(candidate.get(k) for k in ['is_downlimit', 'is_broken', 'negative_event', 'event_risk'])
    if major or score < 30:
        level = 'Risk'
    elif score >= 70:
        level = 'A_shadow'
    elif score >= 50:
        level = 'B_shadow'
    else:
        level = 'C_shadow'
    return {'score': score, 'score_level': level, 'factors': factors, 'penalties': penalties, 'notes': notes}


def build_shadow_score_table(signal_rows: list[dict]) -> list[dict]:
    out = []
    for row in signal_rows:
        scored = compute_shadow_score(row, {})
        out.append({**{k: row.get(k) for k in ['trade_date','code','name','theme_name','layer','role']}, **scored})
    return sorted(out, key=lambda x: x.get('score') or 0, reverse=True)


def build_data_range(data: dict, trade_date: str | None) -> dict:
    dates = []
    for table, rows in data.items():
        key = 'event_date' if table == 'event_calendar' else 'trade_date'
        dates.extend([r.get(key) for r in rows if r.get(key)])
    return {'start': min(dates) if dates else trade_date, 'end': max(dates) if dates else trade_date, 'trade_date': trade_date}


def build_sample_coverage(rows: list[dict], data: dict) -> dict:
    return {
        'observation_pool_count': len(rows),
        'A_count': sum(1 for r in rows if r.get('layer') == 'A'),
        'B_count': sum(1 for r in rows if r.get('layer') == 'B'),
        'C_count': sum(1 for r in rows if r.get('layer') == 'C'),
        'limitup_count': sum(1 for r in rows if r.get('is_limitup')),
        'top_theme_count': sum(1 for r in rows if r.get('is_top_theme')),
        'new_high_count': sum(1 for r in rows if r.get('new_high_type')),
        'lhb_count': sum(1 for r in rows if r.get('has_lhb')),
        'event_count': sum(1 for r in rows if r.get('has_event')),
        'emotion_anchor_count': sum(1 for r in rows if r.get('emotion_anchor')),
        'with_forward_return_count': sum(1 for r in rows if r.get('has_forward_return')),
        'without_forward_return_count': sum(1 for r in rows if not r.get('has_forward_return')),
    }


def shadow_score_rules() -> dict:
    return {
        'positive_weights': {'Top题材': 15, '龙头': 15, '中军': 12, '补涨': 6, '涨停': 12, '连板>=3': 10, '100日新高': 8, '250日新高': 12, '龙虎榜净买': 8, '机构净买': 6, '正向事件': 6, '情绪锚点': 10},
        'penalties': {'炸板': -18, '跌停': -25, '同题材大面': -15, '龙虎榜净卖出': -10, '减持/解禁/监管风险': -12, '数据不足': -8, '仅strong辅助信号': -8},
        'levels': {'A_shadow': 'score>=70', 'B_shadow': '50<=score<70', 'C_shadow': '30<=score<50', 'Risk': 'score<30或重大负反馈'},
        'scope': 'shadow validation only',
    }


def build_missing_fields(inputs: dict, rows: list[dict]) -> dict:
    data = inputs.get('shortline_data') or {}
    return {
        'empty_tables': [t for t in SHORTLINE_TABLES if not data.get(t)],
        'missing_context': not bool((inputs.get('shortline_context') or {}).get('_context_path')),
        'strategy_scoreboard_available': bool((inputs.get('strategy_data') or {}).get('available')),
        'forward_return_available': bool((inputs.get('strategy_data') or {}).get('forward_return_available')),
        'missing_dates': [] if rows else [inputs.get('trade_date') or '未指定日期'],
        'field_notes': ['若无forward return，仅输出覆盖率、共振分布和待验证建议。'] if not any(r.get('has_forward_return') for r in rows) else [],
    }


def build_validation_context(inputs: dict) -> dict:
    raw_rows = build_signal_matrix(inputs)
    rows = attach_forward_returns(raw_rows, inputs.get('strategy_data') or {}, inputs.get('market_data') or {})
    sample = build_sample_coverage(rows, inputs.get('shortline_data') or {})
    context = {
        'ok': True,
        'generated_at': now_iso(),
        'validation_date': inputs.get('trade_date') or today_str(),
        'trade_date': inputs.get('trade_date'),
        'paths': {'shortline_db_path': inputs.get('shortline_db_path'), 'strategy_db_path': inputs.get('strategy_db_path'), 'market_db_path': inputs.get('market_db_path'), 'output_root': inputs.get('output_root')},
        'data_range': build_data_range(inputs.get('shortline_data') or {}, inputs.get('trade_date')),
        'data_status': {'strategy_db_available': bool((inputs.get('strategy_data') or {}).get('available')), 'market_db_available': bool((inputs.get('market_data') or {}).get('available')), 'forward_return_available': bool((inputs.get('strategy_data') or {}).get('forward_return_available'))},
        'sample_coverage': sample,
        'layer_performance': summarize_layer_performance(rows),
        'combo_performance': summarize_combo_performance(rows),
        'negative_feedback': summarize_negative_feedback(rows, inputs),
        'shadow_score_rules': shadow_score_rules(),
        'shadow_score_table': build_shadow_score_table(rows),
        'threshold_recommendations': build_threshold_recommendations(rows, sample),
        'signals_not_ready': build_signals_not_ready(inputs, rows),
        'validation_plan': build_validation_plan(),
        'missing_fields': build_missing_fields(inputs, rows),
        'source_errors': inputs.get('source_errors') or {},
        'risk_disclaimer': '当前验证可能样本不足；历史表现不代表未来收益；规则评分只是shadow analysis，不构成投资建议，不应直接据此交易。',
    }
    return context


def build_threshold_recommendations(rows: list[dict], sample: dict) -> dict:
    a_count = sample.get('A_count') or 0
    risky_b = sum(1 for r in rows if r.get('layer') == 'B' and any(r.get(k) for k in ['is_broken','is_downlimit','negative_event','lhb_net_sell']))
    return {
        'A_layer_threshold': '建议提高门槛：A层至少要求Top题材，并叠加涨停/100或250日新高/龙虎榜正反馈/正向事件之一；若A层数量超过全池40%，应继续收紧。' if a_count else '暂无A层样本，暂不调整。',
        'B_layer_filter': 'B层应过滤明显负反馈。' if risky_b else 'B层暂未发现明显风险混入，继续观察。',
        'C_layer_risk_tags': 'C层建议增加炸板、跌停、负向事件、龙虎榜净卖出、同题材大面等风险标签。',
        'A_required_conditions': ['Top题材', '无重大负反馈', '至少一项强共振'],
        'bonus_only_conditions': ['龙虎榜席位弱识别', '普通正向事件', '60日新高'],
        'observation_only_conditions': ['strong但非涨停非新高非Top题材', '字段缺失较多的事件信号'],
    }


def build_signals_not_ready(inputs: dict, rows: list[dict]) -> list[dict]:
    missing = [t for t in SHORTLINE_TABLES if not (inputs.get('shortline_data') or {}).get(t)]
    out = []
    if missing:
        out.append({'signal': '空表相关信号', 'reason': f'数据缺失率高：{missing}', 'recommendation': '暂不建议纳入正式评分'})
    out.extend([
        {'signal': '龙虎榜席位类型弱识别', 'reason': '依赖关键词，样本和准确性不足', 'recommendation': '只用于shadow观察'},
        {'signal': '政策/行业事件', 'reason': '事件数据源和字段稳定性仍需观察', 'recommendation': '暂不直接进入正式评分'},
        {'signal': '强势股池但非涨停的单独信号', 'reason': '容易误判承接强度', 'recommendation': '只能作为观察项'},
    ])
    if not any(r.get('has_forward_return') for r in rows):
        out.append({'signal': '所有收益验证结论', 'reason': '暂无足够后续表现数据', 'recommendation': '不能作为正式策略依据'})
    return out


def build_validation_plan() -> dict:
    return {
        'min_trading_days': 20,
        'daily_data_to_save': ['observation_pool A/B/C', 'sidecar原始信号', '1/3/5/10/20日forward return', '最大不利波动', '人工复盘标签'],
        'human_vs_system': '比较人工复盘主线、系统Top题材、A/B/C分层和shadow score排序的一致性。',
        'effectiveness_judgement': '若A_shadow在多周期表现、胜率和回撤上持续优于B/C，且样本不少于20个交易日，可考虑进入阶段7评估。',
        'stage7_condition': '数据源稳定、缺失率可控、弱规则信号不作为核心因子、用户确认生产接入范围。',
    }


def _fmt_num(v):
    if v is None: return '—'
    try: return f'{float(v):.4f}'
    except Exception: return str(v)


def _line_items(items, empty='- 未获取到有效数据'):
    return '\n'.join(items) if items else empty


def render_strategy_validation_markdown(context: dict) -> str:
    lines = ['# A 股短线信号验证报告', '']
    p = context.get('paths') or {}; sc = context.get('sample_coverage') or {}
    lines += ['## 0. 数据时间信息', f"- generated_at: {context.get('generated_at')}", f"- validation_date / trade_date: {context.get('validation_date')} / {context.get('trade_date')}", f"- shortline DB path: {p.get('shortline_db_path')}", f"- strategy DB path: {p.get('strategy_db_path')}", f"- market DB path: {p.get('market_db_path')}", f"- 数据覆盖区间: {context.get('data_range')}", f"- 样本数量: {sc.get('observation_pool_count', 0)}", f"- 是否具备 forward return 数据: {context.get('data_status', {}).get('forward_return_available')}", '']
    lines += ['## 1. 样本覆盖情况']
    for k, v in sc.items(): lines.append(f'- {k}: {v}')
    lines.append('')
    lines += ['## 2. A/B/C 观察池验证']
    for layer, perf in (context.get('layer_performance') or {}).items():
        lines.append(f"- {layer}: 样本 {perf.get('sample_count')}，有后续表现 {perf.get('with_forward_return_count')}，1日平均 {_fmt_num(perf.get('avg_forward_1d'))}，3日平均 {_fmt_num(perf.get('avg_forward_3d'))}，5日平均 {_fmt_num(perf.get('avg_forward_5d'))}，10日平均 {_fmt_num(perf.get('avg_forward_10d'))}，20日平均 {_fmt_num(perf.get('avg_forward_20d'))}，胜率 {_fmt_num(perf.get('win_rate_1d'))}，最大不利波动 {_fmt_num(perf.get('max_adverse'))}。{perf.get('note') or ''}")
    if not context.get('data_status', {}).get('forward_return_available'):
        lines += ['- 暂无足够后续表现数据。', '- 当前只能验证信号覆盖率和分层合理性。']
    lines.append('')
    lines += ['## 3. 共振组合分析']
    for name, perf in (context.get('combo_performance') or {}).items():
        lines.append(f"- {name}: 样本数 {perf.get('sample_count')}，覆盖率 {perf.get('coverage')}，平均后续表现1日 {_fmt_num(perf.get('avg_forward_1d'))}，胜率 {_fmt_num(perf.get('win_rate_1d'))}，数据质量：{perf.get('data_quality')}，结论：{perf.get('shadow_score_recommendation')}。")
    lines.append('')
    lines += ['## 4. 负反馈分析']
    for name, item in (context.get('negative_feedback') or {}).items():
        rec = '建议作为扣分项' if item.get('suggest_as_penalty') else '暂不纳入评分'
        lines.append(f"- {name}: 样本数 {item.get('sample_count')}，{rec}；理由：{item.get('reason')}")
    lines.append('')
    rules = context.get('shadow_score_rules') or {}
    lines += ['## 5. Shadow Score 建议', '- score = Top题材分 + 角色分 + 涨停/连板分 + 新高分 + 龙虎榜分 + 事件分 + 情绪锚点分 - 炸板/跌停扣分 - 同题材大面扣分 - 负向事件扣分 - 龙虎榜净卖出扣分 - 数据不足扣分。', '- 这是 shadow score，不是正式策略，不构成交易建议。', '- 加分权重：']
    for k, v in (rules.get('positive_weights') or {}).items(): lines.append(f'  - {k}: +{v}')
    lines.append('- 扣分权重：')
    for k, v in (rules.get('penalties') or {}).items(): lines.append(f'  - {k}: {v}')
    lines.append('- 分层：A_shadow >=70；B_shadow 50-70；C_shadow 30-50；Risk <30或重大负反馈。')
    lines.append('')
    thr = context.get('threshold_recommendations') or {}
    lines += ['## 6. 观察池门槛建议', f"- A 层是否应该提高门槛: {thr.get('A_layer_threshold')}", f"- B 层是否应该过滤负反馈: {thr.get('B_layer_filter')}", f"- C 层是否应该增加风险标签: {thr.get('C_layer_risk_tags')}", f"- A 层必要条件: {', '.join(thr.get('A_required_conditions') or [])}", f"- 只能作为加分项: {', '.join(thr.get('bonus_only_conditions') or [])}", f"- 只能作为观察项: {', '.join(thr.get('observation_only_conditions') or [])}", '']
    lines += ['## 7. 不建议纳入正式策略的信号']
    for item in context.get('signals_not_ready') or []:
        lines.append(f"- {item.get('signal')}: {item.get('reason')}；{item.get('recommendation')}")
    lines.append('')
    plan = context.get('validation_plan') or {}
    lines += ['## 8. 后续验证计划', f"- 建议至少积累交易日: {plan.get('min_trading_days')}", f"- 每日应保存: {', '.join(plan.get('daily_data_to_save') or [])}", f"- 人工复盘对比: {plan.get('human_vs_system')}", f"- 有效性判断: {plan.get('effectiveness_judgement')}", f"- 进入阶段7条件: {plan.get('stage7_condition')}", '']
    mf = context.get('missing_fields') or {}
    lines += ['## 9. 数据缺失说明', f"- 空表: {mf.get('empty_tables')}", f"- 字段缺失/说明: {mf.get('field_notes')}", f"- 日期缺失: {mf.get('missing_dates')}", f"- strategy_scoreboard.db 是否可用: {mf.get('strategy_scoreboard_available')}", f"- forward return 是否可用: {mf.get('forward_return_available')}", '- source_errors:']
    for k, v in (context.get('source_errors') or {}).items(): lines.append(f'  - {k}: {v}')
    lines.append('- 数据不足会使结论只能停留在覆盖率和规则合理性层面。')
    lines.append('')
    lines += ['## 10. 风险提示', '- 当前验证可能样本不足。', '- 历史表现不代表未来收益。', '- 规则评分只是 shadow analysis。', '- 不构成投资建议。', '- 不应直接据此交易。', '']
    return '\n'.join(lines)


def run_strategy_validation(trade_date=None, db_path=None, strategy_db_path=None, market_db_path=None, output_root=None, all_dates=False) -> dict:
    if all_dates and not trade_date:
        trade_date = None
    else:
        trade_date = trade_date or today_str()
    root = Path(output_root or DEFAULT_OUTPUT_ROOT)
    inputs = load_validation_inputs(db_path or DEFAULT_DB_PATH, strategy_db_path or DEFAULT_STRATEGY_DB_PATH, market_db_path or DEFAULT_MARKET_DB_PATH, trade_date, root)
    context = build_validation_context(inputs)
    md = render_strategy_validation_markdown(context)
    root.mkdir(parents=True, exist_ok=True)
    root_json = root / 'strategy-validation-context.json'
    root_md = root / 'strategy-validation-report.md'
    root_json.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
    root_md.write_text(md, encoding='utf-8')
    paths = {'json_path': str(root_json), 'markdown_path': str(root_md)}
    if trade_date:
        day_dir = root / trade_date
        day_dir.mkdir(parents=True, exist_ok=True)
        day_json = day_dir / 'strategy-validation-context.json'
        day_md = day_dir / 'strategy-validation-report.md'
        day_json.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
        day_md.write_text(md, encoding='utf-8')
        paths.update({'daily_json_path': str(day_json), 'daily_markdown_path': str(day_md)})
    return {'ok': True, 'context': context, 'paths': paths}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Shortline shadow signal validation')
    g = parser.add_mutually_exclusive_group()
    g.add_argument('--today', action='store_true')
    g.add_argument('--trade-date')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    parser.add_argument('--strategy-db-path', default=str(DEFAULT_STRATEGY_DB_PATH))
    parser.add_argument('--market-db-path', default=str(DEFAULT_MARKET_DB_PATH))
    parser.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument('--all-dates', action='store_true')
    args = parser.parse_args(argv)
    trade_date = today_str() if args.today else args.trade_date
    result = run_strategy_validation(trade_date, args.db_path, args.strategy_db_path, args.market_db_path, args.output_root, all_dates=args.all_dates)
    print(json.dumps({'ok': result['ok'], 'paths': result['paths']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
