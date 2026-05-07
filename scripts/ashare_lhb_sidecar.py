#!/usr/bin/env python3
"""Stage 4B sidecar: A-share 龙虎榜 collector and resonance analyzer.

Scope guard:
- writes only shadow ``lhb_daily`` and shadow JSON/Markdown files;
- reads shortline sidecar tables for limit-up/theme/new-high resonance;
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

try:  # AkShare is optional in tests; all fetch calls are guarded.
    import akshare as ak
except Exception:  # pragma: no cover
    class _MissingAk:
        pass
    ak = _MissingAk()

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
CST = timezone(timedelta(hours=8))
NEW_HIGH_TYPES = {'60日新高', '100日新高', '250日新高'}
MISSING_TEXT = {'', '-', '--', '—', 'None', 'none', 'nan', 'NaN', 'null'}
FORBIDDEN_ADVICE_WORDS = ('建议买入', '必须买入', '确定性机会', '无脑买入', '满仓')

CODE_ALIASES = ('代码', '股票代码', '证券代码', 'code', 'symbol')
NAME_ALIASES = ('名称', '股票简称', '证券简称', 'name')
NET_BUY_ALIASES = ('净买额', '净买入额', '龙虎榜净买额', 'net_buy')
BUY_AMOUNT_ALIASES = ('买入额', '买入金额', 'buy_amount')
SELL_AMOUNT_ALIASES = ('卖出额', '卖出金额', 'sell_amount')
INSTITUTION_NET_ALIASES = ('机构买入净额', '机构净买额', 'institution_net_buy')
REASON_ALIASES = ('上榜原因', '解读', 'reason')
BUY_SEAT_ALIASES = ('买入营业部', '买入席位')
SELL_SEAT_ALIASES = ('卖出营业部', '卖出席位')
SEAT_NAME_ALIASES = ('营业部名称', 'seat_name')

QUANT_KEYWORDS = ('量化', '量化基金', '量化交易', '华鑫证券上海分公司', '中国国际金融上海分公司', '中金公司上海分公司')
HOT_MONEY_KEYWORDS = ('上海溧阳路', '章盟主', '作手新一', '方新侠', '赵老哥', '佛山', '湖里大道')
INSTITUTION_KEYWORDS = ('机构专用', '机构席位')


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def today_text() -> str:
    return datetime.now(tz=CST).date().isoformat()


def normalize_trade_date(value: str | None = None) -> str:
    if not value:
        return date.today().isoformat()
    text = str(value).strip()
    if re.fullmatch(r'\d{8}', text):
        return f'{text[:4]}-{text[4:6]}-{text[6:8]}'
    return text


def ak_date(value: str) -> str:
    return normalize_trade_date(value).replace('-', '')


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() in MISSING_TEXT


def normalize_code(code: Any) -> str | None:
    text = re.sub(r'\D', '', str(code or ''))
    if not text:
        return None
    return text[-6:].zfill(6)


def pick(row: dict, aliases: tuple[str, ...] | list[str]) -> Any:
    for key in aliases:
        if key in row and not is_missing(row.get(key)):
            return row.get(key)
    return None


def safe_float_money(value: Any, unit_hint: str | None = None) -> float | None:
    """Parse money fields and normalize explicit 万/亿 values to yuan."""
    if is_missing(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        out = float(value)
        return None if math.isnan(out) or math.isinf(out) else out
    text = str(value).strip().replace(',', '').replace('，', '')
    multiplier = 1.0
    hint = str(unit_hint or '')
    if '亿' in text or '亿' in hint:
        multiplier = 100000000.0
    elif '万' in text or '万' in hint:
        multiplier = 10000.0
    text = re.sub(r'[亿元万人民币￥\s]', '', text)
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    return float(match.group(0)) * multiplier


def classify_seat_type(seat_name: str | None) -> dict[str, bool]:
    text = str(seat_name or '')
    return {
        'institution_flag': any(k in text for k in INSTITUTION_KEYWORDS),
        'quant_flag': any(k in text for k in QUANT_KEYWORDS),
        'known_hot_money_flag': any(k in text for k in HOT_MONEY_KEYWORDS),
    }


def _seat_list_from_value(value: Any, side: str) -> list[dict[str, Any]]:
    if is_missing(value):
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r'[;；|\n]+', str(value))
    out = []
    for item in raw_items:
        if isinstance(item, dict):
            seat_name = pick(item, SEAT_NAME_ALIASES) or pick(item, BUY_SEAT_ALIASES) or pick(item, SELL_SEAT_ALIASES)
            amount = safe_float_money(pick(item, BUY_AMOUNT_ALIASES if side == 'buy' else SELL_AMOUNT_ALIASES))
            raw = item
        else:
            seat_name = str(item).strip()
            amount = None
            raw = item
        if not seat_name:
            continue
        out.append({'seat_name': str(seat_name), 'amount': amount, 'flags': classify_seat_type(str(seat_name)), 'raw': raw})
    return out


def normalize_lhb_row(row: dict, trade_date: str, source: str) -> dict:
    raw = dict(row or {})
    code = normalize_code(pick(raw, CODE_ALIASES))
    name = pick(raw, NAME_ALIASES)
    net_buy = safe_float_money(pick(raw, NET_BUY_ALIASES), _unit_hint_for(raw, NET_BUY_ALIASES))
    buy_amount = safe_float_money(pick(raw, BUY_AMOUNT_ALIASES), _unit_hint_for(raw, BUY_AMOUNT_ALIASES))
    sell_amount = safe_float_money(pick(raw, SELL_AMOUNT_ALIASES), _unit_hint_for(raw, SELL_AMOUNT_ALIASES))
    if net_buy is None and buy_amount is not None and sell_amount is not None:
        net_buy = buy_amount - sell_amount
    institution_net_buy = safe_float_money(pick(raw, INSTITUTION_NET_ALIASES), _unit_hint_for(raw, INSTITUTION_NET_ALIASES))
    interpretation = pick(raw, REASON_ALIASES)
    buy_seats = _seat_list_from_value(pick(raw, BUY_SEAT_ALIASES), 'buy')
    sell_seats = _seat_list_from_value(pick(raw, SELL_SEAT_ALIASES), 'sell')
    common_seat = pick(raw, SEAT_NAME_ALIASES)
    if common_seat and not buy_seats and not sell_seats:
        buy_seats = _seat_list_from_value(common_seat, 'buy')
    all_flags = [seat['flags'] for seat in buy_seats + sell_seats]
    missing_fields = []
    if not buy_seats and not sell_seats:
        missing_fields.append('seat_detail')
    if net_buy is None:
        missing_fields.append('net_buy')
    if institution_net_buy is None:
        missing_fields.append('institution_net_buy')
    raw_payload = {'raw': raw, 'missing_fields': missing_fields}
    return {
        'trade_date': normalize_trade_date(trade_date),
        'code': code,
        'name': str(name).strip() if name is not None else None,
        'net_buy': net_buy,
        'institution_net_buy': institution_net_buy,
        'buy_seats_json': json.dumps(buy_seats, ensure_ascii=False),
        'sell_seats_json': json.dumps(sell_seats, ensure_ascii=False),
        'known_hot_money_flag': int(any(flag.get('known_hot_money_flag') for flag in all_flags)),
        'quant_flag': int(any(flag.get('quant_flag') for flag in all_flags)),
        'interpretation': str(interpretation).strip() if interpretation is not None else None,
        'source': source,
        'raw_json': json.dumps(raw_payload, ensure_ascii=False, default=str),
    }


def _unit_hint_for(row: dict, aliases: tuple[str, ...] | list[str]) -> str | None:
    keys = set(row)
    for alias in aliases:
        for suffix in ('单位', '_unit', 'unit'):
            key = f'{alias}{suffix}'
            if key in keys:
                return str(row.get(key))
    joined = ' '.join(str(k) for k in keys)
    return '万' if '万元' in joined else None


def _df_to_rows(df: Any) -> list[dict]:
    if isinstance(df, pd.DataFrame) and not df.empty:
        return df.to_dict('records')
    return []


def _call_source(func_name: str, call_variants: list[dict[str, Any]], trade_date: str, source_name: str) -> tuple[list[dict], str | None]:
    func = getattr(ak, func_name, None)
    if func is None:
        return [], f'{func_name}: unavailable'
    errors = []
    for kwargs in call_variants:
        try:
            df = func(**kwargs)
            rows = [normalize_lhb_row(row, trade_date, source_name) for row in _df_to_rows(df)]
            return [r for r in rows if r.get('code')], None
        except TypeError as exc:
            errors.append(f'TypeError: {exc}')
        except Exception as exc:  # noqa: BLE001 - source failures must degrade.
            errors.append(f'{type(exc).__name__}: {exc}')
            break
    return [], '; '.join(errors) if errors else None


def collect_lhb_data(trade_date: str) -> dict:
    td = normalize_trade_date(trade_date)
    compact = ak_date(td)
    source_errors: dict[str, str] = {}
    all_rows: list[dict] = []
    sources = [
        ('stock_lhb_detail_em', [{'date': compact}, {'trade_date': compact}, {}], 'akshare_lhb_detail'),
        ('stock_lhb_stock_detail_em', [{'date': compact}, {'trade_date': compact}, {}], 'akshare_lhb_stock_detail'),
        ('stock_lhb_stock_statistic_em', [{'date': compact}, {'trade_date': compact}, {}], 'akshare_lhb_stock_statistic'),
    ]
    for func_name, variants, source_name in sources:
        rows, error = _call_source(func_name, variants, td, source_name)
        all_rows.extend(rows)
        if error:
            source_errors[source_name] = error
    return {'trade_date': td, 'lhb_rows': all_rows, 'source_errors': source_errors, 'fetched_at': now_iso()}


def upsert_lhb_daily(conn: sqlite3.Connection, rows: list[dict]) -> None:
    now = now_iso()
    for row in rows or []:
        if not row.get('trade_date') or not row.get('code'):
            continue
        existing = conn.execute('SELECT created_at FROM lhb_daily WHERE trade_date=? AND code=?', (row['trade_date'], row['code'])).fetchone()
        created_at = existing['created_at'] if existing and 'created_at' in existing.keys() else now
        conn.execute(
            '''INSERT INTO lhb_daily (
                trade_date, code, name, net_buy, institution_net_buy, buy_seats_json, sell_seats_json,
                known_hot_money_flag, quant_flag, interpretation, source, raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, code) DO UPDATE SET
                name=excluded.name,
                net_buy=excluded.net_buy,
                institution_net_buy=excluded.institution_net_buy,
                buy_seats_json=excluded.buy_seats_json,
                sell_seats_json=excluded.sell_seats_json,
                known_hot_money_flag=excluded.known_hot_money_flag,
                quant_flag=excluded.quant_flag,
                interpretation=excluded.interpretation,
                source=excluded.source,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at''',
            (
                row['trade_date'], row['code'], row.get('name'), row.get('net_buy'), row.get('institution_net_buy'),
                row.get('buy_seats_json') or '[]', row.get('sell_seats_json') or '[]', int(row.get('known_hot_money_flag') or 0),
                int(row.get('quant_flag') or 0), row.get('interpretation'), row.get('source'), row.get('raw_json'),
                created_at, now,
            ),
        )
    conn.commit()


def load_lhb_rows(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM lhb_daily WHERE trade_date=? ORDER BY COALESCE(net_buy, 0) DESC, code', (normalize_trade_date(trade_date),)).fetchall()
    return [dict(row) for row in rows]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def _load_limitup_map(conn: sqlite3.Connection, trade_date: str) -> dict[str, dict]:
    if not table_exists(conn, 'limitup_daily'):
        return {}
    rows = conn.execute('SELECT * FROM limitup_daily WHERE trade_date=?', (trade_date,)).fetchall()
    return {str(r['code']).zfill(6)[-6:]: dict(r) for r in rows}


def _load_new_high_map(conn: sqlite3.Connection, trade_date: str) -> dict[str, dict]:
    if not table_exists(conn, 'new_high_daily'):
        return {}
    rows = conn.execute("SELECT * FROM new_high_daily WHERE trade_date=? AND high_type IN ('60日新高','100日新高','250日新高')", (trade_date,)).fetchall()
    return {str(r['code']).zfill(6)[-6:]: dict(r) for r in rows}


def _theme_context(conn: sqlite3.Connection, trade_date: str) -> tuple[dict[str, list[dict]], set[str], dict[str, dict]]:
    stock_map: dict[str, list[dict]] = {}
    if table_exists(conn, 'theme_stock_map'):
        for row in conn.execute('SELECT * FROM theme_stock_map WHERE trade_date=?', (trade_date,)):
            stock_map.setdefault(str(row['code']).zfill(6)[-6:], []).append(dict(row))
    top_names: set[str] = set()
    theme_daily: dict[str, dict] = {}
    if table_exists(conn, 'theme_daily'):
        rows = [dict(r) for r in conn.execute('SELECT * FROM theme_daily WHERE trade_date=? ORDER BY COALESCE(score,0) DESC LIMIT 5', (trade_date,))]
        top_names = {r.get('theme_name') for r in rows if r.get('theme_name')}
        theme_daily = {r.get('theme_name'): r for r in rows if r.get('theme_name')}
    return stock_map, top_names, theme_daily


def _best_theme(themes: list[dict], top_names: set[str], theme_daily: dict[str, dict]) -> dict | None:
    if not themes:
        return None
    return sorted(themes, key=lambda r: (r.get('theme_name') in top_names, float((theme_daily.get(r.get('theme_name')) or {}).get('score') or 0), float(r.get('confidence') or 0)), reverse=True)[0]


def _stock_item(row: dict, theme: dict | None = None) -> dict:
    item = {k: row.get(k) for k in ('trade_date', 'code', 'name', 'net_buy', 'institution_net_buy', 'source', 'interpretation', 'quant_flag', 'known_hot_money_flag')}
    if theme:
        item['theme_name'] = theme.get('theme_name')
    return item


def _missing_from_raw(row: dict) -> list[str]:
    try:
        raw = json.loads(row.get('raw_json') or '{}')
        return list(raw.get('missing_fields') or [])
    except Exception:
        return []


def build_lhb_summary(conn: sqlite3.Connection, trade_date: str, source_errors: dict | None = None) -> dict:
    td = normalize_trade_date(trade_date)
    rows = load_lhb_rows(conn, td)
    limitup = _load_limitup_map(conn, td)
    new_high = _load_new_high_map(conn, td)
    stock_themes, top_theme_names, theme_daily = _theme_context(conn, td)
    enriched = []
    missing_fields: dict[str, list[str]] = {'lhb_daily': []}
    for row in rows:
        code = str(row.get('code') or '').zfill(6)[-6:]
        theme = _best_theme(stock_themes.get(code, []), top_theme_names, theme_daily)
        item = _stock_item(row, theme)
        lu = limitup.get(code)
        nh = new_high.get(code)
        item.update({
            'is_limitup': bool(lu and 'limitup' in str(lu.get('source') or '')),
            'is_broken': bool(lu and 'broken' in str(lu.get('source') or '')),
            'is_downlimit': bool(lu and 'downlimit' in str(lu.get('source') or '')),
            'is_new_high': bool(nh),
            'high_type': nh.get('high_type') if nh else None,
            'is_top_theme': bool(theme and theme.get('theme_name') in top_theme_names),
            'theme_negative': bool(theme and (theme_daily.get(theme.get('theme_name')) or {}).get('broken_count')),
        })
        enriched.append(item)
        for field in _missing_from_raw(row):
            if field not in missing_fields['lhb_daily']:
                missing_fields['lhb_daily'].append(field)
        if code not in stock_themes and 'theme_mapping' not in missing_fields['lhb_daily']:
            missing_fields['lhb_daily'].append('theme_mapping')
        if code not in new_high and 'new_high_mapping' not in missing_fields['lhb_daily']:
            missing_fields['lhb_daily'].append('new_high_mapping')
    positive = [r for r in enriched if r.get('net_buy') is not None and r['net_buy'] > 0]
    negative = [r for r in enriched if r.get('net_buy') is not None and r['net_buy'] < 0]
    institution = [r for r in enriched if r.get('institution_net_buy') is not None and r['institution_net_buy'] > 0]
    negative_items = []
    for r in enriched:
        reasons = []
        if r.get('net_buy') is not None and r['net_buy'] < 0:
            reasons.append('龙虎榜净卖出较大')
        if r.get('is_broken'):
            reasons.append('source contains broken')
        if r.get('is_downlimit'):
            reasons.append('source contains downlimit')
        if r.get('theme_negative'):
            reasons.append('题材存在负反馈')
        if reasons:
            negative_items.append({**r, 'reason': '；'.join(reasons)})
    return {
        'trade_date': td,
        'lhb_count': len(rows),
        'net_buy_top': sorted(positive, key=lambda r: r.get('net_buy') or 0, reverse=True)[:10],
        'net_sell_top': sorted(negative, key=lambda r: r.get('net_buy') or 0)[:10],
        'institution_net_buy_top': sorted(institution, key=lambda r: r.get('institution_net_buy') or 0, reverse=True)[:10],
        'quant_flag_items': [r for r in enriched if r.get('quant_flag')][:20],
        'known_hot_money_items': [r for r in enriched if r.get('known_hot_money_flag')][:20],
        'lhb_limitup_resonance': [r for r in enriched if r.get('is_limitup')],
        'lhb_new_high_resonance': [r for r in enriched if r.get('is_new_high')],
        'lhb_theme_resonance': [r for r in enriched if r.get('is_top_theme')],
        'negative_items': negative_items[:20],
        'missing_fields': missing_fields,
        'source_errors': source_errors or {},
        'sources': sorted({r.get('source') for r in rows if r.get('source')}),
        'generated_at': now_iso(),
    }


def fmt_money(value: Any) -> str:
    v = safe_float_money(value)
    if v is None:
        return '—'
    if abs(v) >= 100000000:
        return f'{v / 100000000:.2f}亿'
    if abs(v) >= 10000:
        return f'{v / 10000:.2f}万'
    return f'{v:.2f}'


def stock_label(item: dict) -> str:
    return f"{item.get('name') or '未命名'}({item.get('code') or '------'})"


def _table(lines: list[str], items: list[dict], cols: list[str], renderer) -> None:
    if not items:
        lines.append('- 暂无。')
        return
    lines.append('| ' + ' | '.join(cols) + ' |')
    lines.append('|' + '|'.join(['---'] * len(cols)) + '|')
    for item in items:
        lines.append('| ' + ' | '.join(renderer(item)) + ' |')


def render_lhb_markdown(summary: dict) -> str:
    lines = [
        '# A 股龙虎榜 sidecar 复盘',
        '',
        f"交易日：{summary.get('trade_date')}",
        '',
        '## 1. 总览',
        f"- 龙虎榜股票数：{summary.get('lhb_count', 0)}",
        f"- 净买入 Top 数量：{len(summary.get('net_buy_top') or [])}",
        f"- 净卖出 Top 数量：{len(summary.get('net_sell_top') or [])}",
        f"- 机构净买入数量：{len(summary.get('institution_net_buy_top') or [])}",
        f"- 规则识别量化席位数量：{len(summary.get('quant_flag_items') or [])}",
        f"- 规则识别游资席位数量：{len(summary.get('known_hot_money_items') or [])}",
        f"- 数据源：{', '.join(summary.get('sources') or []) or '—'}",
        f"- 数据时间：{summary.get('generated_at') or '—'}",
        '',
        '## 2. 净买入 Top',
    ]
    _table(lines, summary.get('net_buy_top') or [], ['股票', '题材', '净买入', '是否涨停', '是否新高', '是否属于 Top 题材'], lambda i: [stock_label(i), i.get('theme_name') or '—', fmt_money(i.get('net_buy')), '是' if i.get('is_limitup') else '否', i.get('high_type') or ('是' if i.get('is_new_high') else '否'), '是' if i.get('is_top_theme') else '否'])
    lines.extend(['', '## 3. 净卖出 / 负反馈 Top'])
    _table(lines, (summary.get('net_sell_top') or []) + (summary.get('negative_items') or []), ['股票', '题材', '净买入或净卖出', '是否炸板', '是否跌停', '所在题材是否有负反馈'], lambda i: [stock_label(i), i.get('theme_name') or '—', fmt_money(i.get('net_buy')), '是' if i.get('is_broken') else '否', '是' if i.get('is_downlimit') else '否', '是' if i.get('theme_negative') else '否'])
    lines.extend(['', '## 4. 机构 / 量化 / 游资席位弱识别', '- 席位标签为弱规则识别，可能不准确。'])
    lines.append(f"- 机构净买入：{', '.join(stock_label(i) for i in summary.get('institution_net_buy_top') or []) or '暂无'}")
    lines.append(f"- 规则识别量化席位：{', '.join(stock_label(i) for i in summary.get('quant_flag_items') or []) or '暂无'}")
    lines.append(f"- 规则识别游资席位：{', '.join(stock_label(i) for i in summary.get('known_hot_money_items') or []) or '暂无'}")
    lines.extend(['', '## 5. 龙虎榜 + 涨停共振'])
    _table(lines, summary.get('lhb_limitup_resonance') or [], ['股票', '题材', '净买入'], lambda i: [stock_label(i), i.get('theme_name') or '—', fmt_money(i.get('net_buy'))])
    lines.extend(['', '## 6. 龙虎榜 + 新高共振'])
    _table(lines, summary.get('lhb_new_high_resonance') or [], ['股票', '题材', '新高类型', '净买入'], lambda i: [stock_label(i), i.get('theme_name') or '—', i.get('high_type') or '—', fmt_money(i.get('net_buy'))])
    lines.extend(['', '## 7. 龙虎榜 + 主线题材共振'])
    _table(lines, summary.get('lhb_theme_resonance') or [], ['股票', 'Top 题材', '净买入', '是否涨停', '是否新高'], lambda i: [stock_label(i), i.get('theme_name') or '—', fmt_money(i.get('net_buy')), '是' if i.get('is_limitup') else '否', i.get('high_type') or '否'])
    lines.extend(['', '## 8. 数据缺失说明'])
    missing = summary.get('missing_fields') or {}
    if missing:
        for source, fields in missing.items():
            lines.append(f"- {source}：{', '.join(fields) if fields else '无'}")
    else:
        lines.append('- 暂无字段缺失记录。')
    errors = summary.get('source_errors') or {}
    if errors:
        lines.append('- source_errors：')
        for source, err in errors.items():
            lines.append(f"  - {source}: {err}")
    else:
        lines.append('- source_errors：无')
    lines.extend([
        '',
        '## 9. 风险提示',
        '- 龙虎榜数据具有滞后性。',
        '- 席位类型为规则识别，可能不准确。',
        '- 龙虎榜净买入不等于次日上涨。',
        '- 本报告只用于复盘辅助，不构成投资建议。',
    ])
    return '\n'.join(lines).replace('建议买入', '观察').replace('必须买入', '观察') + '\n'


def run_lhb_sidecar(trade_date: str | None = None, db_path: str | Path = DEFAULT_DB_PATH, output_root: str | Path = DEFAULT_OUTPUT_ROOT, skip_fetch: bool = False, source_errors: dict | None = None) -> dict:
    td = normalize_trade_date(trade_date or today_text())
    db_path = Path(db_path).expanduser()
    output_root = Path(output_root).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    import ashare_shortline_schema as schema
    schema.init_db(db_path)
    fetched = {'lhb_rows': [], 'source_errors': source_errors or {}}
    with schema.connect(db_path) as conn:
        if not skip_fetch:
            fetched = collect_lhb_data(td)
            upsert_lhb_daily(conn, fetched.get('lhb_rows') or [])
        merged_errors = {**(source_errors or {}), **(fetched.get('source_errors') or {})}
        summary = build_lhb_summary(conn, td, source_errors=merged_errors)
    out_dir = output_root / td
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / 'lhb-sidecar.json'
    md_path = out_dir / 'lhb-sidecar.md'
    markdown = render_lhb_markdown(summary)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    md_path.write_text(markdown, encoding='utf-8')
    return {'summary': summary, 'paths': {'json_path': str(json_path), 'markdown_path': str(md_path), 'db_path': str(db_path)}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='A-share LHB sidecar collector and analyzer')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--today', action='store_true', help='Use today as trade date')
    group.add_argument('--trade-date', help='Trade date YYYY-MM-DD or YYYYMMDD')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH), help=f'Shadow shortline DB path. Default: {DEFAULT_DB_PATH}')
    parser.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT), help=f'Shadow output root. Default: {DEFAULT_OUTPUT_ROOT}')
    parser.add_argument('--skip-fetch', action='store_true', help='Only build summary/report from existing lhb_daily')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trade_date = today_text() if args.today else (args.trade_date or today_text())
    result = run_lhb_sidecar(trade_date, db_path=args.db_path, output_root=args.output_root, skip_fetch=args.skip_fetch)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
