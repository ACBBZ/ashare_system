#!/usr/bin/env python3
"""Collect and normalize A-share limit-up ecology data into the shortline sidecar DB.

Scope guard for stage 2:
- only writes ``limitup_daily`` in the shadow ``shortline_signal.db``;
- does not write legacy production DBs;
- does not connect close-summary/opening-brief/opening-action-table/cron/Feishu.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - exercised by integration/runtime, tests monkeypatch it.
    import akshare as ak
except Exception:  # pragma: no cover
    class _MissingAkshare:
        def __getattr__(self, name: str):
            raise RuntimeError(f'akshare is unavailable; cannot call {name}')

    ak = _MissingAkshare()

import ashare_shortline_schema as schema

OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')

SOURCE_CONFIG = {
    'limitup': {
        'label': '涨停池',
        'func': 'stock_zt_pool_em',
        'broken': False,
    },
    'downlimit': {
        'label': '跌停池',
        'func': 'stock_zt_pool_dtgc_em',
        'broken': False,
    },
    'broken': {
        'label': '炸板池',
        'func': 'stock_zt_pool_zbgc_em',
        'broken': True,
    },
    'strong': {
        'label': '强势股池',
        'func': 'stock_zt_pool_strong_em',
        'broken': False,
    },
}

ALIASES: dict[str, tuple[str, ...]] = {
    'code': ('代码', '股票代码', 'symbol', 'code'),
    'name': ('名称', '股票简称', 'name'),
    'reason': ('涨停原因类别', '涨停原因', '所属行业', '题材', 'reason'),
    'first_limit_time': ('首次封板时间', '首次涨停时间', 'first_limit_time'),
    'last_limit_time': ('最后封板时间', '最后涨停时间', 'last_limit_time'),
    'open_count': ('开板次数', '炸板次数', 'open_count'),
    'seal_amount': ('封板资金', '封单资金', '最后封板资金', 'seal_amount'),
    'seal_ratio': ('封成比', '封单成交比', 'seal_ratio'),
    'turnover_rate': ('换手率', 'turnover_rate'),
    'amount': ('成交额', 'amount'),
    'consecutive_board_count': ('连板数', '连板高度', '几天几板', '涨停统计', 'consecutive_board_count'),
}

NORMALIZED_COLUMNS = (
    'trade_date',
    'code',
    'name',
    'theme',
    'first_limit_time',
    'last_limit_time',
    'open_count',
    'seal_amount',
    'seal_ratio',
    'turnover_rate',
    'amount',
    'consecutive_board_count',
    'is_broken_board',
    'is_reseal',
    'reason',
    'source',
    'raw_json',
    'created_at',
    'updated_at',
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def normalize_trade_date(value: str | None = None) -> str:
    if not value:
        return date.today().isoformat()
    text = str(value).strip()
    if re.fullmatch(r'\d{8}', text):
        return f'{text[:4]}-{text[4:6]}-{text[6:8]}'
    return text


def akshare_date(value: str) -> str:
    return normalize_trade_date(value).replace('-', '')


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if str(value).strip() in {'', '-', '--', '—', 'nan', 'NaN', 'None', 'null'}:
        return True
    return False


def _json_default(value: Any) -> Any:
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return str(value)


def _get_alias(row: dict, field: str) -> Any:
    for key in ALIASES[field]:
        if key in row and not _is_missing(row[key]):
            return row[key]
    return None


def _clean_text(value: Any) -> str | None:
    if _is_missing(value):
        return None
    return str(value).strip()


def _normalize_code(value: Any) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    text = re.sub(r'^(sh|sz|bj|SH|SZ|BJ)', '', text)
    match = re.search(r'(\d{6})', text)
    return match.group(1) if match else text


def _to_number(value: Any) -> float | None:
    if _is_missing(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(',', '').replace('%', '')
    multiplier = 1.0
    if '亿' in text:
        multiplier = 100000000.0
    elif '万' in text:
        multiplier = 10000.0
    text = re.sub(r'[亿万元人民币￥\s]', '', text)
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    return float(match.group(0)) * multiplier


def _to_int(value: Any) -> int | None:
    number = _to_number(value)
    return int(number) if number is not None else None


def parse_consecutive_board_count(value) -> int | None:
    """Parse AkShare board-ladder text into an integer board count."""
    if _is_missing(value):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and not math.isnan(value):
        return int(value)
    text = str(value).strip()
    if '首板' in text:
        return 1
    day_board = re.search(r'(\d+)\s*天\s*(\d+)\s*板', text)
    if day_board:
        return int(day_board.group(2))
    consecutive = re.search(r'(\d+)\s*(?:连板|板)', text)
    if consecutive:
        return int(consecutive.group(1))
    plain = re.fullmatch(r'\d+', text)
    if plain:
        return int(text)
    numbers = re.findall(r'\d+', text)
    return int(numbers[-1]) if numbers else None


def normalize_limitup_row(row: dict, trade_date: str, source: str, broken: bool = False) -> dict:
    """Normalize one source row into the ``limitup_daily`` column shape."""
    raw = dict(row)
    reason = _clean_text(_get_alias(raw, 'reason'))
    now = now_iso()
    open_count = _to_int(_get_alias(raw, 'open_count'))
    is_reseal = 1 if open_count and open_count > 0 and not broken else 0
    return {
        'trade_date': normalize_trade_date(trade_date),
        'code': _normalize_code(_get_alias(raw, 'code')),
        'name': _clean_text(_get_alias(raw, 'name')),
        'theme': reason,
        'first_limit_time': _clean_text(_get_alias(raw, 'first_limit_time')),
        'last_limit_time': _clean_text(_get_alias(raw, 'last_limit_time')),
        'open_count': open_count,
        'seal_amount': _to_number(_get_alias(raw, 'seal_amount')),
        'seal_ratio': _to_number(_get_alias(raw, 'seal_ratio')),
        'turnover_rate': _to_number(_get_alias(raw, 'turnover_rate')),
        'amount': _to_number(_get_alias(raw, 'amount')),
        'consecutive_board_count': parse_consecutive_board_count(_get_alias(raw, 'consecutive_board_count')),
        'is_broken_board': 1 if broken else 0,
        'is_reseal': is_reseal,
        'reason': reason,
        'source': source,
        'raw_json': json.dumps(raw, ensure_ascii=False, default=_json_default),
        'created_at': now,
        'updated_at': now,
    }


def _records_from_dataframe(df: Any) -> list[dict]:
    if df is None:
        return []
    if hasattr(df, 'empty') and df.empty:
        return []
    if hasattr(df, 'to_dict'):
        return list(df.to_dict(orient='records'))
    if isinstance(df, list):
        return [dict(item) for item in df]
    return []


def _missing_fields_for_records(records: list[dict]) -> list[str]:
    if not records:
        return []
    columns = set().union(*(record.keys() for record in records))
    missing: list[str] = []
    for field, aliases in ALIASES.items():
        if field in {'code', 'name'}:
            continue
        if not any(alias in columns for alias in aliases):
            missing.append(field)
    return missing


def _fetch_source(source: str, trade_date: str) -> tuple[list[dict], str | None]:
    config = SOURCE_CONFIG[source]
    func = getattr(ak, config['func'])
    try:
        df = func(date=akshare_date(trade_date))
        return _records_from_dataframe(df), None
    except Exception as exc:  # noqa: BLE001 - source failures must not abort the collector.
        return [], f'{type(exc).__name__}: {exc}'


def collect_limitup_data(trade_date: str) -> dict:
    normalized_date = normalize_trade_date(trade_date)
    payload = {
        'trade_date': normalized_date,
        'limitup_rows': [],
        'downlimit_rows': [],
        'broken_rows': [],
        'strong_rows': [],
        'source_errors': {},
        'missing_fields': {},
        'generated_at': now_iso(),
    }
    target_key = {
        'limitup': 'limitup_rows',
        'downlimit': 'downlimit_rows',
        'broken': 'broken_rows',
        'strong': 'strong_rows',
    }
    for source in ('limitup', 'downlimit', 'broken', 'strong'):
        records, error = _fetch_source(source, normalized_date)
        if error:
            payload['source_errors'][source] = error
        missing_fields = _missing_fields_for_records(records)
        if missing_fields:
            payload['missing_fields'][source] = missing_fields
        broken = bool(SOURCE_CONFIG[source]['broken'])
        rows = [normalize_limitup_row(record, normalized_date, source, broken=broken) for record in records]
        rows = [row for row in rows if row.get('code')]
        payload[target_key[source]] = rows
    return payload


def _merge_source(existing: str | None, new: str | None) -> str | None:
    parts: list[str] = []
    for value in (existing, new):
        if not value:
            continue
        for part in str(value).split(','):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return ','.join(parts) if parts else None


def _choose(existing: Any, new: Any) -> Any:
    return new if not _is_missing(new) else existing


def upsert_limitup_daily(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Upsert rows by trade_date+code without duplicating reruns."""
    changed = 0
    for input_row in rows:
        row = {column: input_row.get(column) for column in NORMALIZED_COLUMNS}
        if not row.get('trade_date') or not row.get('code'):
            continue
        existing = conn.execute(
            'SELECT * FROM limitup_daily WHERE trade_date = ? AND code = ?',
            (row['trade_date'], row['code']),
        ).fetchone()
        if existing:
            merged = dict(existing)
            for column in NORMALIZED_COLUMNS:
                if column in {'trade_date', 'code', 'created_at'}:
                    continue
                if column == 'source':
                    merged[column] = _merge_source(merged.get(column), row.get(column))
                elif column == 'is_broken_board':
                    merged[column] = 1 if merged.get(column) or row.get(column) else 0
                elif column == 'is_reseal':
                    merged[column] = 1 if merged.get(column) or row.get(column) else 0
                elif column == 'raw_json':
                    merged[column] = row.get(column) or merged.get(column)
                elif column == 'updated_at':
                    merged[column] = now_iso()
                else:
                    merged[column] = _choose(merged.get(column), row.get(column))
            assignments = ', '.join(f'{column} = ?' for column in NORMALIZED_COLUMNS if column not in {'trade_date', 'code', 'created_at'})
            values = [merged[column] for column in NORMALIZED_COLUMNS if column not in {'trade_date', 'code', 'created_at'}]
            values.extend([row['trade_date'], row['code']])
            conn.execute(f'UPDATE limitup_daily SET {assignments} WHERE trade_date = ? AND code = ?', values)
        else:
            row['created_at'] = row.get('created_at') or now_iso()
            row['updated_at'] = row.get('updated_at') or row['created_at']
            placeholders = ', '.join('?' for _ in NORMALIZED_COLUMNS)
            conn.execute(
                f"INSERT INTO limitup_daily ({', '.join(NORMALIZED_COLUMNS)}) VALUES ({placeholders})",
                [row.get(column) for column in NORMALIZED_COLUMNS],
            )
        changed += 1
    conn.commit()
    return changed


