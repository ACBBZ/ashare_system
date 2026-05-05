#!/usr/bin/env python3
import json
import math
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

import ashare_data_utils as adu

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
DB_PATH = ROOT / 'ashare_monitor.db'
INDEX_TARGETS = {
    'sh000001': '上证指数',
    'sz399001': '深证成指',
    'sz399006': '创业板指',
    'sh000688': '科创50',
    'sh000300': '沪深300',
    'sh000852': '中证1000',
}
MAIN_BOARD_PREFIXES = ('600', '601', '603', '605', '000', '001', '002', '003')
EXCLUDED_PREFIXES = ('300', '301', '688', '689', '8', '4')
CAPTURE_RUN_COLUMNS = {
    'captured_at': 'TEXT',
    'trade_date': 'TEXT',
    'source': 'TEXT',
    'total_stocks': 'INTEGER',
    'up_count': 'INTEGER',
    'down_count': 'INTEGER',
    'flat_count': 'INTEGER',
    'strong_up_count': 'INTEGER',
    'strong_down_count': 'INTEGER',
    'summary_json': 'TEXT',
    'market_count': 'INTEGER',
    'big_drop_count': 'INTEGER',
    'total_count': 'INTEGER',
    'fetch_method': 'TEXT',
    'created_at': 'TEXT',
}
STOCK_SNAPSHOT_COLUMNS = {
    'run_id': 'INTEGER',
    'captured_at': 'TEXT',
    'trade_date': 'TEXT',
    'code': 'TEXT',
    'name': 'TEXT',
    'latest_price': 'REAL',
    'pct_change': 'REAL',
    'change_amount': 'REAL',
    'volume': 'REAL',
    'amount': 'REAL',
    'amplitude': 'REAL',
    'turnover_rate': 'REAL',
    'volume_ratio': 'REAL',
    'pe_dynamic': 'REAL',
    'pb': 'REAL',
    'market_value': 'REAL',
    'circulating_market_value': 'REAL',
    'raw_json': 'TEXT',
    'created_at': 'TEXT',
}
INDEX_SNAPSHOT_COLUMNS = {
    'run_id': 'INTEGER',
    'captured_at': 'TEXT',
    'trade_date': 'TEXT',
    'index_code': 'TEXT',
    'index_name': 'TEXT',
    'latest_value': 'REAL',
    'pct_change': 'REAL',
    'amount': 'REAL',
    'high': 'REAL',
    'low': 'REAL',
    'raw_json': 'TEXT',
    'created_at': 'TEXT',
}
SECTOR_SNAPSHOT_COLUMNS = {
    'run_id': 'INTEGER',
    'captured_at': 'TEXT',
    'trade_date': 'TEXT',
    'sector_name': 'TEXT',
    'pct_change': 'REAL',
    'up_count': 'INTEGER',
    'down_count': 'INTEGER',
    'leader_name': 'TEXT',
    'leader_code': 'TEXT',
    'net_inflow': 'REAL',
    'net_inflow_pct': 'REAL',
    'turnover_rate': 'REAL',
    'raw_json': 'TEXT',
    'created_at': 'TEXT',
}
WATCHLIST_SNAPSHOT_COLUMNS = {
    'run_id': 'INTEGER',
    'captured_at': 'TEXT',
    'trade_date': 'TEXT',
    'code': 'TEXT',
    'name': 'TEXT',
    'source_group': 'TEXT',
    'latest_price': 'REAL',
    'pct_change': 'REAL',
    'volume_ratio': 'REAL',
    'turnover_rate': 'REAL',
    'near_support_flag': 'INTEGER',
    'near_resistance_flag': 'INTEGER',
    'intraday_note': 'TEXT',
    'raw_json': 'TEXT',
    'created_at': 'TEXT',
}
SECTOR_CONSTITUENT_SNAPSHOT_COLUMNS = {
    'run_id': 'INTEGER',
    'captured_at': 'TEXT',
    'trade_date': 'TEXT',
    'sector_name': 'TEXT',
    'code': 'TEXT',
    'name': 'TEXT',
    'latest_price': 'REAL',
    'pct_change': 'REAL',
    'amount': 'REAL',
    'turnover_rate': 'REAL',
    'role': 'TEXT',
    'is_sector_leader': 'INTEGER',
    'raw_json': 'TEXT',
    'created_at': 'TEXT',
}


def now_local():
    return datetime.now().astimezone()


def retry(fn, attempts=2, sleep_seconds=2):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(sleep_seconds)
    raise last


def safe_float(v):
    try:
        if v is None:
            return None
        if isinstance(v, str):
            v = v.replace(',', '').strip()
            if not v:
                return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None


