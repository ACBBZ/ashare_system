#!/usr/bin/env python3
"""Stage 5 shadow report: A-share shortline integrated daily report.

Scope guard:
- reads shadow shortline DB sidecar tables and optional production market DB read-only;
- writes only shadow JSON/Markdown output files;
- does not modify production report generators, cron, Feishu, or production DBs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_MARKET_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
CST = timezone(timedelta(hours=8))
TABLE_DATE_COLUMNS = {
    'limitup_daily': 'trade_date',
    'theme_daily': 'trade_date',
    'theme_stock_map': 'trade_date',
    'emotion_anchors': 'trade_date',
    'new_high_daily': 'trade_date',
    'lhb_daily': 'trade_date',
    'event_calendar': 'event_date',
}
NEW_HIGH_TYPES = {'60日新高', '100日新高', '250日新高'}
RISK_DISCLAIMER = (
    '公开数据可能延迟或字段变化。题材识别为规则归类，可能有误。'
    '龙虎榜席位类型为弱规则识别，可能不准确。事件利好/利空不等于股价涨跌。'
    '新高不等于买点。本报告只用于复盘辅助，不构成投资建议。'
)


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def today_text() -> str:
    return datetime.now(tz=CST).date().isoformat()


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def as_int(value: Any, default: int = 0) -> int:
    f = as_float(value)
    return default if f is None else int(f)


def rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row['name'] if isinstance(row, sqlite3.Row) else row[1] for row in conn.execute(f'PRAGMA table_info({table_name})')}


def load_shortline_tables(conn, trade_date: str) -> dict:
    """Read all shadow sidecar tables for one trade date, degrading on missing tables."""
    out = {'source_errors': {}, 'missing_fields': {}}
    for table, date_col in TABLE_DATE_COLUMNS.items():
        if not table_exists(conn, table):
            out[table] = []
            out['source_errors'][table] = 'table missing'
            continue
        cols = table_columns(conn, table)
        if date_col not in cols:
            out[table] = []
            out['source_errors'][table] = f'{date_col} missing'
            continue
        try:
            out[table] = rows_to_dicts(conn.execute(f'SELECT * FROM {table} WHERE {date_col}=?', (trade_date,)).fetchall())
        except Exception as exc:
            out[table] = []
            out['source_errors'][table] = str(exc)
    return out


def _open_market_db_readonly(market_db_path: str | Path):
    path = Path(market_db_path)
    uri = f'file:{path}?mode=ro'
    return sqlite3.connect(uri, uri=True)


def _read_market_table(conn, table_name: str, trade_date: str, date_cols=('trade_date', 'date', 'capture_date')) -> list[dict]:
    conn.row_factory = sqlite3.Row
    if not table_exists(conn, table_name):
        return []
    cols = table_columns(conn, table_name)
    date_col = next((c for c in date_cols if c in cols), None)
    try:
        if date_col:
            return rows_to_dicts(conn.execute(f'SELECT * FROM {table_name} WHERE {date_col}=?', (trade_date,)).fetchall())
        return rows_to_dicts(conn.execute(f'SELECT * FROM {table_name} LIMIT 200').fetchall())
    except Exception:
        return []


def load_market_snapshot(market_db_path, trade_date: str) -> dict:
    """Read optional production market DB strictly in SQLite read-only mode."""
    if not market_db_path:
        return {'available': False, 'path': None, 'source_errors': {'market_db': 'not configured'}}
    path = Path(market_db_path)
    if not path.exists():
        return {'available': False, 'path': str(path), 'source_errors': {'market_db': 'file missing'}}
    try:
        conn = _open_market_db_readonly(path)
    except Exception as exc:
        return {'available': False, 'path': str(path), 'source_errors': {'market_db': str(exc)}}
    try:
        data = {
            'available': True,
            'path': str(path),
            'capture_runs': _read_market_table(conn, 'capture_runs', trade_date),
            'index_snapshots': _read_market_table(conn, 'index_snapshots', trade_date),
            'sector_snapshots': _read_market_table(conn, 'sector_snapshots', trade_date),
            'sector_constituent_snapshots': _read_market_table(conn, 'sector_constituent_snapshots', trade_date),
            'watchlist_snapshots': _read_market_table(conn, 'watchlist_snapshots', trade_date),
            'stock_snapshots': _read_market_table(conn, 'stock_snapshots', trade_date),
            'source_errors': {},
        }
        if not any(data[k] for k in data if k not in {'available','path','source_errors'}):
            data['source_errors']['market_db'] = 'no usable rows for trade_date or known tables missing'
        return data
    finally:
        conn.close()


def build_data_status(shortline_data: dict, market_data: dict) -> dict:
    mapping = {
        '涨停生态': 'limitup_daily',
        '题材情绪': 'theme_daily',
        '百日新高': 'new_high_daily',
        '龙虎榜': 'lhb_daily',
        '事件日历': 'event_calendar',
    }
    status = {}
    for label, key in mapping.items():
        count = len(shortline_data.get(key) or [])
        status[label] = {'status': 'ok' if count else 'missing', 'rows': count}
    market_rows = 0
    if market_data.get('available'):
        market_rows = sum(len(v) for k, v in market_data.items() if isinstance(v, list))
    status['市场快照'] = {'status': 'ok' if market_rows else 'missing', 'rows': market_rows, 'path': market_data.get('path')}
    return status


def _top_themes(shortline_data: dict, n=5) -> list[dict]:
    return sorted(shortline_data.get('theme_daily') or [], key=lambda r: as_float(r.get('score'), 0) or 0, reverse=True)[:n]


def _theme_map_by_code(shortline_data: dict) -> dict[str, list[dict]]:
    by = {}
    for row in shortline_data.get('theme_stock_map') or []:
        by.setdefault(str(row.get('code')), []).append(row)
    return by


def _top_theme_names(shortline_data: dict) -> set[str]:
    return {r.get('theme_name') for r in _top_themes(shortline_data) if r.get('theme_name')}


def _is_limitup(row: dict) -> bool:
    return 'limitup' in str(row.get('source') or '') and not as_int(row.get('is_broken_board'))


def _is_broken(row: dict) -> bool:
    source = str(row.get('source') or '')
    return bool(as_int(row.get('is_broken_board'))) or 'broken' in source


def _is_downlimit(row: dict) -> bool:
    return 'downlimit' in str(row.get('source') or '')


def build_limitup_section(shortline_data: dict) -> dict:
    rows = shortline_data.get('limitup_daily') or []
    limitups = [r for r in rows if _is_limitup(r)]
    broken = [r for r in rows if _is_broken(r)]
    downlimits = [r for r in rows if _is_downlimit(r)]
    ladder = {}
    for r in limitups:
        board = as_int(r.get('consecutive_board_count'))
        if board:
            ladder.setdefault(str(board), []).append({'code': r.get('code'), 'name': r.get('name'), 'theme': r.get('theme')})
    seal_top = sorted(limitups, key=lambda r: as_float(r.get('seal_amount'), 0) or 0, reverse=True)[:10]
    strong_overlap = []
    for r in rows:
        src = str(r.get('source') or '')
        if 'strong' in src and 'limitup' in src:
            strong_overlap.append({'code': r.get('code'), 'name': r.get('name'), 'theme': r.get('theme')})
    return {
        'limitup_count': len(limitups),
        'downlimit_count': len(downlimits),
        'broken_count': len(broken),
        'max_board': max([as_int(r.get('consecutive_board_count')) for r in limitups] or [0]),
        'board_ladder': ladder,
        'seal_amount_top': [{'code': r.get('code'), 'name': r.get('name'), 'theme': r.get('theme'), 'seal_amount': r.get('seal_amount')} for r in seal_top],
        'theme_reason_groups': _group_count(limitups, 'theme'),
        'strong_limitup_overlap': strong_overlap,
        'missing_fields': [] if rows else ['limitup_daily empty'],
    }


def _group_count(rows: list[dict], key: str) -> list[dict]:
    counts = {}
    for r in rows:
        v = r.get(key) or '未归类'
        counts[v] = counts.get(v, 0) + 1
    return [{'name': k, 'count': v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)]


def build_theme_section(shortline_data: dict) -> list[dict]:
    top_names = _top_theme_names(shortline_data)
    nh_codes = {r.get('code') for r in shortline_data.get('new_high_daily') or [] if r.get('high_type') in NEW_HIGH_TYPES}
    lhb_codes = {r.get('code') for r in shortline_data.get('lhb_daily') or []}
    event_codes = {r.get('code') for r in shortline_data.get('event_calendar') or [] if r.get('code')}
    mapped = shortline_data.get('theme_stock_map') or []
    out = []
    for t in _top_themes(shortline_data):
        theme_name = t.get('theme_name')
        stocks = [m for m in mapped if m.get('theme_name') == theme_name]
        out.append({
            'theme_name': theme_name,
            'status': t.get('status'),
            'score': t.get('score'),
            'limitup_count': t.get('limitup_count'),
            'broken_count': t.get('broken_count'),
            'leading_stock': {'code': t.get('leading_stock_code'), 'name': t.get('leading_stock_name')},
            'middle_stock': {'code': t.get('middle_stock_code'), 'name': t.get('middle_stock_name')},
            'supplement_stocks': [{'code': s.get('code'), 'name': s.get('name'), 'role': s.get('role')} for s in stocks if s.get('role') in {'补涨','后排'}],
            'negative_stock': {'code': t.get('negative_stock_code'), 'name': t.get('negative_stock_name')},
            'is_top_theme': theme_name in top_names,
            'new_high_resonance': any(s.get('code') in nh_codes for s in stocks),
            'lhb_resonance': any(s.get('code') in lhb_codes for s in stocks),
            'event_resonance': any(s.get('code') in event_codes for s in stocks),
        })
    return out


def build_emotion_section(shortline_data: dict) -> list[dict]:
    return [{
        'anchor_type': r.get('anchor_type'),
        'code': r.get('code'),
        'name': r.get('name'),
        'theme_name': r.get('theme_name'),
        'impact_score': r.get('impact_score'),
        'note': r.get('note'),
        'source': r.get('source'),
    } for r in (shortline_data.get('emotion_anchors') or [])]


def build_new_high_section(shortline_data: dict) -> dict:
    rows = shortline_data.get('new_high_daily') or []
    limitup_by_code = {r.get('code'): r for r in shortline_data.get('limitup_daily') or []}
    top_theme_names = _top_theme_names(shortline_data)
    counts = {k: 0 for k in ['60日新高','100日新高','250日新高','历史不足']}
    theme_counts = {}
    limitup_res = []
    top_theme_res = []
    risks = []
    for r in rows:
        ht = r.get('high_type') or '历史不足'
        counts[ht] = counts.get(ht, 0) + 1
        theme = r.get('theme_name') or r.get('sector_name') or '未归类'
        if ht in NEW_HIGH_TYPES:
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
        lu = limitup_by_code.get(r.get('code'))
        item = {'code': r.get('code'), 'name': r.get('name'), 'theme_name': theme, 'high_type': ht}
        if lu and _is_limitup(lu) and ht in NEW_HIGH_TYPES:
            limitup_res.append(item)
        if theme in top_theme_names and ht in NEW_HIGH_TYPES:
            top_theme_res.append(item)
        if lu and (_is_broken(lu) or _is_downlimit(lu)):
            risks.append(item | {'risk': '新高叠加炸板/跌停负反馈'})
    return {
        'checked_count': len(rows),
        'count_60d': counts.get('60日新高', 0),
        'count_100d': counts.get('100日新高', 0),
        'count_250d': counts.get('250日新高', 0),
        'insufficient_history_count': counts.get('历史不足', 0),
        'theme_clusters': [{'theme_name': k, 'count': v} for k, v in sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)],
        'new_high_limitup_resonance': limitup_res,
        'new_high_theme_resonance': top_theme_res,
        'new_high_theme_resonance_count': len(top_theme_res),
        'risk_items': risks,
        'missing_fields': [] if rows else ['new_high_daily empty'],
    }


def _theme_for_code(shortline_data: dict, code: str | None) -> str | None:
    for r in shortline_data.get('theme_stock_map') or []:
        if r.get('code') == code:
            return r.get('theme_name')
    return None


def build_lhb_section(shortline_data: dict) -> dict:
    rows = shortline_data.get('lhb_daily') or []
    limitup_codes = {r.get('code') for r in shortline_data.get('limitup_daily') or [] if _is_limitup(r)}
    nh_codes = {r.get('code') for r in shortline_data.get('new_high_daily') or [] if r.get('high_type') in NEW_HIGH_TYPES}
    top_theme_names = _top_theme_names(shortline_data)
    net_buy_top = sorted(rows, key=lambda r: as_float(r.get('net_buy'), -10**18) or -10**18, reverse=True)[:10]
    net_sell_top = sorted(rows, key=lambda r: as_float(r.get('net_buy'), 10**18) or 10**18)[:10]
    def enrich(r):
        return {'code': r.get('code'), 'name': r.get('name'), 'theme_name': _theme_for_code(shortline_data, r.get('code')), 'net_buy': r.get('net_buy'), 'institution_net_buy': r.get('institution_net_buy')}
    return {
        'lhb_count': len(rows),
        'net_buy_top': [enrich(r) for r in net_buy_top if as_float(r.get('net_buy'), 0) is not None],
        'net_sell_top': [enrich(r) for r in net_sell_top if (as_float(r.get('net_buy'), 0) or 0) < 0],
        'institution_net_buy_top': [enrich(r) for r in sorted(rows, key=lambda x: as_float(x.get('institution_net_buy'), -10**18) or -10**18, reverse=True)[:10] if (as_float(r.get('institution_net_buy'), 0) or 0) > 0],
        'quant_flag_items': [enrich(r) for r in rows if as_int(r.get('quant_flag'))],
        'known_hot_money_items': [enrich(r) for r in rows if as_int(r.get('known_hot_money_flag'))],
        'lhb_limitup_resonance': [enrich(r) for r in rows if r.get('code') in limitup_codes],
        'lhb_new_high_resonance': [enrich(r) for r in rows if r.get('code') in nh_codes],
        'lhb_theme_resonance': [enrich(r) for r in rows if _theme_for_code(shortline_data, r.get('code')) in top_theme_names],
        'negative_items': [enrich(r) for r in rows if (as_float(r.get('net_buy'), 0) or 0) < 0],
        'negative_count': sum(1 for r in rows if (as_float(r.get('net_buy'), 0) or 0) < 0),
        'missing_fields': [] if rows else ['lhb_daily empty'],
    }


def build_event_section(shortline_data: dict) -> dict:
    rows = shortline_data.get('event_calendar') or []
    top_theme_names = _top_theme_names(shortline_data)
    limitup_codes = {r.get('code') for r in shortline_data.get('limitup_daily') or [] if _is_limitup(r)}
    nh_codes = {r.get('code') for r in shortline_data.get('new_high_daily') or [] if r.get('high_type') in NEW_HIGH_TYPES}
    lhb_codes = {r.get('code') for r in shortline_data.get('lhb_daily') or []}
    def item(r):
        return {'code': r.get('code'), 'name': r.get('name'), 'theme_name': r.get('theme_name') or _theme_for_code(shortline_data, r.get('code')), 'title': r.get('title'), 'event_type': r.get('event_type'), 'importance': r.get('importance'), 'expected_impact': r.get('expected_impact')}
    high = [item(r) for r in rows if (as_float(r.get('importance'), 0) or 0) >= 80]
    positive = [item(r) for r in rows if r.get('expected_impact') == '正向关注']
    negative = [item(r) for r in rows if r.get('expected_impact') == '负向风险']
    return {
        'event_count': len(rows),
        'high_importance_events': high,
        'positive_events': positive,
        'negative_events': negative,
        'event_theme_resonance': [item(r) for r in rows if (r.get('theme_name') or _theme_for_code(shortline_data, r.get('code'))) in top_theme_names],
        'event_limitup_resonance': [item(r) for r in rows if r.get('code') in limitup_codes],
        'event_new_high_resonance': [item(r) for r in rows if r.get('code') in nh_codes],
        'event_lhb_resonance': [item(r) for r in rows if r.get('code') in lhb_codes],
        'tomorrow_watch_events': high[:10],
        'missing_fields': [] if rows else ['event_calendar empty'],
    }


def _market_index_rows(market_data: dict) -> list[dict]:
    out = []
    for r in market_data.get('index_snapshots') or []:
        name = r.get('index_name') or r.get('name') or r.get('指数名称') or r.get('code') or r.get('index_code')
        pct = r.get('pct') if 'pct' in r else r.get('change_pct') or r.get('涨跌幅')
        amount = r.get('amount') or r.get('成交额')
        out.append({'name': name, 'pct': pct, 'amount': amount})
    return out


def derive_market_regime(context: dict) -> dict:
    limitup = context.get('limitup_ecology') or {}
    themes = context.get('themes') or []
    new_high = context.get('new_high') or {}
    lhb = context.get('lhb') or {}
    anchors = context.get('emotion_anchors') or []
    limitup_count = as_int(limitup.get('limitup_count'))
    broken_count = as_int(limitup.get('broken_count'))
    downlimit_count = as_int(limitup.get('downlimit_count'))
    max_board = as_int(limitup.get('max_board'))
    top_score = max([as_float(t.get('score'), 0) or 0 for t in themes] or [0])
    top_broken = max([as_int(t.get('broken_count')) for t in themes] or [0])
    positive_anchor = sum(1 for a in anchors if (as_float(a.get('impact_score'), 0) or 0) > 0)
    negative_anchor = sum(1 for a in anchors if (as_float(a.get('impact_score'), 0) or 0) < 0)
    reasons = []
    if not limitup_count and not themes and not new_high:
        return {'regime': '未确认', 'confidence': 0.35, 'reasons': ['关键 sidecar 数据不足'], 'suggested_position_band': '0%-10%', 'note': '仓位区间只是复盘参考，不构成交易建议。'}
    if downlimit_count >= 8 or (broken_count >= 15 and limitup_count <= 20) or (top_broken >= 6 and max_board <= 2):
        regime, band, conf = '退潮', '0%-10%', 0.78
        reasons.append('跌停/炸板/题材负反馈明显')
    elif broken_count >= max(8, int(limitup_count * 0.35)) or (top_broken >= 4 and negative_anchor >= positive_anchor):
        regime, band, conf = '分歧', '0%-20%', 0.72
        reasons.append('涨停与炸板/负反馈并存，题材内部有分化')
    elif limitup_count >= 35 and max_board >= 4 and top_score >= 80 and broken_count <= 6 and new_high.get('new_high_theme_resonance_count', 0) >= 1:
        regime, band, conf = '主升', '20%-40%', 0.8
        reasons.append('涨停数量、连板高度、主线得分和趋势共振同时较强')
    elif len([t for t in themes if (as_float(t.get('score'), 0) or 0) >= 60]) >= 3 and top_score < 85:
        regime, band, conf = '轮动', '10%-20%', 0.65
        reasons.append('多个题材有表现但单一主线不够突出')
    elif limitup_count >= 15 and top_score >= 60:
        regime, band, conf = '修复', '10%-30%', 0.62
        reasons.append('涨停与题材表现中等，存在修复线索但强度未充分确认')
    else:
        regime, band, conf = '未确认', '0%-10%', 0.45
        reasons.append('有效信号不足或互相矛盾')
    return {'regime': regime, 'confidence': conf, 'reasons': reasons, 'suggested_position_band': band, 'note': '仓位区间只是复盘参考，不构成交易建议。'}


def _has_negative(shortline_data: dict, code: str) -> bool:
    for r in shortline_data.get('limitup_daily') or []:
        if r.get('code') == code and (_is_broken(r) or _is_downlimit(r)):
            return True
    for e in shortline_data.get('event_calendar') or []:
        if e.get('code') == code and e.get('expected_impact') == '负向风险':
            return True
    for l in shortline_data.get('lhb_daily') or []:
        if l.get('code') == code and (as_float(l.get('net_buy'), 0) or 0) < 0:
            return True
    return False


def build_observation_pool(conn, trade_date: str, context: dict) -> dict:
    shortline_data = context.get('_shortline_data') or load_shortline_tables(conn, trade_date)
    top_theme_names = {t.get('theme_name') for t in context.get('themes', [])[:5]}
    limitup_codes = {r.get('code') for r in shortline_data.get('limitup_daily') or [] if _is_limitup(r)}
    high_codes = {r.get('code') for r in shortline_data.get('new_high_daily') or [] if r.get('high_type') in {'100日新高','250日新高'}}
    lhb_positive = {r.get('code') for r in shortline_data.get('lhb_daily') or [] if (as_float(r.get('net_buy'), 0) or 0) > 0}
    event_positive = {r.get('code') for r in shortline_data.get('event_calendar') or [] if r.get('expected_impact') == '正向关注' and (as_float(r.get('importance'), 0) or 0) >= 80}
    candidates = {}
    def add(code, name, theme, role, reason):
        if not code:
            return
        rec = candidates.setdefault(code, {'code': code, 'name': name or code, 'theme_name': theme, 'role': role, 'reasons': []})
        if reason not in rec['reasons']:
            rec['reasons'].append(reason)
    for m in shortline_data.get('theme_stock_map') or []:
        if m.get('theme_name') in top_theme_names:
            add(m.get('code'), m.get('name'), m.get('theme_name'), m.get('role'), f"Top题材{m.get('theme_name')} / {m.get('role') or '角色待确认'}")
    for a in context.get('emotion_anchors') or []:
        add(a.get('code'), a.get('name'), a.get('theme_name'), a.get('anchor_type'), f"情绪锚点：{a.get('anchor_type')}")
    for r in context.get('new_high', {}).get('new_high_theme_resonance', []):
        add(r.get('code'), r.get('name'), r.get('theme_name'), '趋势', f"{r.get('high_type')} + Top题材")
    for r in context.get('lhb', {}).get('lhb_limitup_resonance', []):
        add(r.get('code'), r.get('name'), r.get('theme_name'), '资金', '涨停 + 龙虎榜共振')
    for r in context.get('events', {}).get('event_theme_resonance', []):
        add(r.get('code'), r.get('name'), r.get('theme_name'), '事件', '事件 + 主线题材共振')
    pool = {'A': [], 'B': [], 'C': []}
    for rec in candidates.values():
        code = rec['code']; theme = rec.get('theme_name'); role = rec.get('role') or ''
        resonances = []
        if code in limitup_codes: resonances.append('涨停')
        if code in high_codes: resonances.append('100/250日新高')
        if code in lhb_positive: resonances.append('龙虎榜正反馈')
        if code in event_positive: resonances.append('高重要性正向事件')
        neg = _has_negative(shortline_data, code)
        if neg or '负反馈' in role:
            layer = 'C'
            risk = ['存在负反馈或数据不充分，需要只记录风险']
        elif theme in top_theme_names and role in {'龙头','中军','空间板','核心龙头','趋势中军'} and resonances:
            layer = 'A'
            risk = ['若承接不足、题材降温或放量滞涨则降低关注']
        elif theme in top_theme_names and (role in {'补涨','后排','中军'} or resonances):
            layer = 'B'
            risk = ['题材轮动或后排分化时容易回落']
        else:
            layer = 'C'
            risk = ['数据不足，仅记录']
        item = {
            'code': code,
            'name': rec.get('name'),
            'theme_name': theme,
            'layer': layer,
            'reasons': rec['reasons'] + resonances,
            'confirm_conditions': ['题材继续位于前排', '量能与承接同步确认', '未出现炸板/跌停/监管负面扩散'],
            'invalidation_conditions': ['跌破关键承接区', '同题材核心转弱', '出现明显负反馈'],
            'risk_notes': risk + ['未满足确认条件前不作为交易依据'],
        }
        pool[layer].append(item)
    return pool


def build_comprehensive_judgement(context: dict) -> dict:
    env = context.get('market_environment') or {}
    themes = context.get('themes') or []
    limitup = context.get('limitup_ecology') or {}
    new_high = context.get('new_high') or {}
    events = context.get('events') or {}
    lhb = context.get('lhb') or {}
    top = themes[0] if themes else {}
    regime = env.get('regime', '未确认')
    if regime in {'主升','修复'}:
        strength = '偏强' if regime == '主升' else '修复'
    elif regime in {'分歧','轮动'}:
        strength = '分歧' if regime == '分歧' else '轮动'
    else:
        strength = '偏弱' if regime == '退潮' else '未确认'
    drivers = []
    if as_int(limitup.get('max_board')) >= 4: drivers.append('连板')
    if new_high.get('new_high_theme_resonance'): drivers.append('趋势')
    if len(themes) >= 3: drivers.append('题材轮动')
    if events.get('event_theme_resonance'): drivers.append('事件驱动')
    return {
        'shortline_environment': strength,
        'mainline_clarity': '较清晰' if top.get('score') and (as_float(top.get('score'), 0) or 0) >= 80 else '不清晰/需确认',
        'profit_effect_source': drivers or ['未确认'],
        'tomorrow_focus': ['Top题材能否延续', '新高趋势是否有承接', '龙虎榜正负反馈是否扩散', '事件方向是否得到市场确认'],
        'avoid_chasing': ['连续加速后缺乏承接的后排', '炸板/跌停所在题材', '仅有事件标题但无量价确认的方向'],
        'reduce_position_signals': ['跌停/炸板明显增加', '核心龙头转弱', 'Top题材 broken_count 上升', '龙虎榜或事件负反馈扩散'],
        'note': '综合复盘，不是交易指令。',
    }


def _load_sidecar_json(output_root: Path, trade_date: str) -> dict:
    names = ['limitup-ecology.json','theme-emotion.json','new-high-radar.json','lhb-sidecar.json','event-calendar.json']
    out = {'missing_json': [], 'source_errors': {}}
    day_dir = Path(output_root) / trade_date
    for name in names:
        path = day_dir / name
        if not path.exists():
            out['missing_json'].append(str(path))
            continue
        try:
            out[name] = json.loads(path.read_text(encoding='utf-8'))
        except Exception as exc:
            out['source_errors'][name] = str(exc)
    return out


def build_context(conn, trade_date: str, shortline_data: dict, market_data: dict, db_path, market_db_path, source_errors: dict | None = None, output_root: str | Path | None = None) -> dict:
    data_status = build_data_status(shortline_data, market_data)
    limitup = build_limitup_section(shortline_data)
    themes = build_theme_section(shortline_data)
    emotion = build_emotion_section(shortline_data)
    new_high = build_new_high_section(shortline_data)
    lhb = build_lhb_section(shortline_data)
    events = build_event_section(shortline_data)
    market_snapshot = {
        'index_rows': _market_index_rows(market_data),
        'available': bool(market_data.get('available')),
        'source_errors': market_data.get('source_errors') or {},
    }
    context = {
        'ok': True,
        'trade_date': trade_date,
        'generated_at': now_iso(),
        'db_path': str(db_path),
        'market_db_path': str(market_db_path) if market_db_path else None,
        'data_status': data_status,
        'market_snapshot': market_snapshot,
        'limitup_ecology': limitup,
        'themes': themes,
        'emotion_anchors': emotion,
        'new_high': new_high,
        'lhb': lhb,
        'events': events,
        'missing_fields': _build_missing_fields(shortline_data, market_data, output_root, trade_date),
        'source_errors': {},
        'risk_disclaimer': RISK_DISCLAIMER,
        '_shortline_data': shortline_data,
    }
    context['market_environment'] = derive_market_regime(context)
    context['observation_pool'] = build_observation_pool(conn, trade_date, context)
    context['comprehensive_judgement'] = build_comprehensive_judgement(context)
    context['source_errors'].update(shortline_data.get('source_errors') or {})
    context['source_errors'].update(market_data.get('source_errors') or {})
    if source_errors:
        context['source_errors'].update(source_errors)
    return context


def _build_missing_fields(shortline_data: dict, market_data: dict, output_root, trade_date: str) -> dict:
    missing = {'empty_tables': [], 'field_notes': [], 'missing_json': [], 'market_db_available': bool(market_data.get('available')), 'akshare_field_stability_risk': True}
    for table in TABLE_DATE_COLUMNS:
        if not shortline_data.get(table):
            missing['empty_tables'].append(table)
    required_cols = {
        'limitup_daily': ['source','consecutive_board_count','seal_amount'],
        'theme_daily': ['score','broken_count'],
        'new_high_daily': ['high_type'],
        'lhb_daily': ['net_buy','institution_net_buy','quant_flag','known_hot_money_flag'],
        'event_calendar': ['importance','expected_impact'],
    }
    for table, cols in required_cols.items():
        rows = shortline_data.get(table) or []
        if rows:
            have = set(rows[0])
            miss = [c for c in cols if c not in have]
            if miss:
                missing['field_notes'].append({table: miss})
    if output_root:
        sidecar = _load_sidecar_json(Path(output_root), trade_date)
        missing['missing_json'] = sidecar['missing_json']
    return missing


def _money(v):
    f = as_float(v)
    if f is None: return '—'
    if abs(f) >= 1e8: return f'{f/1e8:.2f}亿'
    if abs(f) >= 1e4: return f'{f/1e4:.2f}万'
    return f'{f:.0f}'


def _pct(v):
    f = as_float(v)
    return '—' if f is None else f'{f:.2f}%'


def _stock_text(item):
    if not item: return '—'
    code = item.get('code') or '------'
    return f"{item.get('name') or code}（{code}）"


def _list_lines(items, formatter, empty='无有效数据'):
    if not items:
        return [f'- {empty}']
    return [f'- {formatter(x)}' for x in items]


def render_shortline_daily_markdown(context: dict) -> str:
    lines = ['# A 股短线综合日报', '']
    lines += ['## 0. 数据时间信息', f"- trade_date：{context.get('trade_date')}", f"- generated_at：{context.get('generated_at')}", f"- shortline DB path：{context.get('db_path')}", f"- market DB path：{context.get('market_db_path') or '未配置/不可用'}", '- 数据模块状态：']
    for k, v in context.get('data_status', {}).items():
        lines.append(f"  - {k}：{v.get('status')}（rows={v.get('rows')}）")
    lines.append('')
    env = context.get('market_environment', {})
    market = context.get('market_snapshot', {})
    lines += ['## 1. 市场环境']
    if market.get('index_rows'):
        lines.append('- 主要指数表现：' + '；'.join(f"{r.get('name')} {_pct(r.get('pct'))}" for r in market['index_rows'][:5]))
    else:
        lines.append('- 主要指数表现：市场快照缺失，降级为 sidecar 环境判断。')
    le = context.get('limitup_ecology', {})
    lines += [f"- 成交额：{_money(sum(as_float(r.get('amount'), 0) or 0 for r in market.get('index_rows', [])))}", '- 涨跌家数：生产行情库字段可用时补充，当前未确认。', f"- 涨停家数：{le.get('limitup_count', 0)}", f"- 跌停家数：{le.get('downlimit_count', 0)}", f"- 炸板数量：{le.get('broken_count', 0)}", f"- 最高连板：{le.get('max_board', 0)}", f"- 当前环境标签：{env.get('regime')}（confidence={env.get('confidence')}）", f"- 今日适合仓位区间：{env.get('suggested_position_band')}；这只是复盘参考，不是交易指令。"]
    for r in env.get('reasons', []): lines.append(f"  - 原因：{r}")
    lines.append('')
    lines += ['## 2. 涨停生态', f"- 涨停数量：{le.get('limitup_count', 0)}", f"- 跌停数量：{le.get('downlimit_count', 0)}", f"- 炸板数量：{le.get('broken_count', 0)}", f"- 最高连板：{le.get('max_board', 0)}", f"- 连板梯队：{json.dumps(le.get('board_ladder', {}), ensure_ascii=False)}", '- 最大封板资金 Top 10：']
    lines += _list_lines(le.get('seal_amount_top'), lambda x: f"{_stock_text(x)} {x.get('theme') or ''} 封单 {_money(x.get('seal_amount'))}")
    lines.append('- 涨停原因/题材分类：' + '；'.join(f"{x['name']} {x['count']}" for x in le.get('theme_reason_groups', [])) if le.get('theme_reason_groups') else '- 涨停原因/题材分类：缺失')
    lines.append(f"- 强势股池与涨停池重叠：{len(le.get('strong_limitup_overlap') or [])}")
    lines.append(f"- 数据缺失说明：{', '.join(le.get('missing_fields') or []) or '无'}")
    lines.append('')
    lines += ['## 3. 主线题材']
    lines += _list_lines(context.get('themes'), lambda t: f"{t.get('theme_name')}｜status={t.get('status')}｜score={t.get('score')}｜涨停={t.get('limitup_count')}｜炸板={t.get('broken_count')}｜龙头={_stock_text(t.get('leading_stock'))}｜中军={_stock_text(t.get('middle_stock'))}｜补涨={len(t.get('supplement_stocks') or [])}｜负反馈={_stock_text(t.get('negative_stock'))}｜新高共振={t.get('new_high_resonance')}｜龙虎榜共振={t.get('lhb_resonance')}｜事件共振={t.get('event_resonance')}")
    lines.append('')
    lines += ['## 4. 情绪锚点']
    lines += _list_lines(context.get('emotion_anchors'), lambda a: f"{a.get('anchor_type')}：{_stock_text(a)}｜题材={a.get('theme_name')}｜impact_score={a.get('impact_score')}｜note={a.get('note')}｜source={a.get('source')}")
    lines.append('')
    nh = context.get('new_high', {})
    lines += ['## 5. 百日新高与趋势共振', f"- 检查股票数：{nh.get('checked_count', 0)}", f"- 60日新高数量：{nh.get('count_60d', 0)}", f"- 100日新高数量：{nh.get('count_100d', 0)}", f"- 250日新高数量：{nh.get('count_250d', 0)}", f"- 历史不足数量：{nh.get('insufficient_history_count', 0)}", '- 新高集中的题材：' + ('；'.join(f"{x['theme_name']} {x['count']}" for x in nh.get('theme_clusters', [])) or '无'), '- 新高 + 涨停共振：']
    lines += _list_lines(nh.get('new_high_limitup_resonance'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}｜{x.get('high_type')}")
    lines.append('- 新高 + Top 题材共振：')
    lines += _list_lines(nh.get('new_high_theme_resonance'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}｜{x.get('high_type')}")
    lines.append('- 新高但炸板/负反馈风险：')
    lines += _list_lines(nh.get('risk_items'), lambda x: f"{_stock_text(x)}｜{x.get('risk')}")
    lines.append('')
    lhb = context.get('lhb', {})
    lines += ['## 6. 龙虎榜与资金席位', f"- 龙虎榜股票数：{lhb.get('lhb_count', 0)}", '- 净买入 Top：']
    lines += _list_lines(lhb.get('net_buy_top'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}｜净买入 {_money(x.get('net_buy'))}")
    lines.append('- 净卖出 / 负反馈 Top：')
    lines += _list_lines(lhb.get('net_sell_top'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}｜净额 {_money(x.get('net_buy'))}")
    lines.append('- 机构净买入：')
    lines += _list_lines(lhb.get('institution_net_buy_top'), lambda x: f"{_stock_text(x)}｜机构净额 {_money(x.get('institution_net_buy'))}")
    lines.append(f"- 规则识别量化席位：{len(lhb.get('quant_flag_items') or [])}；规则识别游资席位：{len(lhb.get('known_hot_money_items') or [])}。席位类型为规则弱识别，可能不准确。")
    lines.append('- 龙虎榜 + 涨停共振：'); lines += _list_lines(lhb.get('lhb_limitup_resonance'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}")
    lines.append('- 龙虎榜 + 新高共振：'); lines += _list_lines(lhb.get('lhb_new_high_resonance'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}")
    lines.append('- 龙虎榜 + Top 题材共振：'); lines += _list_lines(lhb.get('lhb_theme_resonance'), lambda x: f"{_stock_text(x)}｜{x.get('theme_name')}")
    lines.append('')
    ev = context.get('events', {})
    lines += ['## 7. 事件日历', '- 高重要性事件：']; lines += _list_lines(ev.get('high_importance_events'), lambda x: f"{_stock_text(x)}｜{x.get('event_type')}｜{x.get('title')}｜{x.get('expected_impact')}")
    lines.append('- 正向关注事件：'); lines += _list_lines(ev.get('positive_events'), lambda x: f"{_stock_text(x)}｜{x.get('title')}")
    lines.append('- 负向风险事件：'); lines += _list_lines(ev.get('negative_events'), lambda x: f"{_stock_text(x)}｜{x.get('title')}")
    for label, key in [('事件 + 主线题材共振','event_theme_resonance'),('事件 + 涨停共振','event_limitup_resonance'),('事件 + 新高共振','event_new_high_resonance'),('事件 + 龙虎榜共振','event_lhb_resonance'),('明日观察事件','tomorrow_watch_events')]:
        lines.append(f'- {label}：'); lines += _list_lines(ev.get(key), lambda x: f"{_stock_text(x)}｜{x.get('title')}")
    lines.append('')
    lines += ['## 8. 候选观察池', '- 本阶段不直接生成交易建议；未满足确认条件前不作为交易依据。']
    for layer in ['A','B','C']:
        lines.append(f'### {layer} 层')
        lines += _list_lines(context.get('observation_pool', {}).get(layer), lambda x: f"{_stock_text(x)}｜题材={x.get('theme_name')}｜原因={'、'.join(x.get('reasons') or [])}｜观察条件={'、'.join(x.get('confirm_conditions') or [])}｜放弃条件={'、'.join(x.get('invalidation_conditions') or [])}｜风险={'、'.join(x.get('risk_notes') or [])}")
    lines.append('')
    cj = context.get('comprehensive_judgement', {})
    lines += ['## 9. 综合判断', f"- 今天短线环境：{cj.get('shortline_environment')}", f"- 当前主线是否清晰：{cj.get('mainline_clarity')}", f"- 今日赚钱效应来源：{'、'.join(cj.get('profit_effect_source') or [])}", f"- 明日最需要观察：{'、'.join(cj.get('tomorrow_focus') or [])}", f"- 哪些方向不能追：{'、'.join(cj.get('avoid_chasing') or [])}", f"- 哪些信号出现要降低仓位：{'、'.join(cj.get('reduce_position_signals') or [])}", f"- 说明：{cj.get('note')}", '']
    miss = context.get('missing_fields', {})
    lines += ['## 10. 数据缺失说明', f"- 空表：{', '.join(miss.get('empty_tables') or []) or '无'}", f"- 字段缺失：{json.dumps(miss.get('field_notes') or [], ensure_ascii=False)}", f"- JSON 缺失：{', '.join(miss.get('missing_json') or []) or '未检查或无缺失'}", f"- market DB 是否可用：{miss.get('market_db_available')}", f"- source_errors：{json.dumps(context.get('source_errors') or {}, ensure_ascii=False)}", f"- 是否存在 AkShare 字段不稳定风险：{miss.get('akshare_field_stability_risk')}", '']
    lines += ['## 11. 风险提示', '- 公开数据可能延迟或字段变化。', '- 题材识别为规则归类，可能有误。', '- 龙虎榜席位类型为弱规则识别，可能不准确。', '- 事件利好/利空不等于股价涨跌。', '- 新高不等于买点。', '- 本报告只用于复盘辅助，不构成投资建议。', '']
    return '\n'.join(lines)


def _json_safe_context(context: dict) -> dict:
    return {k: v for k, v in context.items() if not k.startswith('_')}


def run_shortline_daily_report(trade_date: str | None = None, db_path: str | Path | None = None, market_db_path: str | Path | None = DEFAULT_MARKET_DB_PATH, output_root: str | Path | None = DEFAULT_OUTPUT_ROOT) -> dict:
    trade_date = trade_date or today_text()
    db_path = Path(db_path or DEFAULT_DB_PATH)
    output_root = Path(output_root or DEFAULT_OUTPUT_ROOT)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        # create an empty shadow schema only for the selected shadow DB path
        import ashare_shortline_schema as schema
        schema.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        shortline_data = load_shortline_tables(conn, trade_date)
        market_data = load_market_snapshot(market_db_path, trade_date) if market_db_path else {'available': False, 'path': None, 'source_errors': {'market_db': 'not configured'}}
        context = build_context(conn, trade_date, shortline_data, market_data, db_path, market_db_path, {}, output_root)
    finally:
        conn.close()
    day_dir = output_root / trade_date
    day_dir.mkdir(parents=True, exist_ok=True)
    json_path = day_dir / 'shortline-daily-context.json'
    md_path = day_dir / 'shortline-daily-report.md'
    json_path.write_text(json.dumps(_json_safe_context(context), ensure_ascii=False, indent=2), encoding='utf-8')
    md_path.write_text(render_shortline_daily_markdown(context), encoding='utf-8')
    return {'ok': True, 'trade_date': trade_date, 'paths': {'json_path': str(json_path), 'markdown_path': str(md_path)}, 'context': _json_safe_context(context)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Generate A-share shortline integrated daily shadow report')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--today', action='store_true')
    g.add_argument('--trade-date')
    p.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    p.add_argument('--market-db-path', default=str(DEFAULT_MARKET_DB_PATH))
    p.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    trade_date = today_text() if args.today else (args.trade_date or today_text())
    result = run_shortline_daily_report(trade_date, args.db_path, args.market_db_path, args.output_root)
    print(json.dumps({'ok': result['ok'], 'trade_date': result['trade_date'], 'paths': result['paths']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
