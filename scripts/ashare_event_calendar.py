#!/usr/bin/env python3
"""Stage 4C sidecar: A-share event calendar collector and resonance analyzer.

Scope guard:
- writes only shadow ``event_calendar`` and shadow JSON/Markdown files;
- reads shortline sidecar tables for theme/limit-up/new-high/LHB resonance;
- does not modify production databases, close/opening report generators, cron or Feishu.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd


class _AkShareProxy:
    """Lazy AkShare proxy so unit tests never import/network-touch AkShare unless needed."""

    def __init__(self):
        object.__setattr__(self, '_module', None)
        object.__setattr__(self, '_overrides', {})

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, '_overrides')
        if name in overrides:
            return overrides[name]
        module = object.__getattribute__(self, '_module')
        if module is None:
            try:
                import akshare as imported_ak
            except Exception as exc:  # pragma: no cover
                raise AttributeError(f'akshare unavailable: {exc}') from exc
            object.__setattr__(self, '_module', imported_ak)
            module = imported_ak
        return getattr(module, name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, '_overrides')[name] = value

    def __delattr__(self, name):
        object.__getattribute__(self, '_overrides').pop(name, None)


ak = _AkShareProxy()

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
CST = timezone(timedelta(hours=8))
NEW_HIGH_TYPES = {'60日新高', '100日新高', '250日新高'}
MISSING_TEXT = {'', '-', '--', '—', 'None', 'none', 'nan', 'NaN', 'null'}
FORBIDDEN_ADVICE_WORDS = ('买入', '必涨', '必做', '确定性买入', '必须买入', '建议买入', '满仓')

DATE_ALIASES = ('公告日期', '披露日期', '事件日期', '日期', 'date', 'event_date')
CODE_ALIASES = ('代码', '股票代码', '证券代码', 'code', 'symbol')
NAME_ALIASES = ('名称', '股票简称', '证券简称', 'name')
TITLE_ALIASES = ('公告标题', '标题', '事件标题', 'title')
TYPE_ALIASES = ('公告类型', '事件类型', '类型', 'event_type')
DESC_ALIASES = ('摘要', '内容', 'reason', 'description')
AMOUNT_ALIASES = ('金额', '变动金额', '解禁市值', '回购金额', '比例', '解禁比例')


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def today_text() -> str:
    return datetime.now(tz=CST).date().isoformat()


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() in MISSING_TEXT


def pick(row: dict, aliases: tuple[str, ...] | list[str]) -> Any:
    for key in aliases:
        if key in row and not is_missing(row.get(key)):
            return row.get(key)
    return None


def normalize_code(code) -> str | None:
    text = re.sub(r'\D', '', str(code or ''))
    if not text:
        return None
    return text[-6:].zfill(6)


def normalize_event_date(value) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r'\d{8}', text):
        return f'{text[:4]}-{text[4:6]}-{text[6:8]}'
    text = text.replace('/', '-').replace('.', '-')
    m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if m:
        return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    try:
        return pd.to_datetime(text).date().isoformat()
    except Exception:
        return None


def classify_event_type(title: str, raw_type: str | None = None) -> str:
    text = f'{raw_type or ""} {title or ""}'
    if any(k in text for k in ('业绩预告', '预增', '预减', '扭亏', '续亏')):
        return '业绩预告'
    if any(k in text for k in ('年度报告', '季度报告', '半年度报告', '财报')):
        return '财报披露'
    if '回购' in text:
        return '回购'
    if '增持' in text:
        return '增持'
    if '减持' in text:
        return '减持'
    if any(k in text for k in ('解禁', '限售股上市流通')):
        return '解禁'
    if '停牌' in text:
        return '停牌'
    if '复牌' in text:
        return '复牌'
    if any(k in text for k in ('异常波动', '异动公告')):
        return '异常波动'
    if any(k in text for k in ('监管函', '问询函', '处罚', '立案')):
        return '监管风险'
    if any(k in text for k in ('行业会议', '大会', '论坛')):
        return '行业会议'
    if any(k in text for k in ('政策', '规划', '指导意见')):
        return '政策事件'
    if any(k in text for k in ('股东', '实控人', '控股股东')):
        return '股东变化'
    return '其他事件'


def score_event_importance(event: dict) -> dict:
    event_type = event.get('event_type') or classify_event_type(event.get('title') or '', None)
    title = event.get('title') or ''
    importance = 40
    reasons = []
    if event_type in {'监管风险', '减持', '解禁', '停牌', '复牌', '异常波动', '业绩预告', '回购'}:
        importance += 30
        reasons.append(f'{event_type}属于高关注事件')
    if event.get('is_top_theme'):
        importance += 15
        reasons.append('属于Top题材')
    for key, label in [('is_limitup', '涨停'), ('is_new_high', '新高'), ('is_lhb', '龙虎榜')]:
        if event.get(key):
            importance += 10
            reasons.append(f'同日{label}联动')
    importance = max(0, min(100, importance))
    if event_type in {'回购', '增持'} or any(k in title for k in ('预增', '扭亏')):
        impact = '正向关注'
    elif event_type in {'减持', '监管风险', '解禁'} or any(k in title for k in ('处罚', '立案', '解禁压力')):
        impact = '负向风险'
    elif event_type in {'财报披露', '公司公告', '股东变化', '其他事件'}:
        impact = '中性观察'
    else:
        impact = '事件不确定'
    return {'importance': importance, 'expected_impact': impact, 'reason': '；'.join(reasons) or '基础事件规则评分'}


def normalize_event_row(row: dict, event_date: str | None, source: str) -> dict:
    raw = dict(row or {})
    missing_fields = []
    date_value = pick(raw, DATE_ALIASES) or event_date
    normalized_date = normalize_event_date(date_value)
    if normalized_date is None:
        missing_fields.append('event_date')
        normalized_date = normalize_event_date(event_date) or today_text()
    code = normalize_code(pick(raw, CODE_ALIASES))
    name = pick(raw, NAME_ALIASES)
    raw_type = pick(raw, TYPE_ALIASES)
    title = pick(raw, TITLE_ALIASES)
    temp_type = classify_event_type(str(title or ''), str(raw_type) if raw_type else None)
    if is_missing(title):
        missing_fields.append('title')
        title = f'{temp_type}-{name or code or "无代码事件"}'
    event_type = classify_event_type(str(title), str(raw_type) if raw_type else None)
    description = pick(raw, DESC_ALIASES)
    amount_or_ratio = pick(raw, AMOUNT_ALIASES)
    payload = {'raw': raw, 'missing_fields': missing_fields, 'description': description, 'amount_or_ratio': amount_or_ratio}
    base = {
        'event_date': normalized_date,
        'event_type': event_type,
        'code': code,
        'name': str(name).strip() if name is not None else None,
        'theme_name': raw.get('theme_name'),
        'title': str(title).strip(),
        'source': source or 'unknown',
        'raw_json': json.dumps(payload, ensure_ascii=False, default=str),
    }
    score = score_event_importance(base)
    base.update(score)
    return base


def _df_to_rows(df: Any) -> list[dict]:
    if isinstance(df, pd.DataFrame) and not df.empty:
        return df.to_dict('records')
    return []


def _call_source(func_name: str, variants: list[dict[str, Any]], event_date: str, source: str) -> tuple[list[dict], str | None]:
    func = getattr(ak, func_name, None)
    if func is None:
        return [], f'{func_name}: unavailable'
    errors = []
    for kwargs in variants:
        try:
            df = func(**kwargs)
            return [normalize_event_row(row, event_date, source) for row in _df_to_rows(df)], None
        except TypeError as exc:
            errors.append(f'TypeError: {exc}')
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{type(exc).__name__}: {exc}')
            break
    return [], '; '.join(errors) if errors else None


def collect_event_calendar_data(event_date: str) -> dict:
    ed = normalize_event_date(event_date) or today_text()
    compact = ed.replace('-', '')
    sources = [
        ('stock_notice_report', [{'date': compact}, {'symbol': '全部'}, {}], 'akshare_notice'),
        ('stock_yjyg_em', [{'date': compact}, {'symbol': '全部'}, {}], 'akshare_financial_report'),
        ('stock_hold_management_detail_em', [{'date': compact}, {}], 'akshare_shareholder'),
        ('stock_repurchase_em', [{'date': compact}, {}], 'akshare_repurchase'),
        ('stock_restricted_release_summary_em', [{'date': compact}, {}], 'akshare_unlock'),
    ]
    event_rows: list[dict] = []
    source_errors: dict[str, str] = {}
    for func_name, variants, source in sources:
        rows, err = _call_source(func_name, variants, ed, source)
        if rows:
            # Keep only events for the target date when source provides dates; no invented filtering if date missing.
            event_rows.extend([r for r in rows if r.get('event_date') == ed])
        if err:
            source_errors[source] = err
    if not event_rows:
        source_errors.setdefault('policy_industry_events', '未获取到有效政策/行业事件')
    return {'event_date': ed, 'event_rows': event_rows, 'source_errors': source_errors, 'fetched_at': now_iso()}


def upsert_event_calendar(conn: sqlite3.Connection, rows: list[dict]) -> None:
    now = now_iso()
    for row in rows or []:
        if not row.get('event_date') or not row.get('event_type') or not row.get('title'):
            continue
        created_at = now
        if row.get('code') is None:
            existing = conn.execute(
                'SELECT created_at FROM event_calendar WHERE event_date=? AND event_type=? AND code IS NULL AND title=?',
                (row['event_date'], row['event_type'], row['title']),
            ).fetchone()
            if existing:
                created_at = existing['created_at']
            conn.execute(
                'DELETE FROM event_calendar WHERE event_date=? AND event_type=? AND code IS NULL AND title=?',
                (row['event_date'], row['event_type'], row['title']),
            )
            conn.execute(
                '''INSERT INTO event_calendar (
                    event_date, event_type, code, name, theme_name, title, importance, expected_impact,
                    source, raw_json, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (row['event_date'], row['event_type'], row.get('name'), row.get('theme_name'), row['title'], row.get('importance'), row.get('expected_impact'), row.get('source'), row.get('raw_json'), created_at, now),
            )
        else:
            existing = conn.execute(
                'SELECT created_at FROM event_calendar WHERE event_date=? AND event_type=? AND code=? AND title=?',
                (row['event_date'], row['event_type'], row['code'], row['title']),
            ).fetchone()
            if existing:
                created_at = existing['created_at']
            conn.execute(
                '''INSERT INTO event_calendar (
                    event_date, event_type, code, name, theme_name, title, importance, expected_impact,
                    source, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_date, event_type, title, code) DO UPDATE SET
                    name=excluded.name,
                    theme_name=excluded.theme_name,
                    importance=excluded.importance,
                    expected_impact=excluded.expected_impact,
                    source=excluded.source,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at''',
                (row['event_date'], row['event_type'], row.get('code'), row.get('name'), row.get('theme_name'), row['title'], row.get('importance'), row.get('expected_impact'), row.get('source'), row.get('raw_json'), created_at, now),
            )
    conn.commit()


def load_event_rows(conn: sqlite3.Connection, event_date: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM event_calendar WHERE event_date=? ORDER BY COALESCE(importance,0) DESC, code, title', (normalize_event_date(event_date),)).fetchall()
    return [dict(r) for r in rows]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _map_table(conn, table: str, event_date: str, where: str = '', params: tuple = ()) -> dict[str, dict]:
    if not table_exists(conn, table):
        return {}
    rows = conn.execute(f'SELECT * FROM {table} WHERE {"trade_date" if table != "event_calendar" else "event_date"}=? {where}', (event_date, *params)).fetchall()
    return {str(r['code']).zfill(6)[-6:]: dict(r) for r in rows if 'code' in r.keys() and r['code']}


def _theme_context(conn: sqlite3.Connection, event_date: str) -> tuple[dict[str, list[dict]], set[str], dict[str, dict]]:
    stock_map: dict[str, list[dict]] = {}
    if table_exists(conn, 'theme_stock_map'):
        for row in conn.execute('SELECT * FROM theme_stock_map WHERE trade_date=?', (event_date,)):
            stock_map.setdefault(str(row['code']).zfill(6)[-6:], []).append(dict(row))
    top_names: set[str] = set()
    theme_daily: dict[str, dict] = {}
    if table_exists(conn, 'theme_daily'):
        for row in conn.execute('SELECT * FROM theme_daily WHERE trade_date=? ORDER BY COALESCE(score,0) DESC LIMIT 5', (event_date,)):
            d = dict(row)
            top_names.add(d.get('theme_name'))
            theme_daily[d.get('theme_name')] = d
    return stock_map, top_names, theme_daily


def _best_theme(themes: list[dict], top_names: set[str], theme_daily: dict[str, dict]) -> dict | None:
    if not themes:
        return None
    return sorted(themes, key=lambda r: (r.get('theme_name') in top_names, float((theme_daily.get(r.get('theme_name')) or {}).get('score') or 0), float(r.get('confidence') or 0)), reverse=True)[0]


def _missing_from_raw(row: dict) -> list[str]:
    try:
        return list((json.loads(row.get('raw_json') or '{}')).get('missing_fields') or [])
    except Exception:
        return []


def build_event_summary(conn: sqlite3.Connection, event_date: str, source_errors: dict | None = None) -> dict:
    ed = normalize_event_date(event_date) or today_text()
    events = load_event_rows(conn, ed)
    stock_themes, top_theme_names, theme_daily = _theme_context(conn, ed)
    limitup = _map_table(conn, 'limitup_daily', ed)
    new_high = _map_table(conn, 'new_high_daily', ed, "AND high_type IN ('60日新高','100日新高','250日新高')")
    lhb = _map_table(conn, 'lhb_daily', ed)
    missing_fields: dict[str, list[str]] = {'event_calendar': []}
    enriched = []
    for row in events:
        code = str(row.get('code') or '').zfill(6)[-6:] if row.get('code') else None
        theme = _best_theme(stock_themes.get(code, []) if code else [], top_theme_names, theme_daily)
        lu = limitup.get(code) if code else None
        nh = new_high.get(code) if code else None
        lhb_row = lhb.get(code) if code else None
        item = dict(row)
        if theme and not item.get('theme_name'):
            item['theme_name'] = theme.get('theme_name')
        item.update({
            'is_top_theme': bool(item.get('theme_name') in top_theme_names),
            'is_limitup': bool(lu and 'limitup' in str(lu.get('source') or '')),
            'is_new_high': bool(nh),
            'high_type': nh.get('high_type') if nh else None,
            'is_lhb': bool(lhb_row),
            'theme_negative': bool(item.get('theme_name') and (theme_daily.get(item.get('theme_name')) or {}).get('broken_count')),
        })
        score = score_event_importance(item)
        item.update(score)
        enriched.append(item)
        for field in _missing_from_raw(row):
            if field not in missing_fields['event_calendar']:
                missing_fields['event_calendar'].append(field)
        if code and code not in stock_themes and 'theme_mapping' not in missing_fields['event_calendar']:
            missing_fields['event_calendar'].append('theme_mapping')
        if code and code not in limitup and 'limitup_mapping' not in missing_fields['event_calendar']:
            missing_fields['event_calendar'].append('limitup_mapping')
        if code and code not in new_high and 'new_high_mapping' not in missing_fields['event_calendar']:
            missing_fields['event_calendar'].append('new_high_mapping')
        if code and code not in lhb and 'lhb_mapping' not in missing_fields['event_calendar']:
            missing_fields['event_calendar'].append('lhb_mapping')
    high = [e for e in enriched if (e.get('importance') or 0) >= 70]
    positive = [e for e in enriched if e.get('expected_impact') == '正向关注' or e.get('is_top_theme')]
    negative = [e for e in enriched if e.get('expected_impact') == '负向风险' or e.get('theme_negative')]
    watch = []
    for e in sorted(set([id(x) for x in high + positive + negative]) and enriched, key=lambda x: x.get('importance') or 0, reverse=True):
        if (e in high or e in positive or e in negative or e.get('is_limitup') or e.get('is_new_high') or e.get('is_lhb')):
            watch.append({
                'code': e.get('code'), 'name': e.get('name'), 'theme_name': e.get('theme_name'), 'event_type': e.get('event_type'),
                'title': e.get('title'), 'watch_point': _watch_point(e), 'expected_impact': e.get('expected_impact'), 'importance': e.get('importance'),
            })
    if not events:
        missing_fields['event_calendar'].append('notice_data')
    return {
        'event_date': ed,
        'event_count': len(events),
        'high_importance_events': high[:20],
        'positive_watch_events': positive[:20],
        'negative_risk_events': negative[:20],
        'event_theme_resonance': [e for e in enriched if e.get('is_top_theme')],
        'event_limitup_resonance': [e for e in enriched if e.get('is_limitup')],
        'event_new_high_resonance': [e for e in enriched if e.get('is_new_high')],
        'event_lhb_resonance': [e for e in enriched if e.get('is_lhb')],
        'tomorrow_watchlist': watch[:30],
        'missing_fields': missing_fields,
        'source_errors': source_errors or {},
        'sources': sorted({e.get('source') for e in events if e.get('source')}),
        'generated_at': now_iso(),
    }


def _watch_point(e: dict) -> str:
    parts = []
    if e.get('expected_impact') == '负向风险' or e.get('theme_negative'):
        parts.append('防负反馈与承接不足')
    if e.get('is_top_theme'):
        parts.append('观察题材强度与资金反馈')
    if e.get('is_limitup') or e.get('is_new_high') or e.get('is_lhb'):
        parts.append('跟踪事件与资金/量能是否延续')
    if not parts:
        parts.append('观察公告后市场反馈')
    return '；'.join(parts)


def stock_label(item: dict) -> str:
    if item.get('code'):
        return f"{item.get('name') or '未命名'}({item.get('code')})"
    return item.get('theme_name') or '无代码事件'


def _table(lines: list[str], items: list[dict], cols: list[str], renderer) -> None:
    if not items:
        lines.append('- 暂无。')
        return
    lines.append('| ' + ' | '.join(cols) + ' |')
    lines.append('|' + '|'.join(['---'] * len(cols)) + '|')
    for item in items:
        lines.append('| ' + ' | '.join(str(x) if x is not None else '—' for x in renderer(item)) + ' |')


def render_event_markdown(summary: dict) -> str:
    lines = [
        '# A 股事件日历 sidecar 复盘', '', f"事件日期：{summary.get('event_date')}", '',
        '## 1. 总览',
        f"- 事件数量：{summary.get('event_count', 0)}",
        f"- 高重要性事件数量：{len(summary.get('high_importance_events') or [])}",
        f"- 正向关注事件数量：{len(summary.get('positive_watch_events') or [])}",
        f"- 负向风险事件数量：{len(summary.get('negative_risk_events') or [])}",
        f"- 数据源：{', '.join(summary.get('sources') or []) or '—'}",
        f"- 数据时间：{summary.get('generated_at') or '—'}",
    ]
    if not summary.get('event_count'):
        lines.append('- 未获取到有效事件。')
    lines.extend(['', '## 2. 高重要性事件'])
    _table(lines, summary.get('high_importance_events') or [], ['日期', '股票/题材', '事件类型', '标题', '重要性', '影响方向', '规则说明'], lambda i: [i.get('event_date'), stock_label(i), i.get('event_type'), i.get('title'), i.get('importance'), i.get('expected_impact'), i.get('reason')])
    lines.extend(['', '## 3. 正向关注事件'])
    _table(lines, summary.get('positive_watch_events') or [], ['股票/题材', '事件类型', '标题', '影响方向', '题材'], lambda i: [stock_label(i), i.get('event_type'), i.get('title'), i.get('expected_impact'), i.get('theme_name')])
    lines.extend(['', '## 4. 负向风险事件'])
    _table(lines, summary.get('negative_risk_events') or [], ['股票/题材', '事件类型', '标题', '影响方向', '风险点'], lambda i: [stock_label(i), i.get('event_type'), i.get('title'), i.get('expected_impact'), '同题材负反馈' if i.get('theme_negative') else i.get('reason')])
    lines.extend(['', '## 5. 事件 + 主线题材共振'])
    _table(lines, summary.get('event_theme_resonance') or [], ['股票', 'Top题材', '事件类型', '标题'], lambda i: [stock_label(i), i.get('theme_name'), i.get('event_type'), i.get('title')])
    lines.extend(['', '## 6. 事件 + 涨停 / 新高 / 龙虎榜共振', '### 6.1 事件 + 涨停'])
    _table(lines, summary.get('event_limitup_resonance') or [], ['股票', '题材', '事件类型', '标题'], lambda i: [stock_label(i), i.get('theme_name'), i.get('event_type'), i.get('title')])
    lines.append('### 6.2 事件 + 新高')
    _table(lines, summary.get('event_new_high_resonance') or [], ['股票', '题材', '新高类型', '标题'], lambda i: [stock_label(i), i.get('theme_name'), i.get('high_type'), i.get('title')])
    lines.append('### 6.3 事件 + 龙虎榜')
    _table(lines, summary.get('event_lhb_resonance') or [], ['股票', '题材', '事件类型', '标题'], lambda i: [stock_label(i), i.get('theme_name'), i.get('event_type'), i.get('title')])
    lines.extend(['', '## 7. 明日观察清单'])
    _table(lines, summary.get('tomorrow_watchlist') or [], ['股票/题材', '事件', '观察点', '影响方向'], lambda i: [stock_label(i), i.get('title'), i.get('watch_point'), i.get('expected_impact')])
    lines.extend(['', '## 8. 数据缺失说明'])
    for source, fields in (summary.get('missing_fields') or {}).items():
        lines.append(f"- {source}：{', '.join(fields) if fields else '无'}")
    errors = summary.get('source_errors') or {}
    if errors:
        lines.append('- source_errors：')
        for source, err in errors.items():
            lines.append(f'  - {source}: {err}')
    else:
        lines.append('- source_errors：无')
    lines.extend(['', '## 9. 风险提示', '- 公告和事件数据可能延迟或字段变化。', '- 事件利好/利空不等于股价涨跌。', '- 事件影响需要结合市场环境、题材强度、量能和资金反馈确认。', '- 本报告只用于复盘辅助，不构成投资建议。'])
    md = '\n'.join(lines) + '\n'
    for word in ('确定性买入', '必须买入', '建议买入'):
        md = md.replace(word, '观察')
    return md


def run_event_calendar(event_date: str | None = None, db_path: str | Path = DEFAULT_DB_PATH, output_root: str | Path = DEFAULT_OUTPUT_ROOT, skip_fetch: bool = False, source_errors: dict | None = None) -> dict:
    ed = normalize_event_date(event_date) or today_text()
    db_path = Path(db_path).expanduser()
    output_root = Path(output_root).expanduser()
    import ashare_shortline_schema as schema
    schema.init_db(db_path)
    fetched = {'event_rows': [], 'source_errors': source_errors or {}}
    with schema.connect(db_path) as conn:
        if not skip_fetch:
            fetched = collect_event_calendar_data(ed)
            upsert_event_calendar(conn, fetched.get('event_rows') or [])
        errors = {**(source_errors or {}), **(fetched.get('source_errors') or {})}
        summary = build_event_summary(conn, ed, source_errors=errors)
    out_dir = output_root / ed
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / 'event-calendar.json'
    md_path = out_dir / 'event-calendar.md'
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    md_path.write_text(render_event_markdown(summary), encoding='utf-8')
    return {'summary': summary, 'paths': {'json_path': str(json_path), 'markdown_path': str(md_path), 'db_path': str(db_path)}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='A-share event calendar sidecar collector and analyzer')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--today', action='store_true')
    group.add_argument('--event-date')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    parser.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument('--skip-fetch', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ed = today_text() if args.today else (args.event_date or today_text())
    result = run_event_calendar(ed, db_path=args.db_path, output_root=args.output_root, skip_fetch=args.skip_fetch)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