def sanitize_value(v):
    if isinstance(v, dict):
        return {str(k): sanitize_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [sanitize_value(i) for i in v]
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, 'item'):
        try:
            return sanitize_value(v.item())
        except Exception:
            pass
    if isinstance(v, str):
        return v
    if isinstance(v, (int, bool)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    f = safe_float(v)
    return f if f is not None else str(v)


def normalize_code(code):
    s = str(code or '').strip().lower()
    for prefix in ('sh', 'sz', 'bj'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    digits = ''.join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else None


def is_main_board_code(code):
    code = str(code or '').strip()
    if code.startswith(EXCLUDED_PREFIXES):
        return False
    return code.startswith(MAIN_BOARD_PREFIXES)


def in_trading_session(dt):
    hm = dt.hour * 60 + dt.minute
    morning = 9 * 60 + 30 <= hm <= 11 * 60 + 30
    afternoon = 13 * 60 <= hm <= 15 * 60
    return dt.weekday() < 5 and (morning or afternoon)


def ensure_day_paths(trade_date):
    day_dir = ROOT / trade_date
    day_dir.mkdir(parents=True, exist_ok=True)
    return {
        'day_dir': day_dir,
        'snapshots_path': day_dir / 'snapshots.jsonl',
        'latest_summary_path': day_dir / 'latest-summary.md',
    }


def ensure_table(cur, table_name, create_sql, columns):
    cur.execute(create_sql)
    cur.execute(f'PRAGMA table_info({table_name})')
    existing = {row[1] for row in cur.fetchall()}
    for col, col_type in columns.items():
        if col not in existing:
            cur.execute(f'ALTER TABLE {table_name} ADD COLUMN {col} {col_type}')


def ensure_db_schema(conn):
    cur = conn.cursor()
    ensure_table(
        cur,
        'capture_runs',
        '''
        CREATE TABLE IF NOT EXISTS capture_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            source TEXT,
            total_stocks INTEGER,
            up_count INTEGER,
            down_count INTEGER,
            flat_count INTEGER,
            strong_up_count INTEGER,
            strong_down_count INTEGER,
            summary_json TEXT NOT NULL
        )
        ''',
        CAPTURE_RUN_COLUMNS,
    )
    ensure_table(
        cur,
        'stock_snapshots',
        '''
        CREATE TABLE IF NOT EXISTS stock_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            code TEXT,
            name TEXT,
            latest_price REAL,
            pct_change REAL,
            change_amount REAL,
            volume REAL,
            amount REAL,
            amplitude REAL,
            turnover_rate REAL,
            volume_ratio REAL,
            pe_dynamic REAL,
            pb REAL,
            market_value REAL,
            circulating_market_value REAL,
            raw_json TEXT NOT NULL
        )
        ''',
        STOCK_SNAPSHOT_COLUMNS,
    )
    ensure_table(
        cur,
        'index_snapshots',
        '''
        CREATE TABLE IF NOT EXISTS index_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            index_code TEXT,
            index_name TEXT,
            latest_value REAL,
            pct_change REAL,
            amount REAL,
            high REAL,
            low REAL,
            raw_json TEXT NOT NULL
        )
        ''',
        INDEX_SNAPSHOT_COLUMNS,
    )
    ensure_table(
        cur,
        'sector_snapshots',
        '''
        CREATE TABLE IF NOT EXISTS sector_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            sector_name TEXT,
            pct_change REAL,
            up_count INTEGER,
            down_count INTEGER,
            leader_name TEXT,
            leader_code TEXT,
            net_inflow REAL,
            net_inflow_pct REAL,
            turnover_rate REAL,
            raw_json TEXT NOT NULL
        )
        ''',
        SECTOR_SNAPSHOT_COLUMNS,
    )
    ensure_table(
        cur,
        'watchlist_snapshots',
        '''
        CREATE TABLE IF NOT EXISTS watchlist_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            code TEXT,
            name TEXT,
            source_group TEXT,
            latest_price REAL,
            pct_change REAL,
            volume_ratio REAL,
            turnover_rate REAL,
            near_support_flag INTEGER,
            near_resistance_flag INTEGER,
            intraday_note TEXT,
            raw_json TEXT NOT NULL
        )
        ''',
        WATCHLIST_SNAPSHOT_COLUMNS,
    )
    ensure_table(
        cur,
        'sector_constituent_snapshots',
        '''
        CREATE TABLE IF NOT EXISTS sector_constituent_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            sector_name TEXT,
            code TEXT,
            name TEXT,
            latest_price REAL,
            pct_change REAL,
            amount REAL,
            turnover_rate REAL,
            role TEXT,
            is_sector_leader INTEGER,
            raw_json TEXT NOT NULL
        )
        ''',
        SECTOR_CONSTITUENT_SNAPSHOT_COLUMNS,
    )
    cur.execute('CREATE INDEX IF NOT EXISTS idx_capture_runs_trade_date ON capture_runs(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_capture_runs_captured_at ON capture_runs(captured_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_stock_snapshots_trade_date ON stock_snapshots(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_stock_snapshots_code ON stock_snapshots(code)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_stock_snapshots_captured_at ON stock_snapshots(captured_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_stock_snapshots_run_id ON stock_snapshots(run_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_index_snapshots_trade_date ON index_snapshots(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_index_snapshots_captured_at ON index_snapshots(captured_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sector_snapshots_trade_date ON sector_snapshots(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sector_snapshots_captured_at ON sector_snapshots(captured_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_watchlist_snapshots_trade_date ON watchlist_snapshots(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_watchlist_snapshots_code ON watchlist_snapshots(code)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_watchlist_snapshots_captured_at ON watchlist_snapshots(captured_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sector_constituent_snapshots_trade_date ON sector_constituent_snapshots(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sector_constituent_snapshots_sector_name ON sector_constituent_snapshots(sector_name)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sector_constituent_snapshots_code ON sector_constituent_snapshots(code)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sector_constituent_snapshots_run_id ON sector_constituent_snapshots(run_id)')
    conn.commit()


def fetch_spot_df():
    errors = []
    try:
        df = adu.ak_call(ak.stock_zh_a_spot, timeout=10, attempts=2)
        if df is not None and not df.empty:
            return df.copy(), 'stock_zh_a_spot', errors
        errors.append('stock_zh_a_spot returned empty')
    except Exception as exc:
        errors.append(f'stock_zh_a_spot failed: {exc}')
    try:
        df = adu.ak_call(ak.stock_zh_a_spot_em, timeout=10, attempts=2)
        if df is not None and not df.empty:
            return df.copy(), 'stock_zh_a_spot_em', errors
        errors.append('stock_zh_a_spot_em returned empty')
    except Exception as exc:
        errors.append(f'stock_zh_a_spot_em failed: {exc}')
    try:
        df = adu.fetch_eastmoney_spot_df()
        if df is not None and not df.empty:
            return df.copy(), 'eastmoney_direct_clist', errors
        errors.append('eastmoney_direct_clist returned empty')
    except Exception as exc:
        errors.append(f'eastmoney_direct_clist failed: {exc}')
    raise RuntimeError(' ; '.join(errors) if errors else 'unable to fetch market data')


def standardize_df(df):
    col_map = {
        '代码': 'code',
        '名称': 'name',
        '最新价': 'latest_price',
        '涨跌幅': 'pct_change',
        '涨跌额': 'change_amount',
        '成交量': 'volume',
        '成交额': 'amount',
        '振幅': 'amplitude',
        '换手率': 'turnover_rate',
        '量比': 'volume_ratio',
        '市盈率-动态': 'pe_dynamic',
        '市净率': 'pb',
        '总市值': 'market_value',
        '流通市值': 'circulating_market_value',
        '时间戳': 'timestamp',
    }
    out = df.copy().rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    if 'code' in out.columns:
        out['code'] = out['code'].map(normalize_code)
        out = out[out['code'].notna()].copy()
    else:
        out['code'] = None
    if 'name' not in out.columns:
        out['name'] = None
    for col in ['latest_price', 'pct_change', 'change_amount', 'volume', 'amount', 'amplitude', 'turnover_rate', 'volume_ratio', 'pe_dynamic', 'pb', 'market_value', 'circulating_market_value']:
        if col not in out.columns:
            out[col] = None
        out[col] = pd.to_numeric(out[col], errors='coerce')
    out['is_main_board'] = out['code'].map(is_main_board_code)
    return out


def build_top_table(df, metric, limit=20, ascending=False):
    if metric not in df.columns:
        return []
    base_cols = ['code', 'name', 'latest_price', 'pct_change']
    cols = base_cols[:] if metric in base_cols else base_cols + [metric]
    sub = df[cols].copy()
    sub = sub[sub[metric].notna()].sort_values(metric, ascending=ascending).head(limit)
    records = []
    for _, row in sub.iterrows():
        item = {
            'code': str(row.get('code') or ''),
            'name': row.get('name'),
            'latest_price': safe_float(row.get('latest_price')),
            'pct_change': safe_float(row.get('pct_change')),
        }
        if metric not in item:
            item[metric] = safe_float(row.get(metric))
        records.append(sanitize_value(item))
    return records


def fetch_index_snapshots():
    errors = []
    try:
        df = adu.ak_call(ak.stock_zh_index_spot_sina, timeout=10, attempts=2)
        df['代码'] = df['代码'].astype(str)
        items = []
        for code, label in INDEX_TARGETS.items():
            sub = df[df['代码'] == code]
            if sub.empty:
                errors.append(f'{code} missing in index spot')
                continue
            row = sub.iloc[0].to_dict()
            items.append(sanitize_value({
                'index_code': code,
                'index_name': label,
                'latest_value': safe_float(row.get('最新价') or row.get('最新点位') or row.get('收盘')),
                'pct_change': safe_float(row.get('涨跌幅')),
                'amount': safe_float(row.get('成交额')),
                'high': safe_float(row.get('最高')),
                'low': safe_float(row.get('最低')),
                'raw': sanitize_value(row),
            }))
        if items:
            return items, errors
    except Exception as exc:
        errors.append(f'stock_zh_index_spot_sina failed: {exc}')
    items, fb_errors = adu.fetch_index_quotes(INDEX_TARGETS)
    return [sanitize_value(item) for item in items], errors + fb_errors


def normalize_asof_time(asof_time):
    if asof_time is None:
        return now_local().isoformat()
    if isinstance(asof_time, datetime):
        return asof_time.isoformat()
    return str(asof_time)


def _load_latest_snapshot_rows(cur, table_name, columns, target_date, asof_time, limit):
    latest = cur.execute(
        f"""
        SELECT run_id, captured_at
        FROM {table_name}
        WHERE trade_date = ?
          AND captured_at <= ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (target_date, asof_time),
    ).fetchone()
    if latest is None:
        return []
    return cur.execute(
        f"""
        SELECT {columns}
        FROM {table_name}
        WHERE trade_date = ?
          AND run_id = ?
          AND captured_at = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (target_date, latest['run_id'], latest['captured_at'], limit),
    ).fetchall()


def load_recent_sector_context_from_db(target_date=None, asof_time=None):
    if target_date is None:
        target_date = now_local().date().isoformat()
    else:
        target_date = str(target_date)
    asof_time = normalize_asof_time(asof_time)
    if not DB_PATH.exists():
        return {}, {}, {}, {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    constituent_rows = _load_latest_snapshot_rows(
        cur,
        'sector_constituent_snapshots',
        'sector_name, code, name, latest_price, pct_change, amount, turnover_rate, role, is_sector_leader, raw_json',
        target_date,
        asof_time,
        12000,
    )
    sector_rows = _load_latest_snapshot_rows(
        cur,
        'sector_snapshots',
        'sector_name, pct_change, up_count, down_count, leader_name, leader_code, net_inflow, raw_json',
        target_date,
        asof_time,
        600,
    )
    conn.close()

    code_to_sector = {}
    sector_groups = {}
    stage_map = {}
    sector_meta = {}
    for row in sector_rows:
        sector_name = str(row['sector_name'] or '')
        if not sector_name or sector_name in sector_meta:
            continue
        sector_meta[sector_name] = {
            'pct_change': safe_float(row['pct_change']),
            'up_count': int(safe_float(row['up_count']) or 0),
            'down_count': int(safe_float(row['down_count']) or 0),
            'leader_name': row['leader_name'],
            'leader_code': normalize_code(row['leader_code']),
            'net_inflow': safe_float(row['net_inflow']),
        }
        sector_meta[sector_name]['stage'] = infer_sector_stage(
            sector_meta[sector_name]['pct_change'],
            None,
            sector_meta[sector_name]['net_inflow'],
        )
        stage_map[sector_name] = sector_meta[sector_name]['stage']
    for row in constituent_rows:
        sector_name = str(row['sector_name'] or '')
        code = normalize_code(row['code'])
        if not sector_name or not code:
            continue
        item = {
            'sector_name': sector_name,
            'code': code,
            'name': row['name'],
            'latest_price': safe_float(row['latest_price']),
            'pct_change': safe_float(row['pct_change']),
            'amount': safe_float(row['amount']),
            'turnover_rate': safe_float(row['turnover_rate']),
            'role': row['role'],
            'is_sector_leader': int(safe_float(row['is_sector_leader']) or 0),
        }
        sector_groups.setdefault(sector_name, []).append(item)
        code_to_sector.setdefault(code, {
            'sector_name': sector_name,
            'role': row['role'],
            'sector_leader': (sector_meta.get(sector_name) or {}).get('leader_name') or (row['name'] if item['is_sector_leader'] else None),
            'leader_code': (sector_meta.get(sector_name) or {}).get('leader_code') or (code if item['is_sector_leader'] else None),
            'stage': stage_map.get(sector_name),
        })
    return code_to_sector, sector_groups, stage_map, sector_meta


def infer_sector_snapshots_from_cache(df, limit=8, target_date=None, asof_time=None):
    code_to_sector, sector_groups, stage_map, sector_meta = load_recent_sector_context_from_db(
        target_date=target_date,
        asof_time=asof_time,
    )
    if df is None or df.empty or not code_to_sector:
        return [], ['sector cache unavailable']
    work = df[df['is_main_board'] == True].copy()
    work['sector_name'] = work['code'].map(lambda x: (code_to_sector.get(str(x)) or {}).get('sector_name'))
    work = work[work['sector_name'].notna()].copy()
    if work.empty:
        return [], ['sector cache mapping empty']
    ranked = []
    for sector_name, sub in work.groupby('sector_name'):
        sub = sub.copy()
        sub['pct_change'] = pd.to_numeric(sub['pct_change'], errors='coerce')
        sub['amount'] = pd.to_numeric(sub['amount'], errors='coerce')
        sub['turnover_rate'] = pd.to_numeric(sub.get('turnover_rate'), errors='coerce')
        up = int((sub['pct_change'].fillna(0) > 0).sum())
        down = int((sub['pct_change'].fillna(0) < 0).sum())
        pct = safe_float(sub['pct_change'].head(5).mean()) or 0
        turnover = safe_float(sub['turnover_rate'].head(5).mean())
        amount_sum = safe_float(sub['amount'].sum()) or 0
        leader_row = sub.sort_values(['pct_change', 'amount'], ascending=False).iloc[0]
        meta = sector_meta.get(sector_name) or {}
        leader_name = leader_row.get('name') or meta.get('leader_name')
        leader_code = normalize_code(leader_row.get('code')) or meta.get('leader_code')
        score = pct * 5 + (up - down) * 0.8 + min(amount_sum / 1e8, 25)
        ranked.append((score, sanitize_value({
            'sector_name': sector_name,
            'pct_change': pct,
            'up_count': up,
            'down_count': down,
            'leader_name': leader_name,
            'leader_code': leader_code,
            'net_inflow': meta.get('net_inflow'),
            'turnover_rate': turnover,
            'raw': {'source': 'sector_constituent_cache', 'amount_sum': amount_sum},
            'flow_raw': {},
            'stage': stage_map.get(sector_name),
        })))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:limit]], ['sector snapshots inferred from recent sector_constituent cache']


def fetch_sector_snapshots(limit=8, df=None, target_date=None, asof_time=None):
    errors = []
    try:
        board = adu.ak_call(ak.stock_board_industry_name_em, timeout=30, attempts=3).copy()
    except Exception as exc:
        fallback_rows, fb_errors = infer_sector_snapshots_from_cache(
            df,
            limit=limit,
            target_date=target_date,
            asof_time=asof_time,
        )
        return fallback_rows, [f'stock_board_industry_name_em failed: {exc}'] + fb_errors
    try:
        flow = adu.ak_call(ak.stock_sector_fund_flow_rank, indicator='今日', sector_type='行业资金流', timeout=20, attempts=2).copy()
    except Exception as exc:
        flow = pd.DataFrame(columns=['名称', '今日主力净流入-净额'])
        errors.append(f'stock_sector_fund_flow_rank failed: {exc}')
    for c in ['涨跌幅', '总市值', '换手率', '上涨家数', '下跌家数']:
        if c in board.columns:
            board[c] = pd.to_numeric(board[c], errors='coerce')
    if '今日主力净流入-净额' in flow.columns:
        flow['今日主力净流入-净额'] = pd.to_numeric(flow['今日主力净流入-净额'], errors='coerce')
    flow_map = {str(r.get('名称')): sanitize_value(dict(r)) for _, r in flow.iterrows()}
    ranked = []
    for _, row in board.iterrows():
        name = str(row.get('板块名称') or '')
        if not name:
            continue
        pct = safe_float(row.get('涨跌幅')) or 0
        up = int(safe_float(row.get('上涨家数')) or 0)
        down = int(safe_float(row.get('下跌家数')) or 0)
        breadth = (up + 1) / (down + 1)
        flow_row = flow_map.get(name) or {}
        inflow = safe_float(flow_row.get('今日主力净流入-净额'))
        score = pct * 4 + breadth + (min(inflow / 1e8, 20) if inflow is not None else 0)
        ranked.append((score, sanitize_value({
            'sector_name': name,
            'pct_change': pct,
            'up_count': up,
            'down_count': down,
            'leader_name': row.get('领涨股票'),
            'leader_code': normalize_code(row.get('领涨股票代码') or row.get('领涨股票-代码')),
            'net_inflow': inflow,
            'net_inflow_pct': safe_float(flow_row.get('今日主力净流入-净占比')),
            'turnover_rate': safe_float(row.get('换手率')),
            'raw': sanitize_value(dict(row)),
            'flow_raw': flow_row,
        })))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:limit]], errors