def _stock_label(row: sqlite3.Row | dict) -> str:
    name = row['name'] if row['name'] else '未命名'
    return f"{name}({row['code']})"


def build_limitup_summary(
    conn: sqlite3.Connection,
    trade_date: str,
    missing_fields: dict[str, list[str]] | None = None,
    source_errors: dict[str, str] | None = None,
    generated_at: str | None = None,
) -> dict:
    normalized_date = normalize_trade_date(trade_date)
    rows = conn.execute(
        'SELECT * FROM limitup_daily WHERE trade_date = ? ORDER BY consecutive_board_count DESC, seal_amount DESC',
        (normalized_date,),
    ).fetchall()
    limitup_rows = [row for row in rows if row['source'] and 'limitup' in row['source'].split(',')]
    downlimit_rows = [row for row in rows if row['source'] and 'downlimit' in row['source'].split(',')]
    broken_rows = [row for row in rows if row['is_broken_board']]

    ladder: dict[int, list[str]] = defaultdict(list)
    for row in limitup_rows:
        count = row['consecutive_board_count'] or 1
        ladder[int(count)].append(_stock_label(row))

    top_seal = [
        {
            'code': row['code'],
            'name': row['name'],
            'seal_amount': row['seal_amount'],
        }
        for row in sorted(
            [row for row in limitup_rows if row['seal_amount'] is not None],
            key=lambda item: item['seal_amount'],
            reverse=True,
        )[:10]
    ]

    reason_groups: dict[str, list[str]] = defaultdict(list)
    for row in limitup_rows:
        reason = row['reason'] or row['theme']
        if reason:
            reason_groups[reason].append(_stock_label(row))
    top_reasons = [
        {'reason': reason, 'count': len(stocks), 'stocks': stocks}
        for reason, stocks in sorted(reason_groups.items(), key=lambda item: len(item[1]), reverse=True)
    ]

    sources = sorted({part for row in rows if row['source'] for part in row['source'].split(',') if part})
    return {
        'trade_date': normalized_date,
        'zt_count': len(limitup_rows),
        'dt_count': len(downlimit_rows),
        'broken_count': len(broken_rows),
        'max_consecutive_board': max([row['consecutive_board_count'] or 1 for row in limitup_rows], default=0),
        'ladder': dict(sorted(ladder.items(), key=lambda item: item[0], reverse=True)),
        'top_seal_amount': top_seal,
        'top_reasons': top_reasons,
        'broken_stocks': [_stock_label(row) for row in broken_rows],
        'missing_fields': missing_fields or {},
        'source_errors': source_errors or {},
        'sources': sources,
        'generated_at': generated_at or now_iso(),
    }


