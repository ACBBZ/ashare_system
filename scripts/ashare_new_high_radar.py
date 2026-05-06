#!/usr/bin/env python3
"""Stage 4A sidecar: new-high radar and theme resonance analyzer.

Scope guard:
- writes only the shadow ``new_high_daily`` table and shadow JSON/Markdown files;
- reads ``limitup_daily``, ``theme_stock_map`` and ``theme_daily`` from the shortline DB;
- optionally reads market DB tables but never writes production databases;
- does not touch close-summary/opening-brief/opening-action-table, cron or Feishu.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

DEFAULT_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/shortline/shortline_signal.db')
DEFAULT_OUTPUT_ROOT = Path('/home/admin/Notes/market/ashare-monitor/shortline')
DEFAULT_MARKET_DB_PATH = Path('/home/admin/Notes/market/ashare-monitor/ashare_monitor.db')
CST = timezone(timedelta(hours=8))
NEW_HIGH_TYPES = {'60日新高', '100日新高', '250日新高'}


def now_iso() -> str:
    return datetime.now(tz=CST).isoformat(timespec='seconds')


def today_text() -> str:
    return datetime.now(tz=CST).date().isoformat()


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace(',', '').replace('%', '')
            if not text or text in {'-', '—', 'None', 'nan'}:
                return None
            value = text
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def normalize_code(code: Any) -> str | None:
    text = re.sub(r'\D', '', str(code or ''))
    if not text:
        return None
    return text[-6:].zfill(6)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except Exception:
        return set()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_new_high_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS new_high_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            high_type TEXT NOT NULL,
            theme_name TEXT,
            sector_name TEXT,
            amount REAL,
            turnover_rate REAL,
            position_20d REAL,
            position_60d REAL,
            position_100d REAL,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code, high_type)
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_new_high_daily_trade_date ON new_high_daily(trade_date)')


def calculate_position(close_price: Any, low_price: Any, high_price: Any) -> float | None:
    close_v = safe_float(close_price)
    low_v = safe_float(low_price)
    high_v = safe_float(high_price)
    if close_v is None or low_v is None or high_v is None:
        return None
    denom = high_v - low_v
    if denom == 0:
        return None
    return round((close_v - low_v) / denom * 100, 2)


def _pick_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for name in aliases:
        if name in df.columns:
            return name
    return None


def _prepare_hist(hist_df: pd.DataFrame) -> pd.DataFrame:
    if hist_df is None or hist_df.empty:
        return pd.DataFrame(columns=['date', 'high', 'low', 'close'])
    df = hist_df.copy()
    date_col = _pick_col(df, ['date', '日期', 'trade_date', '交易日期'])
    high_col = _pick_col(df, ['high', '最高', '最高价'])
    low_col = _pick_col(df, ['low', '最低', '最低价'])
    close_col = _pick_col(df, ['close', '收盘', '收盘价', 'latest_price'])
    if high_col is None or low_col is None or close_col is None:
        return pd.DataFrame(columns=['date', 'high', 'low', 'close'])
    out = pd.DataFrame({
        'date': df[date_col].astype(str) if date_col else range(len(df)),
        'high': pd.to_numeric(df[high_col], errors='coerce'),
        'low': pd.to_numeric(df[low_col], errors='coerce'),
        'close': pd.to_numeric(df[close_col], errors='coerce'),
    })
    out = out.dropna(subset=['high', 'low', 'close']).reset_index(drop=True)
    return out


def _window_position(df: pd.DataFrame, n: int, today_close: float) -> float | None:
    if len(df) < n:
        return None
    window = df.tail(n)
    return calculate_position(today_close, window['low'].min(), window['high'].max())


def detect_high_type(hist_df: pd.DataFrame, trade_date: str) -> dict[str, Any]:
    df = _prepare_hist(hist_df)
    if df.empty:
        return {
            'high_type': '历史不足', 'position_20d': None, 'position_60d': None, 'position_100d': None,
            'insufficient_history': True, 'high_60': False, 'high_100': False, 'high_250': False,
            'history_days': 0,
        }
    # Prefer rows up to trade_date when comparable; fixtures may not include the target date.
    if 'date' in df.columns:
        maybe = df[df['date'].astype(str) <= str(trade_date)]
        if not maybe.empty:
            df = maybe
    today = df.iloc[-1]
    today_high = safe_float(today['high'])
    today_close = safe_float(today['close'])
    history_days = len(df)
    insufficient = history_days < 60 or today_high is None or today_close is None
    high_60 = high_100 = high_250 = False
    if not insufficient:
        high_60 = bool(today_high >= safe_float(df.tail(60)['high'].max()))
        if history_days >= 100:
            high_100 = bool(today_high >= safe_float(df.tail(100)['high'].max()))
        if history_days >= 250:
            high_250 = bool(today_high >= safe_float(df.tail(250)['high'].max()))
    if insufficient:
        high_type = '历史不足'
    elif high_250:
        high_type = '250日新高'
    elif high_100:
        high_type = '100日新高'
    elif high_60:
        high_type = '60日新高'
    else:
        high_type = '非新高'
    return {
        'high_type': high_type,
        'position_20d': _window_position(df, 20, today_close),
        'position_60d': _window_position(df, 60, today_close),
        'position_100d': _window_position(df, 100, today_close),
        'insufficient_history': insufficient,
        'high_60': high_60,
        'high_100': high_100,
        'high_250': high_250,
        'history_days': history_days,
    }


def load_shortline_universe(conn: sqlite3.Connection, trade_date: str, scope: str = 'shortline') -> list[dict[str, Any]]:
    if scope not in {'shortline', 'watchlist', 'all'}:
        raise ValueError(f'Unsupported scope: {scope}')
    rows: dict[str, dict[str, Any]] = {}
    if table_exists(conn, 'limitup_daily'):
        for row in conn.execute(
            '''SELECT code, name, source, amount, turnover_rate, theme AS theme_name
               FROM limitup_daily WHERE trade_date=?''',
            (trade_date,),
        ):
            code = normalize_code(row['code'])
            if code:
                rows[code] = {**dict(row), 'code': code, 'origin': 'limitup_daily'}
    if table_exists(conn, 'theme_stock_map'):
        for row in conn.execute(
            '''SELECT code, name, source, theme_name, role, confidence
               FROM theme_stock_map WHERE trade_date=?''',
            (trade_date,),
        ):
            code = normalize_code(row['code'])
            if code and code not in rows:
                rows[code] = {**dict(row), 'code': code, 'origin': 'theme_stock_map'}
            elif code:
                rows[code]['theme_name'] = rows[code].get('theme_name') or row['theme_name']
                rows[code]['role'] = rows[code].get('role') or row['role']
    if scope == 'watchlist':
        # MVP: no stable watchlist table in the shadow DB; return the stable shortline universe instead.
        return list(rows.values())
    return list(rows.values())


def resolve_theme_for_code(conn: sqlite3.Connection, trade_date: str, code: str) -> dict[str, Any]:
    code = normalize_code(code) or str(code)
    if not table_exists(conn, 'theme_stock_map'):
        return {'theme_name': None, 'theme_id': None, 'role': None, 'confidence': None, 'score': None}
    rows = conn.execute(
        '''SELECT m.theme_id, m.theme_name, m.role, m.confidence, d.score
           FROM theme_stock_map m
           LEFT JOIN theme_daily d ON d.trade_date=m.trade_date AND d.theme_id=m.theme_id
           WHERE m.trade_date=? AND m.code=?
           ORDER BY COALESCE(d.score, -1) DESC, COALESCE(m.confidence, 0) DESC, m.theme_name ASC''',
        (trade_date, code),
    ).fetchall()
    if rows:
        return dict(rows[0])
    return {'theme_name': None, 'theme_id': None, 'role': None, 'confidence': None, 'score': None}


def load_sector_map(market_db_path: str | Path | None, trade_date: str) -> tuple[dict[str, str], str | None]:
    if not market_db_path:
        return {}, '板块成分数据不可用'
    path = Path(market_db_path)
    if not path.exists():
        return {}, '板块成分数据不可用'
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, 'sector_constituent_snapshots'):
            return {}, '板块成分数据不可用'
        cols = get_columns(conn, 'sector_constituent_snapshots')
        if not {'trade_date', 'sector_name', 'code'} <= cols:
            return {}, '板块成分数据不可用'
        out = {}
        for row in conn.execute(
            '''SELECT code, sector_name, MAX(captured_at) AS captured_at
               FROM sector_constituent_snapshots WHERE trade_date=? GROUP BY code, sector_name''',
            (trade_date,),
        ):
            code = normalize_code(row['code'])
            if code and code not in out:
                out[code] = row['sector_name']
        return out, None if out else '板块成分数据不可用'
    except Exception as exc:
        return {}, f'板块成分数据不可用: {exc}'
    finally:
        try:
            conn.close()
        except Exception:
            pass


def fetch_local_history(market_db_path: str | Path | None, code: str, trade_date: str) -> pd.DataFrame:
    if not market_db_path or not Path(market_db_path).exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(market_db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, 'stock_snapshots'):
            return pd.DataFrame()
        cols = get_columns(conn, 'stock_snapshots')
        if not {'trade_date', 'code'} <= cols:
            return pd.DataFrame()
        price_col = 'latest_price' if 'latest_price' in cols else None
        if price_col is None:
            return pd.DataFrame()
        # This DB is intraday snapshot based, not true OHLC; use same price as high/low/close only as a weak local fallback.
        rows = conn.execute(
            f'''SELECT trade_date AS date, MAX({price_col}) AS high, MIN({price_col}) AS low,
                      AVG({price_col}) AS close
                FROM stock_snapshots
                WHERE code=? AND trade_date<=? AND {price_col} IS NOT NULL
                GROUP BY trade_date ORDER BY trade_date ASC''',
            (code, trade_date),
        ).fetchall()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def fetch_akshare_history(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak
    start = start_date.replace('-', '')
    end = end_date.replace('-', '')
    df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start, end_date=end, adjust='')
    return df


def upsert_new_high_daily(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    ensure_new_high_table(conn)
    now = now_iso()
    for item in rows:
        trade_date = item.get('trade_date')
        code = normalize_code(item.get('code'))
        if not trade_date or not code:
            continue
        # Schema PK includes high_type, but stage 4A semantics require one row per trade_date+code.
        conn.execute('DELETE FROM new_high_daily WHERE trade_date=? AND code=?', (trade_date, code))
        conn.execute(
            '''INSERT INTO new_high_daily (
                trade_date, code, name, high_type, theme_name, sector_name, amount, turnover_rate,
                position_20d, position_60d, position_100d, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                trade_date, code, item.get('name'), item.get('high_type') or '历史不足', item.get('theme_name'),
                item.get('sector_name'), safe_float(item.get('amount')), safe_float(item.get('turnover_rate')),
                safe_float(item.get('position_20d')), safe_float(item.get('position_60d')), safe_float(item.get('position_100d')),
                item.get('source'), item.get('created_at') or now, now,
            ),
        )
    conn.commit()


def _high_rank(high_type: str | None) -> int:
    return {'非新高': 0, '历史不足': -1, '60日新高': 1, '100日新高': 2, '250日新高': 3}.get(high_type or '', 0)


def _new_high_where(alias: str = 'n') -> str:
    return f"{alias}.high_type IN ('60日新高','100日新高','250日新高')"


def build_new_high_summary(conn: sqlite3.Connection, trade_date: str) -> dict[str, Any]:
    ensure_new_high_table(conn)
    rows = [dict(r) for r in conn.execute('SELECT * FROM new_high_daily WHERE trade_date=?', (trade_date,)).fetchall()]
    limitup_by_code = {}
    if table_exists(conn, 'limitup_daily'):
        limitup_by_code = {normalize_code(r['code']): dict(r) for r in conn.execute('SELECT * FROM limitup_daily WHERE trade_date=?', (trade_date,)).fetchall() if normalize_code(r['code'])}
    top_theme_names = set()
    theme_scores = {}
    if table_exists(conn, 'theme_daily'):
        for r in conn.execute('SELECT theme_name, score FROM theme_daily WHERE trade_date=? ORDER BY COALESCE(score,0) DESC LIMIT 5', (trade_date,)):
            top_theme_names.add(r['theme_name'])
            theme_scores[r['theme_name']] = safe_float(r['score']) or 0
    theme_map = {}
    if table_exists(conn, 'theme_stock_map'):
        for r in conn.execute('SELECT code, theme_name, role FROM theme_stock_map WHERE trade_date=?', (trade_date,)):
            theme_map.setdefault(normalize_code(r['code']), []).append(dict(r))

    total_checked = len(rows)
    new_high_60_count = sum(1 for r in rows if _high_rank(r.get('high_type')) >= 1)
    new_high_100_count = sum(1 for r in rows if _high_rank(r.get('high_type')) >= 2)
    new_high_250_count = sum(1 for r in rows if _high_rank(r.get('high_type')) >= 3)
    insufficient_history_count = sum(1 for r in rows if r.get('high_type') == '历史不足')
    sources = sorted({r.get('source') for r in rows if r.get('source')})

    theme_groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get('high_type') not in NEW_HIGH_TYPES:
            continue
        theme = r.get('theme_name') or '未映射题材'
        theme_groups.setdefault(theme, []).append(r)
    theme_resonance = []
    for theme, group in theme_groups.items():
        reps = sorted(group, key=lambda r: (_high_rank(r.get('high_type')), safe_float(r.get('amount')) or 0), reverse=True)[:5]
        has_limitup = any('limitup' in str(limitup_by_code.get(normalize_code(r.get('code')), {}).get('source') or '') for r in group)
        theme_resonance.append({
            'theme_name': theme,
            'new_high_count': len(group),
            'new_high_100_count': sum(1 for r in group if _high_rank(r.get('high_type')) >= 2),
            'new_high_250_count': sum(1 for r in group if _high_rank(r.get('high_type')) >= 3),
            'is_top_theme': theme in top_theme_names,
            'representative_stocks': [{'code': r.get('code'), 'name': r.get('name'), 'high_type': r.get('high_type')} for r in reps],
            'has_limitup_resonance': has_limitup,
            'score': theme_scores.get(theme),
        })
    theme_resonance.sort(key=lambda x: (x['new_high_250_count'], x['new_high_100_count'], x['new_high_count'], x.get('score') or 0), reverse=True)

    limitup_new_high = []
    strong_theme_new_high = []
    risk_items = []
    negative_themes = set()
    for code, lu in limitup_by_code.items():
        src = str(lu.get('source') or '')
        if 'broken' in src or 'downlimit' in src or lu.get('is_broken_board') == 1:
            theme = lu.get('theme') or lu.get('reason')
            if theme:
                negative_themes.add(theme)
    for r in rows:
        code = normalize_code(r.get('code'))
        high_type = r.get('high_type')
        if high_type in NEW_HIGH_TYPES:
            lu = limitup_by_code.get(code, {})
            src = str(lu.get('source') or '')
            mapped = theme_map.get(code, [])
            role = mapped[0].get('role') if mapped else None
            item = {**r, 'role': role, 'limitup_source': src, 'consecutive_board_count': lu.get('consecutive_board_count'), 'is_broken_board': lu.get('is_broken_board')}
            if 'limitup' in src:
                limitup_new_high.append(item)
            if r.get('theme_name') in top_theme_names and high_type in {'100日新高', '250日新高'}:
                strong_theme_new_high.append(item)
            if 'broken' in src or lu.get('is_broken_board') == 1:
                risk_items.append({'risk': '新高但炸板', 'code': r.get('code'), 'name': r.get('name'), 'theme_name': r.get('theme_name'), 'detail': f"{high_type} 但 source={src}"})
            if 'downlimit' in src:
                risk_items.append({'risk': '新高但跌停/亏钱效应', 'code': r.get('code'), 'name': r.get('name'), 'theme_name': r.get('theme_name'), 'detail': f"{high_type} 但 source={src}"})
            if r.get('theme_name') in negative_themes:
                risk_items.append({'risk': '新高但同题材有负反馈', 'code': r.get('code'), 'name': r.get('name'), 'theme_name': r.get('theme_name'), 'detail': '同题材存在 broken/downlimit 个股'})
            if src == 'strong' or (('strong' in src) and 'limitup' not in src):
                risk_items.append({'risk': '只有强势股池信号但非涨停', 'code': r.get('code'), 'name': r.get('name'), 'theme_name': r.get('theme_name'), 'detail': f'{high_type} 但非涨停，不应误判为主线龙头'})
        elif high_type == '历史不足':
            risk_items.append({'risk': '历史数据不足', 'code': r.get('code'), 'name': r.get('name'), 'theme_name': r.get('theme_name'), 'detail': '无法判断 60/100/250 日新高'})

    missing_fields = {
        'history': '存在历史行情不足' if insufficient_history_count else '历史行情满足检测或无检查项',
        'amount_turnover_rate': 'amount/turnover_rate 存在缺失' if any(r.get('amount') is None or r.get('turnover_rate') is None for r in rows) else 'amount/turnover_rate 可用或部分可用',
        'theme_mapping': '存在 theme 映射缺失' if any(not r.get('theme_name') for r in rows) else 'theme 映射可用',
        'sector_mapping': '存在 sector 映射缺失' if any(not r.get('sector_name') for r in rows) else 'sector 映射可用',
    }
    return {
        'trade_date': trade_date,
        'total_checked': total_checked,
        'new_high_60_count': new_high_60_count,
        'new_high_100_count': new_high_100_count,
        'new_high_250_count': new_high_250_count,
        'insufficient_history_count': insufficient_history_count,
        'theme_resonance': theme_resonance,
        'limitup_new_high': sorted(limitup_new_high, key=lambda r: (_high_rank(r.get('high_type')), safe_float(r.get('amount')) or 0), reverse=True)[:30],
        'strong_theme_new_high': sorted(strong_theme_new_high, key=lambda r: (_high_rank(r.get('high_type')), safe_float(r.get('amount')) or 0), reverse=True)[:30],
        'risk_items': risk_items[:50],
        'source_errors': {},
        'missing_fields': missing_fields,
        'sources': sources,
        'generated_at': now_iso(),
    }


def fmt_num(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return '—'
    if abs(v) >= 1e8:
        return f'{v/1e8:.2f}亿'
    if abs(v) >= 1e4:
        return f'{v/1e4:.2f}万'
    return f'{v:.2f}'


def render_new_high_markdown(summary: dict[str, Any]) -> str:
    lines = ['# A 股百日新高与题材共振雷达', '']
    lines += ['## 1. 总览']
    lines += [
        f"- 交易日：{summary.get('trade_date')}",
        f"- 检查股票数：{summary.get('total_checked', 0)}",
        f"- 60日新高数量：{summary.get('new_high_60_count', 0)}",
        f"- 100日新高数量：{summary.get('new_high_100_count', 0)}",
        f"- 250日新高数量：{summary.get('new_high_250_count', 0)}",
        f"- 历史不足数量：{summary.get('insufficient_history_count', 0)}",
        f"- 数据源：{', '.join(summary.get('sources') or []) or '—'}",
        f"- 数据时间：{summary.get('generated_at') or '—'}",
        '',
    ]
    lines += ['## 2. 题材共振']
    if summary.get('theme_resonance'):
        lines += ['| 题材名称 | 新高股数量 | 100日/250日新高数量 | 今日Top题材 | 代表股票 | 涨停共振 |', '|---|---:|---:|---|---|---|']
        for item in summary['theme_resonance'][:20]:
            reps = '、'.join(f"{r.get('name') or r.get('code')}({r.get('high_type')})" for r in item.get('representative_stocks', [])) or '—'
            lines.append(f"| {item.get('theme_name')} | {item.get('new_high_count')} | {item.get('new_high_100_count')}/{item.get('new_high_250_count')} | {'是' if item.get('is_top_theme') else '否'} | {reps} | {'是' if item.get('has_limitup_resonance') else '否'} |")
    else:
        lines.append('- 暂无题材新高共振。')
    lines.append('')

    lines += ['## 3. 新高 + 涨停共振']
    if summary.get('limitup_new_high'):
        lines += ['| 股票 | 题材 | high_type | 连板数 | 成交额 | 炸板/负反馈 |', '|---|---|---|---:|---:|---|']
        for r in summary['limitup_new_high'][:20]:
            neg = '是' if ('broken' in str(r.get('limitup_source') or '') or r.get('is_broken_board') == 1) else '否'
            lines.append(f"| {r.get('name') or r.get('code')}（{r.get('code')}） | {r.get('theme_name') or '—'} | {r.get('high_type')} | {r.get('consecutive_board_count') or '—'} | {fmt_num(r.get('amount'))} | {neg} |")
    else:
        lines.append('- 暂无新高 + 涨停共振。')
    lines.append('')

    lines += ['## 4. 强题材趋势新高']
    if summary.get('strong_theme_new_high'):
        lines += ['| 股票 | 题材 | 角色 | high_type | position_20d | position_60d | position_100d |', '|---|---|---|---|---:|---:|---:|']
        for r in summary['strong_theme_new_high'][:20]:
            lines.append(f"| {r.get('name') or r.get('code')}（{r.get('code')}） | {r.get('theme_name') or '—'} | {r.get('role') or '—'} | {r.get('high_type')} | {r.get('position_20d') if r.get('position_20d') is not None else '—'} | {r.get('position_60d') if r.get('position_60d') is not None else '—'} | {r.get('position_100d') if r.get('position_100d') is not None else '—'} |")
    else:
        lines.append('- 暂无强题材趋势新高。')
    lines.append('')

    lines += ['## 5. 风险项']
    if summary.get('risk_items'):
        for item in summary['risk_items'][:30]:
            lines.append(f"- {item.get('risk')}：{item.get('name') or item.get('code')}（{item.get('code')}），题材 {item.get('theme_name') or '—'}；{item.get('detail') or ''}")
    else:
        lines.append('- 暂无显著风险项。')
    lines.append('')

    lines += ['## 6. 数据缺失说明']
    missing = summary.get('missing_fields') or {}
    lines += [
        f"- 历史行情：{missing.get('history', '—')}",
        f"- amount/turnover_rate：{missing.get('amount_turnover_rate', '—')}",
        f"- theme 映射：{missing.get('theme_mapping', '—')}",
        f"- sector 映射：{missing.get('sector_mapping', '—')}",
        f"- source_errors：{json.dumps(summary.get('source_errors') or {}, ensure_ascii=False)}",
        '',
    ]
    lines += ['## 7. 风险提示']
    lines += [
        '- 历史新高不等于买点。',
        '- 新高需要结合题材、量能、承接和市场环境。',
        '- 本报告只用于复盘辅助，不构成投资建议。',
        '',
    ]
    return '\n'.join(lines)


def _hist_start_date(trade_date: str) -> str:
    dt = datetime.fromisoformat(trade_date).date() - timedelta(days=430)
    return dt.isoformat()


def run_new_high_radar(
    trade_date: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    market_db_path: str | Path | None = DEFAULT_MARKET_DB_PATH,
    scope: str = 'shortline',
    hist_fetcher: Callable[[str, str, str], pd.DataFrame] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    output_root = Path(output_root)
    source_errors: dict[str, str] = {}
    hist_fetcher = hist_fetcher or (lambda code, start, end: fetch_akshare_history(code, start, end))
    with connect(db_path) as conn:
        ensure_new_high_table(conn)
        universe = load_shortline_universe(conn, trade_date, scope=scope)
        sector_map, sector_note = load_sector_map(market_db_path, trade_date)
        rows = []
        start_date = _hist_start_date(trade_date)
        for item in universe:
            code = normalize_code(item.get('code'))
            if not code:
                continue
            data_source = source or 'local_stock_snapshots'
            hist = fetch_local_history(market_db_path, code, trade_date)
            local_good = not hist.empty and {'high', 'low', 'close'} <= set(hist.columns) and len(hist) >= 60
            if not local_good:
                try:
                    hist = hist_fetcher(code, start_date, trade_date)
                    data_source = source or 'akshare_hist'
                except Exception as exc:
                    source_errors[code] = str(exc)
                    hist = pd.DataFrame()
                    data_source = source or 'akshare_hist_error'
            detected = detect_high_type(hist, trade_date)
            theme = resolve_theme_for_code(conn, trade_date, code)
            theme_name = theme.get('theme_name') or item.get('theme_name')
            rows.append({
                'trade_date': trade_date,
                'code': code,
                'name': item.get('name') or code,
                'high_type': detected['high_type'],
                'theme_name': theme_name,
                'sector_name': sector_map.get(code),
                'amount': item.get('amount'),
                'turnover_rate': item.get('turnover_rate'),
                'position_20d': detected.get('position_20d'),
                'position_60d': detected.get('position_60d'),
                'position_100d': detected.get('position_100d'),
                'source': data_source,
            })
        upsert_new_high_daily(conn, rows)
        summary = build_new_high_summary(conn, trade_date)
        summary['source_errors'] = source_errors
        if sector_note:
            summary['missing_fields']['sector_mapping'] = sector_note
        out_dir = output_root / trade_date
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / 'new-high-radar.json'
        md_path = out_dir / 'new-high-radar.md'
        markdown = render_new_high_markdown(summary)
        payload = {'summary': summary, 'rows': rows, 'generated_at': now_iso()}
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        md_path.write_text(markdown, encoding='utf-8')
    return {'ok': True, 'trade_date': trade_date, 'summary': summary, 'row_count': len(rows), 'paths': {'json_path': str(json_path), 'markdown_path': str(md_path)}}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build A-share new-high resonance sidecar report.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--today', action='store_true')
    group.add_argument('--trade-date')
    parser.add_argument('--scope', choices=['shortline', 'watchlist', 'all'], default='shortline')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    parser.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument('--market-db-path', default=str(DEFAULT_MARKET_DB_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trade_date = today_text() if args.today or not args.trade_date else args.trade_date
    result = run_new_high_radar(
        trade_date=trade_date,
        db_path=args.db_path,
        output_root=args.output_root,
        market_db_path=args.market_db_path,
        scope=args.scope,
    )
    print(json.dumps({
        'ok': result['ok'],
        'trade_date': result['trade_date'],
        'row_count': result['row_count'],
        'new_high_60_count': result['summary']['new_high_60_count'],
        'new_high_100_count': result['summary']['new_high_100_count'],
        'new_high_250_count': result['summary']['new_high_250_count'],
        'insufficient_history_count': result['summary']['insufficient_history_count'],
        'paths': result['paths'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