def read_latest_close_summary():
    files = sorted(ROOT.glob('20*-*-*/close-summary.md'), reverse=True)
    return files[0] if files else None


def parse_watchlist_targets():
    targets = {}
    close_summary = read_latest_close_summary()
    if close_summary and close_summary.exists():
        section = None
        for raw_line in close_summary.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if line.startswith('### A层'):
                section = 'core'
            elif line.startswith('### B层'):
                section = 'secondary'
            elif line.startswith('### C层'):
                section = 'record'
            m = re.match(r'^###\s*([^(（]+)[（(](\d{6})[）)]', line)
            if m:
                name = m.group(1).strip()
                code = m.group(2)
                targets[code] = {'code': code, 'name': name, 'source_group': section or 'candidate'}
        content = close_summary.read_text(encoding='utf-8')
        holding_matches = re.findall(r'([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)\s*[0-9]+成（成本\s*[0-9.]+，\s*[0-9]+股）', content)
        # Older summaries may include holdings by name only; keep names if later matched by code elsewhere.
        for name in holding_matches:
            targets.setdefault(name, {'code': None, 'name': name, 'source_group': 'holding_name_only'})
    return list(targets.values())


def build_watchlist_snapshots(df, targets):
    if not targets:
        return []
    code_map = {str(t.get('code')): t for t in targets if t.get('code')}
    records = []
    for _, row in df.iterrows():
        code = str(row.get('code') or '')
        if code not in code_map:
            continue
        pct = safe_float(row.get('pct_change'))
        turn = safe_float(row.get('turnover_rate'))
        volume_ratio = safe_float(row.get('volume_ratio'))
        near_support = int(pct is not None and -2 <= pct <= 2)
        near_resistance = int(pct is not None and pct >= 7)
        note_parts = []
        if volume_ratio is not None and volume_ratio >= 2:
            note_parts.append('量比放大')
        if turn is not None and turn >= 8:
            note_parts.append('换手活跃')
        if pct is not None and pct <= -3:
            note_parts.append('分时偏弱')
        if pct is not None and pct >= 5:
            note_parts.append('分时走强')
        records.append(sanitize_value({
            'code': code,
            'name': row.get('name') or code_map[code].get('name'),
            'source_group': code_map[code].get('source_group') or 'candidate',
            'latest_price': safe_float(row.get('latest_price')),
            'pct_change': pct,
            'volume_ratio': volume_ratio,
            'turnover_rate': turn,
            'near_support_flag': near_support,
            'near_resistance_flag': near_resistance,
            'intraday_note': '；'.join(note_parts) if note_parts else '盘中常规跟踪',
            'raw': sanitize_value(row.to_dict()),
        }))
    return records