def _format_money(value: Any) -> str:
    number = _to_number(value)
    if number is None:
        return '—'
    if abs(number) >= 100000000:
        return f'{number / 100000000:.2f}亿'
    if abs(number) >= 10000:
        return f'{number / 10000:.2f}万'
    return f'{number:.0f}'


def render_limitup_markdown(summary: dict) -> str:
    lines: list[str] = [
        '# A 股涨停生态快照',
        '',
        f"交易日：{summary.get('trade_date', '—')}",
        '',
        '## 1. 总览',
        f"- 涨停家数：{summary.get('zt_count', 0)}",
        f"- 跌停家数：{summary.get('dt_count', 0)}",
        f"- 炸板数量：{summary.get('broken_count', 0)}",
        f"- 最高连板：{summary.get('max_consecutive_board', 0)}",
        f"- 数据源：{', '.join(summary.get('sources') or SOURCE_CONFIG.keys())}",
        f"- 数据时间：{summary.get('generated_at', '—')}",
        '',
        '## 2. 连板梯队',
    ]
    ladder = summary.get('ladder') or {}
    if ladder:
        for board in sorted(ladder, key=lambda value: int(value), reverse=True):
            lines.append(f"- {board}板：{', '.join(ladder[board])}")
    else:
        lines.append('- 暂无连板梯队数据')

    lines.extend(['', '## 3. 最大封板资金 Top 10'])
    top_seal = summary.get('top_seal_amount') or []
    if top_seal:
        for idx, item in enumerate(top_seal, start=1):
            lines.append(f"{idx}. {item.get('name') or '未命名'}({item.get('code')})：{_format_money(item.get('seal_amount'))}")
    else:
        lines.append('封板资金字段缺失')

    lines.extend(['', '## 4. 涨停原因分类'])
    top_reasons = summary.get('top_reasons') or []
    if top_reasons:
        for item in top_reasons:
            lines.append(f"- {item.get('reason')}：{item.get('count', 0)} 只；{', '.join(item.get('stocks') or [])}")
    else:
        lines.append('涨停原因字段缺失')

    lines.extend(['', '## 5. 炸板/负反馈'])
    source_errors = summary.get('source_errors') or {}
    if 'broken' in source_errors:
        lines.append(f"- 炸板池接口失败：{source_errors['broken']}")
    lines.append(f"- 炸板股数量：{summary.get('broken_count', 0)}")
    broken_stocks = summary.get('broken_stocks') or []
    lines.append(f"- 炸板股列表：{', '.join(broken_stocks) if broken_stocks else '无/未获取'}")

    lines.extend(['', '## 6. 数据缺失说明'])
    missing_fields = summary.get('missing_fields') or {}
    if missing_fields:
        for source, fields in missing_fields.items():
            lines.append(f"- {source} 缺失字段：{', '.join(fields)}")
    else:
        lines.append('- missing_fields：无')
    if source_errors:
        for source, error in source_errors.items():
            lines.append(f"- {source} source_error：{error}")
    else:
        lines.append('- source_errors：无')

    lines.extend([
        '',
        '## 7. 风险提示',
        '- 数据来自公开接口，可能延迟或字段变化。',
        '- 本报告只用于复盘辅助，不构成投资建议。',
        '',
    ])
    return '\n'.join(lines)


