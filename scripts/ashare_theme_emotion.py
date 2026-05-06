#!/usr/bin/env python3
"""Build shadow theme map and emotion anchors from ``limitup_daily``.

Stage 3 scope guard:
- reads the shortline sidecar DB and optionally reads sector data from market DB;
- writes only theme_daily, theme_stock_map, emotion_anchors in shortline_signal.db;
- does not modify production DBs, production report generators, cron, or Feishu.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import ashare_shortline_schema as schema

OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
MARKET_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
UNCATEGORIZED_THEME = '未归类题材'

THEME_SPLIT_RE = re.compile(r'[+/、，,;；|\s]+')
FORBIDDEN_ADVICE_WORDS = ('建议买入', '必须买入', '确定性机会', '无脑买入', '满仓')


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def normalize_trade_date(value: str | None = None) -> str:
    if not value:
        return date.today().isoformat()
    text = str(value).strip()
    if re.fullmatch(r'\d{8}', text):
        return f'{text[:4]}-{text[4:6]}-{text[6:8]}'
    return text


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() in {'', '-', '--', '—', 'nan', 'NaN', 'None', 'null'}


def to_float(value: Any) -> float | None:
    if is_missing(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).replace(',', '').replace('%', '').strip()
    multiplier = 1.0
    if '亿' in text:
        multiplier = 100000000.0
    elif '万' in text:
        multiplier = 10000.0
    text = re.sub(r'[亿万元人民币￥\s]', '', text)
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    return float(match.group(0)) * multiplier if match else None


def source_parts(source: str | None) -> set[str]:
    return {part.strip() for part in str(source or '').split(',') if part.strip()}


def stock_label(row: dict) -> str:
    return f"{row.get('name') or '未命名'}({row.get('code')})"


def theme_id_for(theme_name: str) -> str:
    digest = hashlib.sha1(theme_name.encode('utf-8')).hexdigest()[:10]
    return f'theme_{digest}'


def load_limitup_rows(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT * FROM limitup_daily WHERE trade_date = ? ORDER BY code',
        (normalize_trade_date(trade_date),),
    ).fetchall()
    return [dict(row) for row in rows]


def load_sector_mapping(market_db_path: str | Path | None, trade_date: str) -> tuple[dict[str, str], str]:
    if not market_db_path:
        return {}, '板块成分数据不可用'
    path = Path(market_db_path).expanduser()
    if not path.exists():
        return {}, '板块成分数据不可用'
    mapping: dict[str, str] = {}
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                row['name']
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            }
            if 'sector_constituent_snapshots' in tables:
                cols = {row['name'] for row in conn.execute('PRAGMA table_info(sector_constituent_snapshots)')}
                code_col = next((c for c in ('code', 'stock_code', '股票代码') if c in cols), None)
                sector_col = next((c for c in ('sector_name', 'board_name', 'theme_name', '板块名称') if c in cols), None)
                if code_col and sector_col:
                    date_filter = 'WHERE trade_date = ?' if 'trade_date' in cols else ''
                    params = (normalize_trade_date(trade_date),) if date_filter else ()
                    for row in conn.execute(f'SELECT {code_col} AS code, {sector_col} AS sector FROM sector_constituent_snapshots {date_filter}', params):
                        if row['code'] and row['sector']:
                            mapping[str(row['code']).zfill(6)[-6:]] = str(row['sector'])
            if mapping:
                return mapping, '板块成分数据可用'
    except Exception as exc:  # noqa: BLE001 - optional read-only market DB must degrade.
        return {}, f'板块成分数据不可用：{type(exc).__name__}: {exc}'
    return {}, '板块成分数据不可用'


def split_reason_keywords(reason: str | None) -> list[str]:
    if is_missing(reason):
        return []
    out: list[str] = []
    for part in THEME_SPLIT_RE.split(str(reason).strip()):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


def identify_themes(row: dict, sector_mapping: dict[str, str] | None = None) -> list[tuple[str, float, str]]:
    if not is_missing(row.get('theme')):
        return [(str(row['theme']).strip(), 0.90, 'theme字段直接命中')]
    reason_themes = split_reason_keywords(row.get('reason'))
    if reason_themes:
        return [(name, 0.75, 'reason关键词解析') for name in reason_themes]
    sector_mapping = sector_mapping or {}
    sector = sector_mapping.get(str(row.get('code') or '').zfill(6)[-6:])
    if sector:
        return [(sector, 0.60, 'sector弱映射')]
    return [(UNCATEGORIZED_THEME, 0.30, '未归类题材')]


def build_theme_stock_records(rows: list[dict], sector_mapping: dict[str, str] | None = None) -> list[dict]:
    records: list[dict] = []
    for row in rows:
        for theme_name, confidence, method in identify_themes(row, sector_mapping):
            evidence = {
                'theme': row.get('theme'),
                'reason': row.get('reason'),
                'source': row.get('source'),
                'consecutive_board_count': row.get('consecutive_board_count'),
                'is_broken_board': row.get('is_broken_board'),
                'amount': row.get('amount'),
                'seal_amount': row.get('seal_amount'),
                'method': method,
            }
            records.append({
                'trade_date': row.get('trade_date'),
                'theme_id': theme_id_for(theme_name),
                'theme_name': theme_name,
                'code': row.get('code'),
                'name': row.get('name'),
                'role': '未确认',
                'evidence': json.dumps(evidence, ensure_ascii=False),
                'confidence': confidence,
                'source': row.get('source'),
                '_row': row,
            })
    return records


def assign_theme_roles(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record['theme_id']].append(record)

    for group in grouped.values():
        for record in group:
            parts = source_parts(record.get('source'))
            row = record['_row']
            if row.get('is_broken_board') or 'broken' in parts or 'downlimit' in parts:
                record['role'] = '负反馈'

        positive = [r for r in group if r['role'] != '负反馈' and 'limitup' in source_parts(r.get('source'))]
        if positive:
            leader = max(
                positive,
                key=lambda r: (
                    r['_row'].get('consecutive_board_count') or 0,
                    to_float(r['_row'].get('seal_amount')) or 0,
                    to_float(r['_row'].get('amount')) or 0,
                ),
            )
            leader['role'] = '龙头'

        non_negative = [r for r in group if r['role'] not in {'负反馈', '龙头'}]
        if non_negative:
            middle = max(non_negative, key=lambda r: to_float(r['_row'].get('amount')) or 0)
            if to_float(middle['_row'].get('amount')) is not None and source_parts(middle.get('source')) & {'strong', 'limitup'}:
                middle['role'] = '中军'

        for record in group:
            if record['role'] != '未确认':
                continue
            parts = source_parts(record.get('source'))
            boards = record['_row'].get('consecutive_board_count') or 0
            if 'limitup' in parts and boards <= 1:
                record['role'] = '补涨'
            elif 'limitup' in parts:
                record['role'] = '后排'
            else:
                record['role'] = '后排'
    return records


def upsert_theme_stock_map(conn: sqlite3.Connection, records: list[dict]) -> int:
    now = now_iso()
    for record in records:
        conn.execute(
            '''INSERT INTO theme_stock_map (
                trade_date, theme_id, theme_name, code, name, role, evidence, confidence, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, theme_id, code) DO UPDATE SET
                theme_name = excluded.theme_name,
                name = excluded.name,
                role = excluded.role,
                evidence = excluded.evidence,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at''',
            (
                record['trade_date'], record['theme_id'], record['theme_name'], record['code'], record.get('name'),
                record['role'], record['evidence'], record['confidence'], record.get('source'), now, now,
            ),
        )
    conn.commit()
    return len(records)


def calculate_theme_status(limitup_count: int, broken_count: int, downlimit_count: int, max_board: int, strong_count: int) -> str:
    if limitup_count == 0 and (broken_count + downlimit_count) > 0:
        return '退潮'
    if limitup_count >= 3 and max_board >= 3 and broken_count <= max(1, limitup_count // 3):
        return '主升'
    if (broken_count + downlimit_count) >= max(2, limitup_count):
        return '分歧'
    if limitup_count >= 1 and strong_count >= 1:
        return '修复'
    if 1 <= limitup_count <= 2 and max_board <= 2:
        return '轮动'
    if not limitup_count and not strong_count:
        return '未确认'
    return '未确认'


def build_theme_daily_records(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record['theme_id']].append(record)
    daily: list[dict] = []
    for theme_id, group in grouped.items():
        limitup = [r for r in group if 'limitup' in source_parts(r.get('source'))]
        broken = [r for r in group if r['_row'].get('is_broken_board') or 'broken' in source_parts(r.get('source'))]
        downlimit = [r for r in group if 'downlimit' in source_parts(r.get('source'))]
        strong = [r for r in group if 'strong' in source_parts(r.get('source'))]
        max_board = max([r['_row'].get('consecutive_board_count') or 0 for r in limitup], default=0)
        top_seal = max([to_float(r['_row'].get('seal_amount')) or 0 for r in limitup], default=0)
        top_amount = max([to_float(r['_row'].get('amount')) or 0 for r in group], default=0)
        leading_seal_score = min(top_seal / 100000000 * 8, 16) if top_seal else 0
        middle_amount_score = min(top_amount / 100000000 * 4, 12) if top_amount else 0
        raw_score = len(limitup) * 10 + max_board * 8 + leading_seal_score + middle_amount_score - len(broken) * 8 - len(downlimit) * 12
        score = max(0, min(100, round(raw_score, 2)))
        status = calculate_theme_status(len(limitup), len(broken), len(downlimit), max_board, len(strong))
        leader = next((r for r in group if r['role'] == '龙头'), None)
        middle = next((r for r in group if r['role'] == '中军'), None)
        negative = next((r for r in group if r['role'] == '负反馈'), None)
        evidence = {
            'limitup_count': len(limitup),
            'broken_count': len(broken),
            'downlimit_count': len(downlimit),
            'strong_count': len(strong),
            'max_consecutive_board': max_board,
            'leading_seal_amount_score': round(leading_seal_score, 2),
            'middle_amount_score': round(middle_amount_score, 2),
            'raw_score': round(raw_score, 2),
            'status_rule': status,
        }
        daily.append({
            'trade_date': group[0]['trade_date'],
            'theme_id': theme_id,
            'theme_name': group[0]['theme_name'],
            'parent_theme': None,
            'status': status,
            'score': score,
            'limitup_count': len(limitup),
            'broken_count': len(broken),
            'leading_stock_code': leader.get('code') if leader else None,
            'leading_stock_name': leader.get('name') if leader else None,
            'middle_stock_code': middle.get('code') if middle else None,
            'middle_stock_name': middle.get('name') if middle else None,
            'negative_stock_code': negative.get('code') if negative else None,
            'negative_stock_name': negative.get('name') if negative else None,
            'evidence_json': json.dumps(evidence, ensure_ascii=False),
            '_records': group,
        })
    return sorted(daily, key=lambda r: r['score'], reverse=True)


def upsert_theme_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = now_iso()
    for row in rows:
        conn.execute(
            '''INSERT INTO theme_daily (
                trade_date, theme_id, theme_name, parent_theme, status, score, limitup_count, broken_count,
                leading_stock_code, leading_stock_name, middle_stock_code, middle_stock_name,
                negative_stock_code, negative_stock_name, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, theme_id) DO UPDATE SET
                theme_name = excluded.theme_name,
                parent_theme = excluded.parent_theme,
                status = excluded.status,
                score = excluded.score,
                limitup_count = excluded.limitup_count,
                broken_count = excluded.broken_count,
                leading_stock_code = excluded.leading_stock_code,
                leading_stock_name = excluded.leading_stock_name,
                middle_stock_code = excluded.middle_stock_code,
                middle_stock_name = excluded.middle_stock_name,
                negative_stock_code = excluded.negative_stock_code,
                negative_stock_name = excluded.negative_stock_name,
                evidence_json = excluded.evidence_json,
                updated_at = excluded.updated_at''',
            (
                row['trade_date'], row['theme_id'], row['theme_name'], row.get('parent_theme'), row['status'], row['score'],
                row['limitup_count'], row['broken_count'], row.get('leading_stock_code'), row.get('leading_stock_name'),
                row.get('middle_stock_code'), row.get('middle_stock_name'), row.get('negative_stock_code'),
                row.get('negative_stock_name'), row['evidence_json'], now, now,
            ),
        )
    conn.commit()
    return len(rows)


def make_anchor(trade_date: str, anchor_type: str, record: dict, theme_name: str, status: str, impact_score: float, note: str) -> dict:
    return {
        'trade_date': trade_date,
        'anchor_type': anchor_type,
        'code': record.get('code'),
        'name': record.get('name'),
        'theme_name': theme_name,
        'status': status,
        'impact_score': max(0, min(100, round(impact_score, 2))),
        'note': note,
        'source': record.get('source'),
    }


def build_emotion_anchors(theme_daily: list[dict], records: list[dict], trade_date: str) -> list[dict]:
    anchors: list[dict] = []
    limitup_records = [r for r in records if 'limitup' in source_parts(r.get('source')) and r['role'] != '负反馈']
    if limitup_records:
        space = max(limitup_records, key=lambda r: (r['_row'].get('consecutive_board_count') or 0, to_float(r['_row'].get('seal_amount')) or 0, to_float(r['_row'].get('amount')) or 0))
        anchors.append(make_anchor(trade_date, '空间板', space, space['theme_name'], '高度锚点', 70 + (space['_row'].get('consecutive_board_count') or 0) * 5, f"全市场连板高度最高：{space['_row'].get('consecutive_board_count') or 1}板。"))

    if theme_daily:
        top_theme = theme_daily[0]
        leader = next((r for r in records if r['theme_id'] == top_theme['theme_id'] and r['role'] == '龙头'), None)
        if leader:
            anchors.append(make_anchor(trade_date, '核心龙头', leader, top_theme['theme_name'], top_theme['status'], top_theme['score'], f"来自得分最高题材 {top_theme['theme_name']} 的龙头。"))
        top_theme_ids = {row['theme_id'] for row in theme_daily[:3]}
        middles = [r for r in records if r['theme_id'] in top_theme_ids and r['role'] == '中军']
        if middles:
            middle = max(middles, key=lambda r: to_float(r['_row'].get('amount')) or 0)
            anchors.append(make_anchor(trade_date, '趋势中军', middle, middle['theme_name'], '趋势中军', min((to_float(middle['_row'].get('amount')) or 0) / 100000000 * 10 + 50, 100), '得分靠前题材中成交额较高的中军。'))

    downlimit = [r for r in records if 'downlimit' in source_parts(r.get('source'))]
    for record in sorted(downlimit, key=lambda r: to_float(r['_row'].get('amount')) or 0, reverse=True)[:3]:
        anchors.append(make_anchor(trade_date, '亏钱效应', record, record['theme_name'], '负反馈', 80, 'source 包含 downlimit，代表亏钱效应。'))

    broken = [r for r in records if r['_row'].get('is_broken_board') or 'broken' in source_parts(r.get('source'))]
    for record in sorted(broken, key=lambda r: (to_float(r['_row'].get('amount')) or 0, to_float(r['_row'].get('seal_amount')) or 0), reverse=True)[:5]:
        anchors.append(make_anchor(trade_date, '炸板负反馈', record, record['theme_name'], '负反馈', 70, 'source 包含 broken 或 is_broken_board=1。'))
    top_theme_ids = {row['theme_id'] for row in theme_daily[:5]}
    for record in [r for r in broken + downlimit if r['theme_id'] in top_theme_ids][:5]:
        anchors.append(make_anchor(trade_date, '同题材大面', record, record['theme_name'], '负反馈', 75, f"主线/前排题材 {record['theme_name']} 内出现 broken/downlimit 负反馈。"))

    unique: dict[tuple[str, str], dict] = {}
    for anchor in anchors:
        if anchor.get('code'):
            unique[(anchor['anchor_type'], anchor['code'])] = anchor
    return list(unique.values())


def upsert_emotion_anchors(conn: sqlite3.Connection, anchors: list[dict]) -> int:
    now = now_iso()
    for anchor in anchors:
        conn.execute(
            '''INSERT INTO emotion_anchors (
                trade_date, anchor_type, code, name, theme_name, status, impact_score, note, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, anchor_type, code) DO UPDATE SET
                name = excluded.name,
                theme_name = excluded.theme_name,
                status = excluded.status,
                impact_score = excluded.impact_score,
                note = excluded.note,
                source = excluded.source,
                updated_at = excluded.updated_at''',
            (
                anchor['trade_date'], anchor['anchor_type'], anchor['code'], anchor.get('name'), anchor.get('theme_name'),
                anchor.get('status'), anchor.get('impact_score'), anchor.get('note'), anchor.get('source'), now, now,
            ),
        )
    conn.commit()
    return len(anchors)


def build_data_notes(rows: list[dict], sector_status: str, source_errors: dict | None = None) -> dict:
    return {
        'seal_amount': '缺 seal_amount' if rows and all(is_missing(row.get('seal_amount')) for row in rows) else 'seal_amount 可用或部分可用',
        'reason_theme': '缺 reason/theme' if rows and all(is_missing(row.get('reason')) and is_missing(row.get('theme')) for row in rows) else 'reason/theme 可用或部分可用',
        'sector_mapping': sector_status,
        'source_errors': source_errors or {},
    }


def build_summary(trade_date: str, theme_daily: list[dict], records: list[dict], anchors: list[dict], data_notes: dict) -> dict:
    status = theme_daily[0]['status'] if theme_daily else '未确认'
    rule_note = '数据不足，未形成明确题材强度。'
    if theme_daily:
        evidence = json.loads(theme_daily[0]['evidence_json'])
        rule_note = f"Top题材 {theme_daily[0]['theme_name']}：涨停 {evidence.get('limitup_count')}，炸板 {evidence.get('broken_count')}，跌停 {evidence.get('downlimit_count')}，最高连板 {evidence.get('max_consecutive_board')}，触发 {theme_daily[0]['status']} 规则。"
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        grouped[record['theme_name']][record['role']].append(stock_label(record))
    return {
        'trade_date': trade_date,
        'generated_at': now_iso(),
        'top_themes': [{k: v for k, v in row.items() if not k.startswith('_')} for row in theme_daily[:10]],
        'theme_stock_map': [{k: v for k, v in row.items() if not k.startswith('_')} for row in records],
        'emotion_anchors': anchors,
        'theme_groups': {theme: dict(roles) for theme, roles in grouped.items()},
        'market_status': status,
        'market_status_rule': rule_note,
        'data_notes': data_notes,
    }


def render_markdown(summary: dict) -> str:
    lines = ['# A 股题材与情绪锚点快照', '', f"交易日：{summary['trade_date']}", f"生成时间：{summary['generated_at']}", '']
    lines.append('## 1. 今日主线题材 Top 5')
    top5 = summary.get('top_themes', [])[:5]
    if not top5:
        lines.append('- 暂无可确认题材。')
    for theme in top5:
        lines.extend([
            f"- 题材名称：{theme['theme_name']}",
            f"  - 状态：{theme['status']}",
            f"  - 分数：{theme['score']}",
            f"  - 涨停数量：{theme['limitup_count']}",
            f"  - 炸板/跌停数量：{theme['broken_count']}",
            f"  - 龙头：{theme.get('leading_stock_name') or '无'}",
            f"  - 中军：{theme.get('middle_stock_name') or '无'}",
            f"  - 负反馈：{theme.get('negative_stock_name') or '无'}",
        ])
    lines.extend(['', '## 2. 题材股票映射'])
    groups = summary.get('theme_groups', {})
    if not groups:
        lines.append('- 暂无题材股票映射。')
    for theme, roles in groups.items():
        lines.append(f"### {theme}")
        for role in ['龙头', '中军', '补涨', '后排', '负反馈', '未确认']:
            lines.append(f"- {role}：{', '.join(roles.get(role, [])) if roles.get(role) else '无'}")
    lines.extend(['', '## 3. 情绪锚点'])
    anchors_by_type: dict[str, list[dict]] = defaultdict(list)
    for anchor in summary.get('emotion_anchors', []):
        anchors_by_type[anchor['anchor_type']].append(anchor)
    for anchor_type in ['空间板', '核心龙头', '趋势中军', '亏钱效应', '同题材大面', '炸板负反馈']:
        anchors = anchors_by_type.get(anchor_type, [])
        if not anchors:
            lines.append(f"- {anchor_type}：无")
        else:
            text = '；'.join(f"{a.get('name') or '未命名'}({a.get('code')})，{a.get('theme_name')}，{a.get('note')}" for a in anchors)
            lines.append(f"- {anchor_type}：{text}")
    lines.extend(['', '## 4. 主线状态判断', f"- 今日状态：{summary.get('market_status', '未确认')}", f"- 规则说明：{summary.get('market_status_rule', '数据不足。')}", ''])
    lines.extend(['## 5. 明日观察点', '- 观察高分题材是否继续扩散，重点看涨停数量、连板高度、炸板反馈是否同步改善。', '- 观察核心龙头、趋势中军和负反馈锚点是否出现背离。', '- 只记录复盘观察点，不输出确定性买入建议。', ''])
    lines.append('## 6. 数据缺失说明')
    notes = summary.get('data_notes', {})
    lines.append(f"- 是否缺 seal_amount：{notes.get('seal_amount')}")
    lines.append(f"- 是否缺 reason/theme：{notes.get('reason_theme')}")
    lines.append(f"- 是否缺 sector 映射：{notes.get('sector_mapping')}")
    lines.append(f"- 是否存在 source_errors：{json.dumps(notes.get('source_errors') or {}, ensure_ascii=False)}")
    lines.extend(['', '## 7. 风险提示', '- 公开数据可能延迟或字段变化。', '- 题材识别为规则归类，可能有误。', '- 本报告只用于复盘辅助，不构成投资建议。', ''])
    markdown = '\n'.join(lines)
    for word in FORBIDDEN_ADVICE_WORDS:
        markdown = markdown.replace(word, '确定性表述已移除')
    return markdown


def write_reports(summary: dict, output_root: str | Path | None = None) -> dict[str, str]:
    root = Path(output_root).expanduser() if output_root is not None else OUTPUT_ROOT
    out_dir = root / summary['trade_date']
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / 'theme-emotion.json'
    md_path = out_dir / 'theme-emotion.md'
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    md_path.write_text(render_markdown(summary), encoding='utf-8')
    return {'json_path': str(json_path), 'markdown_path': str(md_path)}


def analyze_theme_emotion(
    trade_date: str,
    db_path: str | Path | None = None,
    output_root: str | Path | None = None,
    market_db_path: str | Path | None = MARKET_DB_PATH,
) -> dict:
    normalized_date = normalize_trade_date(trade_date)
    schema.init_db(db_path)
    sector_mapping, sector_status = load_sector_mapping(market_db_path, normalized_date)
    with schema.connect(db_path) as conn:
        rows = load_limitup_rows(conn, normalized_date)
        records = assign_theme_roles(build_theme_stock_records(rows, sector_mapping))
        theme_daily = build_theme_daily_records(records)
        anchors = build_emotion_anchors(theme_daily, records, normalized_date)
        upsert_theme_stock_map(conn, records)
        upsert_theme_daily(conn, theme_daily)
        upsert_emotion_anchors(conn, anchors)
    data_notes = build_data_notes(rows, sector_status)
    summary = build_summary(normalized_date, theme_daily, records, anchors, data_notes)
    paths = write_reports(summary, output_root)
    return {
        'ok': True,
        'trade_date': normalized_date,
        'theme_daily': [{k: v for k, v in row.items() if not k.startswith('_')} for row in theme_daily],
        'theme_stock_map': [{k: v for k, v in row.items() if not k.startswith('_')} for row in records],
        'emotion_anchors': anchors,
        'summary': summary,
        'paths': paths,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Build shadow A-share theme and emotion anchor analysis')
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument('--today', action='store_true')
    date_group.add_argument('--trade-date')
    parser.add_argument('--db-path', default=str(schema.DB_PATH))
    parser.add_argument('--output-root', default=str(OUTPUT_ROOT))
    parser.add_argument('--market-db-path', default=str(MARKET_DB_PATH))
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    trade_date = normalize_trade_date(None if args.today else args.trade_date)
    result = analyze_theme_emotion(trade_date, db_path=args.db_path, output_root=args.output_root, market_db_path=args.market_db_path)
    print(json.dumps({
        'ok': result['ok'],
        'trade_date': result['trade_date'],
        'theme_count': len(result['theme_daily']),
        'theme_stock_map_count': len(result['theme_stock_map']),
        'emotion_anchor_count': len(result['emotion_anchors']),
        'paths': result['paths'],
        'data_notes': result['summary']['data_notes'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