def build_index_map(index_items):
    return {str(item.get('index_name')): item for item in index_items}


def infer_sector_stage(pct_change, turnover_rate, net_inflow):
    pct_change = safe_float(pct_change) or 0
    turnover_rate = safe_float(turnover_rate) or 0
    net_inflow = safe_float(net_inflow)
    if pct_change >= 3 and turnover_rate >= 3 and (net_inflow is None or net_inflow > 0):
        return '主升'
    if pct_change >= 1.5 and turnover_rate >= 2 and (net_inflow is None or net_inflow >= 0):
        return '修复'
    if pct_change > 0:
        return '轮动'
    return '退潮'


def compute_sector_resonance_items(sector_items, index_items):
    index_map = build_index_map(index_items)
    sh_pct = safe_float((index_map.get('上证指数') or {}).get('pct_change')) or 0
    sz_pct = safe_float((index_map.get('深证成指') or {}).get('pct_change')) or 0
    items = []
    for item in sector_items:
        pct = safe_float(item.get('pct_change')) or 0
        if pct >= 2 and max(sh_pct, sz_pct) > 0:
            resonance = '是'
        elif pct >= 1 and max(sh_pct, sz_pct) >= -0.2:
            resonance = '弱共振/结构性共振'
        else:
            resonance = '弱共振/分化'
        items.append(sanitize_value({
            'sector_name': item.get('sector_name'),
            'pct_change': pct,
            'resonance': resonance,
            'sector_leader': item.get('leader_name'),
            'leader_code': item.get('leader_code'),
            'stage': infer_sector_stage(item.get('pct_change'), item.get('turnover_rate'), item.get('net_inflow')),
        }))
    return items