def write_reports(summary: dict, output_root: str | Path | None = None) -> dict[str, str]:
    root = Path(output_root).expanduser() if output_root is not None else OUTPUT_ROOT
    out_dir = root / summary['trade_date']
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / 'limitup-ecology.json'
    md_path = out_dir / 'limitup-ecology.md'
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding='utf-8')
    md_path.write_text(render_limitup_markdown(summary), encoding='utf-8')
    return {'json_path': str(json_path), 'markdown_path': str(md_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Collect A-share limit-up ecology into shortline sidecar DB')
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument('--today', action='store_true', help='Use today as trade date')
    date_group.add_argument('--trade-date', help='Trade date, e.g. 2026-05-06')
    parser.add_argument('--db-path', default=str(schema.DB_PATH), help=f'Shortline sidecar DB path. Default: {schema.DB_PATH}')
    parser.add_argument('--output-root', default=str(OUTPUT_ROOT), help=f'Report output root. Default: {OUTPUT_ROOT}')
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    trade_date = normalize_trade_date(None if args.today else args.trade_date)
    schema.init_db(args.db_path)
    payload = collect_limitup_data(trade_date)
    all_rows = payload['limitup_rows'] + payload['downlimit_rows'] + payload['broken_rows'] + payload['strong_rows']
    with schema.connect(args.db_path) as conn:
        upserted = upsert_limitup_daily(conn, all_rows)
        summary = build_limitup_summary(
            conn,
            trade_date,
            missing_fields=payload['missing_fields'],
            source_errors=payload['source_errors'],
            generated_at=payload['generated_at'],
        )
    paths = write_reports(summary, args.output_root)
    print(json.dumps({'ok': True, 'trade_date': trade_date, 'upserted_rows': upserted, **paths, 'source_errors': payload['source_errors'], 'missing_fields': payload['missing_fields']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