def fetch_sector_constituent_maps(sector_items, target_date=None, asof_time=None):
    code_to_sector = {}
    sector_leader_map = {}
    cache_rows = []
    errors = []
    cached_code_map, cached_sector_groups, cached_stage_map, cached_sector_meta = load_recent_sector_context_from_db(
        target_date=target_date,
        asof_time=asof_time,
    )
    for item in sector_items:
        sector_name = str(item.get('sector_name') or '')
        if not sector_name:
            continue
        cons = None
        try:
            cons = adu.ak_call(ak.stock_board_industry_cons_em, symbol=sector_name, timeout=20, attempts=2).copy()
            keep = [c for c in ['代码', '名称', '最新价', '涨跌幅', '成交额', '换手率', '振幅'] if c in cons.columns]
            cons = cons[keep]
            if '代码' in cons.columns:
                cons['代码'] = cons['代码'].astype(str).str.zfill(6)
                cons = cons[cons['代码'].map(is_main_board_code)].copy()
            for c in ['最新价', '涨跌幅', '成交额', '换手率', '振幅']:
                if c in cons.columns:
                    cons[c] = pd.to_numeric(cons[c], errors='coerce')
        except Exception as exc:
            errors.append(f'{sector_name} constituents failed: {exc}')
            cached_rows = cached_sector_groups.get(sector_name) or []
            if cached_rows:
                cons = pd.DataFrame([
                    {
                        '代码': r.get('code'),
                        '名称': r.get('name'),
                        '最新价': r.get('latest_price'),
                        '涨跌幅': r.get('pct_change'),
                        '成交额': r.get('amount'),
                        '换手率': r.get('turnover_rate'),
                    }
                    for r in cached_rows
                ])
        if cons is None or cons.empty:
            continue
        cons['leader_score'] = cons['涨跌幅'].fillna(0) * 0.6 + (cons['成交额'].fillna(0) / 1e8) * 0.4
        leader = cons.sort_values(['leader_score', '涨跌幅', '成交额'], ascending=False).iloc[0]
        leader_code = str(leader.get('代码'))
        leader_name = str(leader.get('名称'))
        sector_leader_map[sector_name] = {
            'sector_leader': leader_name,
            'leader_code': leader_code,
        }
        for _, row in cons.iterrows():
            code = str(row.get('代码'))
            amount = safe_float(row.get('成交额')) or 0
            pct = safe_float(row.get('涨跌幅')) or 0
            role = '补涨'
            if code == leader_code:
                role = '龙头'
            elif amount >= 2e9:
                role = '中军'
            elif pct >= 5:
                role = '补涨'
            code_to_sector[code] = {
                'sector_name': sector_name,
                'role': role,
                'spot_name': str(row.get('名称')),
                'sector_leader': leader_name,
                'leader_code': leader_code,
                'stage': cached_stage_map.get(sector_name) or item.get('stage'),
            }
            cache_rows.append(sanitize_value({
                'sector_name': sector_name,
                'code': code,
                'name': str(row.get('名称')),
                'latest_price': safe_float(row.get('最新价')),
                'pct_change': pct,
                'amount': amount,
                'turnover_rate': safe_float(row.get('换手率')),
                'role': role,
                'is_sector_leader': int(code == leader_code),
                'raw': sanitize_value(row.to_dict()),
            }))
    for code, ctx in cached_code_map.items():
        code_to_sector.setdefault(code, ctx)
    return code_to_sector, sector_leader_map, cache_rows, errors


def attach_sector_context_to_anomalies(main_df, sector_items, index_items, limit=12, target_date=None, asof_time=None):
    if main_df.empty:
        return [], [], []
    anomaly = main_df.copy()
    anomaly['signal_score'] = (
        anomaly['pct_change'].fillna(0) * 0.45 +
        anomaly['turnover_rate'].fillna(0) * 0.20 +
        anomaly['volume_ratio'].fillna(0) * 0.20 +
        (anomaly['amount'].fillna(0) / 1e8).clip(upper=100) * 0.15
    )
    anomaly = anomaly[
        (anomaly['pct_change'].fillna(0) >= 5) |
        (anomaly['turnover_rate'].fillna(0) >= 8) |
        (anomaly['volume_ratio'].fillna(0) >= 2)
    ].copy()
    anomaly = anomaly.sort_values('signal_score', ascending=False).head(limit)
    sector_resonance = {item.get('sector_name'): item for item in compute_sector_resonance_items(sector_items, index_items)}
    code_map, sector_leader_map, cache_rows, errors = fetch_sector_constituent_maps(
        sector_items,
        target_date=target_date,
        asof_time=asof_time,
    )
    rows = []
    for _, row in anomaly.iterrows():
        code = str(row.get('code') or '')
        sector_ctx = code_map.get(code) or {}
        sector_name = sector_ctx.get('sector_name')
        matched_sector = sector_resonance.get(sector_name) if sector_name else None
        rows.append(sanitize_value({
            'code': code,
            'name': row.get('name'),
            'latest_price': safe_float(row.get('latest_price')),
            'pct_change': safe_float(row.get('pct_change')),
            'amount': safe_float(row.get('amount')),
            'turnover_rate': safe_float(row.get('turnover_rate')),
            'volume_ratio': safe_float(row.get('volume_ratio')),
            'sector_name': sector_name,
            'resonance': matched_sector.get('resonance') if matched_sector else '待补充分板块映射',
            'sector_leader': sector_ctx.get('sector_leader') or (matched_sector.get('sector_leader') if matched_sector else None),
            'leader_code': sector_ctx.get('leader_code') or (matched_sector.get('leader_code') if matched_sector else None),
            'sector_stage': matched_sector.get('stage') if matched_sector else '待判断',
            'role': sector_ctx.get('role') or '待判断',
        }))
    return rows, cache_rows, errors


def build_summary(df, fetch_method, captured_at, index_items, sector_items, watchlist_items):
    pct = pd.to_numeric(df['pct_change'], errors='coerce')
    main_df = df[df['is_main_board'] == True].copy()
    main_pct = pd.to_numeric(main_df['pct_change'], errors='coerce')
    sector_resonance = compute_sector_resonance_items(sector_items, index_items)
    anomaly_focus, sector_constituent_cache, anomaly_errors = attach_sector_context_to_anomalies(
        main_df,
        sector_items,
        index_items,
        target_date=captured_at.date().isoformat(),
        asof_time=captured_at,
    )
    summary = {
        'captured_at': captured_at.isoformat(),
        'trade_date': captured_at.date().isoformat(),
        'source': 'akshare',
        'fetch_method': fetch_method,
        'total_stocks': int(len(df)),
        'market_count': int(len(df)),
        'total_count': int(len(df)),
        'up_count': int((pct > 0).sum()),
        'down_count': int((pct < 0).sum()),
        'flat_count': int((pct == 0).sum()),
        'strong_up_count': int((pct >= 9).sum()),
        'strong_down_count': int((pct <= -5).sum()),
        'big_drop_count': int((pct <= -5).sum()),
        'main_board_count': int(len(main_df)),
        'main_board_up_count': int((main_pct > 0).sum()),
        'main_board_down_count': int((main_pct < 0).sum()),
        'top_pct_change': build_top_table(df, 'pct_change'),
        'top_amount': build_top_table(df, 'amount'),
        'top_turnover_rate': build_top_table(df, 'turnover_rate'),
        'top_volume_ratio': build_top_table(df, 'volume_ratio'),
        'top_main_board_pct_change': build_top_table(main_df, 'pct_change'),
        'top_main_board_amount': build_top_table(main_df, 'amount'),
        'index_snapshots': index_items,
        'sector_snapshots': sector_items,
        'sector_resonance': sector_resonance,
        'watchlist_snapshots': watchlist_items,
        'anomaly_focus': anomaly_focus,
        'sector_constituent_cache': sector_constituent_cache,
        'anomaly_mapping_errors': anomaly_errors,
    }
    return sanitize_value(summary)


def format_num(v, digits=2):
    v = safe_float(v)
    return '数据缺失' if v is None else f'{v:.{digits}f}'


def format_yi(v):
    v = safe_float(v)
    return '数据缺失' if v is None else f'{v / 1e8:.2f}亿'


def render_table(rows, metric_key, metric_label):
    lines = [f'| 代码 | 名称 | 最新价 | 涨跌幅 | {metric_label} |', '|---|---|---:|---:|---:|']
    for row in rows:
        lines.append(
            f"| {row.get('code', '')} | {row.get('name', '')} | {format_num(row.get('latest_price'))} | {format_num(row.get('pct_change'))} | {format_num(row.get(metric_key))} |"
        )
    return '\n'.join(lines)


def render_index_lines(index_items):
    lines = []
    for item in index_items:
        lines.append(
            f"- {item.get('index_name')}：{format_num(item.get('latest_value'))} 点，涨跌幅 {format_num(item.get('pct_change'))}% ，成交额 {format_yi(item.get('amount'))}"
        )
    return '\n'.join(lines) if lines else '- 数据缺失'


def render_sector_lines(sector_items):
    if not sector_items:
        return '- 数据缺失'
    lines = []
    for item in sector_items[:6]:
        lines.append(
            f"- {item.get('sector_name')}：涨幅 {format_num(item.get('pct_change'))}% ，上涨/下跌 {item.get('up_count', 0)}/{item.get('down_count', 0)}，领涨股 {item.get('leader_name') or '数据缺失'}，净流入 {format_yi(item.get('net_inflow'))}"
        )
    return '\n'.join(lines)


def render_watchlist_lines(watchlist_items):
    if not watchlist_items:
        return '- 暂无从最近收盘摘要中解析到观察池代码'
    lines = []
    for item in watchlist_items[:12]:
        lines.append(
            f"- {item.get('name')}（{item.get('code')}，{item.get('source_group')}）：{format_num(item.get('pct_change'))}% ，量比 {format_num(item.get('volume_ratio'))}，换手 {format_num(item.get('turnover_rate'))}% ，备注：{item.get('intraday_note')}"
        )
    return '\n'.join(lines)


def render_anomaly_lines(anomaly_items):
    if not anomaly_items:
        return '- 暂无明确主板异动股'
    lines = []
    for item in anomaly_items[:12]:
        lines.append(
            f"- {item.get('name')}（{item.get('code')}）：涨跌幅 {format_num(item.get('pct_change'))}% ，成交额 {format_yi(item.get('amount'))}，换手 {format_num(item.get('turnover_rate'))}% ，量比 {format_num(item.get('volume_ratio'))}，所属板块 {item.get('sector_name') or '待补充'}，板块阶段 {item.get('sector_stage') or '待判断'}，角色 {item.get('role') or '待判断'}，共振 {item.get('resonance') or '待判断'}，带动股 {item.get('sector_leader') or '待补充'}"
        )
    return '\n'.join(lines)


def render_sector_resonance_lines(items):
    if not items:
        return '- 暂无板块共振摘要'
    lines = []
    for item in items[:8]:
        lines.append(
            f"- {item.get('sector_name')}：涨幅 {format_num(item.get('pct_change'))}% ，共振 {item.get('resonance')}，带动板块上涨的股票 {item.get('sector_leader')}（{item.get('leader_code') or '代码缺失'}）"
        )
    return '\n'.join(lines)


def write_latest_summary(summary, latest_summary_path):
    ts_display = summary['captured_at'].replace('T', ' ')
    content = f'''# A股盘面监控 - {ts_display}\n\n## 市场概览\n- 抓取时间：{ts_display}\n- 数据源：{summary.get('source', 'akshare')} / {summary.get('fetch_method', 'unknown')}\n- 全市场股票数：{summary.get('total_stocks', 0)}\n- 主板股票数：{summary.get('main_board_count', 0)}\n\n## 涨跌分布\n- 上涨家数：{summary.get('up_count', 0)}\n- 下跌家数：{summary.get('down_count', 0)}\n- 平盘家数：{summary.get('flat_count', 0)}\n- 涨停/大涨（>=9%）：{summary.get('strong_up_count', 0)}\n- 大跌（<=-5%）：{summary.get('strong_down_count', 0)}\n- 主板上涨/下跌：{summary.get('main_board_up_count', 0)} / {summary.get('main_board_down_count', 0)}\n\n## 指数快照\n{render_index_lines(summary.get('index_snapshots', []))}\n\n## 强势板块快照\n{render_sector_lines(summary.get('sector_snapshots', []))}\n\n## 板块共振摘要\n{render_sector_resonance_lines(summary.get('sector_resonance', []))}\n\n## 主板异动焦点\n{render_anomaly_lines(summary.get('anomaly_focus', []))}\n\n## 观察池快照\n{render_watchlist_lines(summary.get('watchlist_snapshots', []))}\n\n## 主板涨幅前 20\n{render_table(summary.get('top_main_board_pct_change', []), 'pct_change', '涨跌幅')}\n\n## 主板成交额前 20\n{render_table(summary.get('top_main_board_amount', []), 'amount', '成交额')}\n\n## 全市场换手率前 20\n{render_table(summary.get('top_turnover_rate', []), 'turnover_rate', '换手率')}\n\n## 全市场量比前 20\n{render_table(summary.get('top_volume_ratio', []), 'volume_ratio', '量比')}\n'''
    latest_summary_path.write_text(content, encoding='utf-8')


def append_snapshot(summary, snapshots_path):
    with snapshots_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(summary, ensure_ascii=False, allow_nan=False) + '\n')


def insert_db(summary, df, index_items, sector_items, watchlist_items):
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_db_schema(conn)
    cur = conn.cursor()
    cur.execute(
        '''
        INSERT INTO capture_runs (
            captured_at, trade_date, source, total_stocks, up_count, down_count, flat_count,
            strong_up_count, strong_down_count, summary_json, market_count, big_drop_count,
            total_count, fetch_method, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            summary['captured_at'],
            summary['trade_date'],
            summary.get('source'),
            summary.get('total_stocks'),
            summary.get('up_count'),
            summary.get('down_count'),
            summary.get('flat_count'),
            summary.get('strong_up_count'),
            summary.get('strong_down_count'),
            json.dumps(summary, ensure_ascii=False, allow_nan=False),
            summary.get('market_count'),
            summary.get('big_drop_count'),
            summary.get('total_count'),
            summary.get('fetch_method'),
            summary['captured_at'],
        ),
    )
    run_id = cur.lastrowid
    stock_rows = []
    for _, row in df.iterrows():
        raw = sanitize_value(row.to_dict())
        stock_rows.append(
            (
                run_id,
                summary['captured_at'],
                summary['trade_date'],
                row.get('code'),
                row.get('name'),
                safe_float(row.get('latest_price')),
                safe_float(row.get('pct_change')),
                safe_float(row.get('change_amount')),
                safe_float(row.get('volume')),
                safe_float(row.get('amount')),
                safe_float(row.get('amplitude')),
                safe_float(row.get('turnover_rate')),
                safe_float(row.get('volume_ratio')),
                safe_float(row.get('pe_dynamic')),
                safe_float(row.get('pb')),
                safe_float(row.get('market_value')),
                safe_float(row.get('circulating_market_value')),
                json.dumps(raw, ensure_ascii=False, allow_nan=False),
                summary['captured_at'],
            )
        )
    cur.executemany(
        '''
        INSERT INTO stock_snapshots (
            run_id, captured_at, trade_date, code, name, latest_price, pct_change, change_amount,
            volume, amount, amplitude, turnover_rate, volume_ratio, pe_dynamic, pb,
            market_value, circulating_market_value, raw_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        stock_rows,
    )
    if index_items:
        cur.executemany(
            '''
            INSERT INTO index_snapshots (
                run_id, captured_at, trade_date, index_code, index_name, latest_value, pct_change,
                amount, high, low, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [(
                run_id,
                summary['captured_at'],
                summary['trade_date'],
                item.get('index_code'),
                item.get('index_name'),
                safe_float(item.get('latest_value')),
                safe_float(item.get('pct_change')),
                safe_float(item.get('amount')),
                safe_float(item.get('high')),
                safe_float(item.get('low')),
                json.dumps(sanitize_value(item.get('raw') or item), ensure_ascii=False, allow_nan=False),
                summary['captured_at'],
            ) for item in index_items],
        )
    if sector_items:
        cur.executemany(
            '''
            INSERT INTO sector_snapshots (
                run_id, captured_at, trade_date, sector_name, pct_change, up_count, down_count,
                leader_name, leader_code, net_inflow, net_inflow_pct, turnover_rate, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [(
                run_id,
                summary['captured_at'],
                summary['trade_date'],
                item.get('sector_name'),
                safe_float(item.get('pct_change')),
                int(item.get('up_count') or 0),
                int(item.get('down_count') or 0),
                item.get('leader_name'),
                item.get('leader_code'),
                safe_float(item.get('net_inflow')),
                safe_float(item.get('net_inflow_pct')),
                safe_float(item.get('turnover_rate')),
                json.dumps(sanitize_value(item), ensure_ascii=False, allow_nan=False),
                summary['captured_at'],
            ) for item in sector_items],
        )
    if watchlist_items:
        cur.executemany(
            '''
            INSERT INTO watchlist_snapshots (
                run_id, captured_at, trade_date, code, name, source_group, latest_price, pct_change,
                volume_ratio, turnover_rate, near_support_flag, near_resistance_flag, intraday_note,
                raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [(
                run_id,
                summary['captured_at'],
                summary['trade_date'],
                item.get('code'),
                item.get('name'),
                item.get('source_group'),
                safe_float(item.get('latest_price')),
                safe_float(item.get('pct_change')),
                safe_float(item.get('volume_ratio')),
                safe_float(item.get('turnover_rate')),
                int(item.get('near_support_flag') or 0),
                int(item.get('near_resistance_flag') or 0),
                item.get('intraday_note'),
                json.dumps(sanitize_value(item.get('raw') or item), ensure_ascii=False, allow_nan=False),
                summary['captured_at'],
            ) for item in watchlist_items],
        )
    sector_cache = summary.get('sector_constituent_cache') or []
    if sector_cache:
        cur.executemany(
            '''
            INSERT INTO sector_constituent_snapshots (
                run_id, captured_at, trade_date, sector_name, code, name, latest_price, pct_change,
                amount, turnover_rate, role, is_sector_leader, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [(
                run_id,
                summary['captured_at'],
                summary['trade_date'],
                item.get('sector_name'),
                item.get('code'),
                item.get('name'),
                safe_float(item.get('latest_price')),
                safe_float(item.get('pct_change')),
                safe_float(item.get('amount')),
                safe_float(item.get('turnover_rate')),
                item.get('role'),
                int(item.get('is_sector_leader') or 0),
                json.dumps(sanitize_value(item.get('raw') or item), ensure_ascii=False, allow_nan=False),
                summary['captured_at'],
            ) for item in sector_cache],
        )
    conn.commit()
    conn.close()
    return run_id


def main():
    current = now_local()
    trade_date = current.date().isoformat()
    if adu.skip_cron_if_not_a_share_trading_day(trade_date, task='ashare-background-monitor'):
        return
    paths = ensure_day_paths(trade_date)
    if not in_trading_session(current):
        result = {
            'status': 'skipped',
            'reason': 'outside_trading_session',
            'captured_at': current.isoformat(),
            'trade_date': trade_date,
            'day_dir': str(paths['day_dir']),
        }
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return

    df_raw, fetch_method, prior_errors = fetch_spot_df()
    df = standardize_df(df_raw)
    if df.empty:
        raise RuntimeError('fetched dataframe is empty after standardization')

    index_items, index_errors = fetch_index_snapshots()
    sector_items, sector_errors = fetch_sector_snapshots(
        limit=8,
        df=df,
        target_date=trade_date,
        asof_time=current,
    )
    watchlist_targets = parse_watchlist_targets()
    watchlist_items = build_watchlist_snapshots(df, watchlist_targets)

    summary = build_summary(df, fetch_method, current, index_items, sector_items, watchlist_items)
    append_snapshot(summary, paths['snapshots_path'])
    write_latest_summary(summary, paths['latest_summary_path'])
    run_id = insert_db(summary, df, index_items, sector_items, watchlist_items)
    result = {
        'status': 'captured',
        'captured_at': summary['captured_at'],
        'trade_date': summary['trade_date'],
        'fetch_method': fetch_method,
        'run_id': run_id,
        'total_stocks': summary['total_stocks'],
        'main_board_count': summary['main_board_count'],
        'up_count': summary['up_count'],
        'down_count': summary['down_count'],
        'flat_count': summary['flat_count'],
        'strong_up_count': summary['strong_up_count'],
        'strong_down_count': summary['strong_down_count'],
        'sector_snapshot_count': len(sector_items),
        'watchlist_snapshot_count': len(watchlist_items),
        'snapshots_path': str(paths['snapshots_path']),
        'latest_summary_path': str(paths['latest_summary_path']),
        'db_path': str(DB_PATH),
        'prior_fetch_errors': prior_errors + index_errors + sector_errors + (summary.get('anomaly_mapping_errors') or []),
    }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
