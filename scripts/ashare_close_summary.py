#!/usr/bin/env python3
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

import ashare_data_utils as adu
import ashare_strategy_engine as ase

try:
    import ashare_ledger_lib as ledger_lib
except Exception:
    ledger_lib = None

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
TODAY = datetime.now().astimezone().date().isoformat()
DAY_DIR = ROOT / TODAY
CLOSE_SUMMARY = DAY_DIR / 'close-summary.md'
CONTEXT_JSON = DAY_DIR / 'close-summary-context.json'
DB_PATH = ROOT / 'ashare_monitor.db'
INDEX_LABEL_ORDER = ['上证指数', '深证成指', '创业板指', '科创50', '沪深300', '中证1000']
MAIN_BOARD_PREFIXES = ('600', '601', '603', '605', '000', '001', '002', '003')
EXCLUDED_PREFIXES = ('300', '301', '688', '689', '8', '4')


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


def fmt_pct(v):
    v = safe_float(v)
    return '数据缺失' if v is None else f'{v:.2f}%'


def fmt_yi(v):
    v = safe_float(v)
    return '数据缺失' if v is None else f'{v/1e8:.2f}亿'


def fmt_num(v, nd=2):
    v = safe_float(v)
    return '数据缺失' if v is None else f'{v:.{nd}f}'


def is_main_board_code(code):
    code = str(code or '').strip()
    if code.startswith(EXCLUDED_PREFIXES):
        return False
    return code.startswith(MAIN_BOARD_PREFIXES)


def normalize_code(code):
    s = str(code or '').strip().lower()
    for prefix in ('sh', 'sz', 'bj'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    digits = ''.join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else None


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def load_db_rows(query, params=()):
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, params)
    rows = [row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def asof_to_text(asof_time=None):
    asof_time = asof_time or datetime.now().astimezone()
    return asof_time.isoformat() if hasattr(asof_time, 'isoformat') else str(asof_time)


def read_latest_capture(target_date=None, asof_time=None):
    target_date = target_date or TODAY
    asof_text = asof_to_text(asof_time)
    rows = load_db_rows(
        "SELECT * FROM capture_runs WHERE trade_date = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
        (target_date, asof_text),
    )
    if not rows:
        return None
    row = rows[0]
    try:
        row['summary'] = json.loads(row.get('summary_json') or '{}')
    except Exception:
        row['summary'] = {}
    return row


def load_snapshot_layers(run_id, target_date=None):
    if target_date:
        index_rows = load_db_rows(
            "SELECT * FROM index_snapshots WHERE run_id = ? AND trade_date = ? ORDER BY id ASC",
            (run_id, target_date),
        )
        sector_rows = load_db_rows(
            "SELECT * FROM sector_snapshots WHERE run_id = ? AND trade_date = ? ORDER BY id ASC",
            (run_id, target_date),
        )
        watchlist_rows = load_db_rows(
            "SELECT * FROM watchlist_snapshots WHERE run_id = ? AND trade_date = ? ORDER BY id ASC",
            (run_id, target_date),
        )
        sector_constituent_rows = load_db_rows(
            "SELECT * FROM sector_constituent_snapshots WHERE run_id = ? AND trade_date = ? ORDER BY sector_name ASC, is_sector_leader DESC, amount DESC",
            (run_id, target_date),
        )
    else:
        index_rows = load_db_rows(
            "SELECT * FROM index_snapshots WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        )
        sector_rows = load_db_rows(
            "SELECT * FROM sector_snapshots WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        )
        watchlist_rows = load_db_rows(
            "SELECT * FROM watchlist_snapshots WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        )
        sector_constituent_rows = load_db_rows(
            "SELECT * FROM sector_constituent_snapshots WHERE run_id = ? ORDER BY sector_name ASC, is_sector_leader DESC, amount DESC",
            (run_id,),
        )
    return index_rows, sector_rows, watchlist_rows, sector_constituent_rows


def load_latest_layer_rows(table_name, trade_date, order_by='id ASC', asof_time=None):
    asof_text = asof_to_text(asof_time)
    run_rows = load_db_rows(
        f"SELECT run_id FROM {table_name} WHERE trade_date = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
        (trade_date, asof_text),
    )
    if not run_rows:
        return []
    run_id = run_rows[0].get('run_id')
    if run_id is None:
        return []
    return load_db_rows(
        f"SELECT * FROM {table_name} WHERE run_id = ? AND trade_date = ? ORDER BY {order_by}",
        (run_id, trade_date),
    )


def get_db_trade_dates(limit=10):
    rows = load_db_rows(
        "SELECT DISTINCT trade_date FROM capture_runs WHERE trade_date IS NOT NULL ORDER BY trade_date DESC LIMIT ?",
        (limit,),
    )
    dates = [str(r.get('trade_date')) for r in rows if r.get('trade_date')]
    return dates


def get_fs_trade_dates(limit=10):
    dates = []
    for path in ROOT.iterdir() if ROOT.exists() else []:
        name = path.name
        if len(name) == 10 and name[4] == '-' and name[7] == '-':
            dates.append(name)
    return sorted(dates, reverse=True)[:limit]


def get_db_total_amount(trade_date, asof_time=None):
    asof_text = asof_to_text(asof_time)
    run_rows = load_db_rows(
        "SELECT id FROM capture_runs WHERE trade_date = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
        (trade_date, asof_text),
    )
    if not run_rows:
        return None
    run_id = run_rows[0].get('id')
    sum_rows = load_db_rows(
        "SELECT SUM(amount) AS total_amount FROM stock_snapshots WHERE run_id = ? AND trade_date = ?",
        (run_id, trade_date),
    )
    if not sum_rows:
        return None
    return safe_float(sum_rows[0].get('total_amount'))


def fetch_tencent_quote(symbol):
    try:
        return adu.fetch_tencent_quote(symbol)
    except Exception:
        return None


def get_trade_dates(target_date=None):
    target_date = target_date or TODAY
    db_dates = get_db_trade_dates(limit=20)
    if target_date in db_dates:
        prev_dates = [d for d in db_dates if d < target_date]
        return target_date, (prev_dates[0] if prev_dates else target_date)
    try:
        df = adu.ak_call(ak.stock_zh_index_daily, symbol='sh000001', timeout=20, attempts=3)
        df = df.tail(30).copy()
        dates = sorted(str(x) for x in df['date'].tolist() if str(x) <= target_date)
        prev_dates = [d for d in dates if d < target_date]
        if target_date in dates:
            return target_date, (prev_dates[-1] if prev_dates else target_date)
        if prev_dates:
            return target_date, prev_dates[-1]
    except Exception:
        pass
    fs_dates = get_fs_trade_dates(limit=20)
    prev_dates = [d for d in fs_dates if d < target_date]
    if target_date in fs_dates:
        return target_date, (prev_dates[0] if prev_dates else target_date)
    if prev_dates:
        return target_date, prev_dates[0]
    db_prev_dates = [d for d in db_dates if d < target_date]
    if db_prev_dates:
        return target_date, db_prev_dates[0]
    return target_date, target_date


def get_exchange_amounts(today_yyyymmdd, prev_yyyymmdd, asof_time=None):
    today_amt = None
    prev_amt = None

    def sse_amount(df):
        row = df[df['单日情况'] == '成交金额']
        stock = safe_float(row.iloc[0].get('股票')) if not row.empty else None
        return None if stock is None else stock * 1e8

    def sz_amount(df):
        row = df[df['证券类别'] == '股票']
        return safe_float(row.iloc[0].get('成交金额')) if not row.empty else None

    try:
        sse_today = adu.ak_call(ak.stock_sse_deal_daily, date=today_yyyymmdd, timeout=20, attempts=3)
        sse_prev = adu.ak_call(ak.stock_sse_deal_daily, date=prev_yyyymmdd, timeout=20, attempts=3)
        sz_today = adu.ak_call(ak.stock_szse_summary, date=today_yyyymmdd, timeout=20, attempts=3)
        sz_prev = adu.ak_call(ak.stock_szse_summary, date=prev_yyyymmdd, timeout=20, attempts=3)
        today_amt = (sse_amount(sse_today) or 0) + (sz_amount(sz_today) or 0)
        prev_amt = (sse_amount(sse_prev) or 0) + (sz_amount(sz_prev) or 0)
    except Exception:
        pass

    if not today_amt:
        today_amt = get_db_total_amount(today_yyyymmdd[:4] + '-' + today_yyyymmdd[4:6] + '-' + today_yyyymmdd[6:8], asof_time=asof_time)
    if not prev_amt:
        prev_amt = get_db_total_amount(prev_yyyymmdd[:4] + '-' + prev_yyyymmdd[4:6] + '-' + prev_yyyymmdd[6:8], asof_time=asof_time)

    today_amt = today_amt or 0
    prev_amt = prev_amt or 0
    return {
        'today': today_amt,
        'prev': prev_amt,
        'delta': today_amt - prev_amt,
        'delta_pct': ((today_amt / prev_amt - 1) * 100) if prev_amt else None,
        'source': 'akshare_exchange' if today_amt and prev_amt else 'db_stock_snapshots_fallback',
    }


def get_limit_stats(today_yyyymmdd, latest_capture=None, run_id=None, target_date=None):
    try:
        zt = adu.ak_call(ak.stock_zt_pool_em, date=today_yyyymmdd, timeout=20, attempts=3)
        dt = adu.ak_call(ak.stock_zt_pool_dtgc_em, date=today_yyyymmdd, timeout=20, attempts=3)
        try:
            strong = adu.ak_call(ak.stock_zt_pool_strong_em, date=today_yyyymmdd, timeout=20, attempts=2)
        except Exception:
            strong = pd.DataFrame()
        try:
            zbgc = adu.ak_call(ak.stock_zt_pool_zbgc_em, date=today_yyyymmdd, timeout=20, attempts=2)
        except Exception:
            zbgc = pd.DataFrame()
        lb_col = next((c for c in ['连板数', '连板高度', '几天几板', '涨停统计'] if c in zt.columns), None)
        max_lb = None
        if lb_col:
            vals = pd.to_numeric(zt[lb_col], errors='coerce')
            if vals.notna().any():
                max_lb = int(vals.max())
        return {
            'zt_count': int(len(zt)),
            'dt_count': int(len(dt)),
            'max_lb': max_lb,
            'zt_df': zt,
            'strong_df': strong,
            'zbgc_df': zbgc,
            'source': 'akshare_limit_pool',
        }
    except Exception:
        zt_count = safe_float((latest_capture or {}).get('strong_up_count'))
        dt_count = safe_float((latest_capture or {}).get('strong_down_count'))
        if run_id and (zt_count is None or dt_count is None):
            if target_date:
                rows = load_db_rows(
                    "SELECT pct_change FROM stock_snapshots WHERE run_id = ? AND trade_date = ?",
                    (run_id, target_date),
                )
            else:
                rows = load_db_rows(
                    "SELECT pct_change FROM stock_snapshots WHERE run_id = ?",
                    (run_id,),
                )
            pcts = [safe_float(r.get('pct_change')) for r in rows]
            pcts = [p for p in pcts if p is not None]
            if zt_count is None:
                zt_count = sum(1 for p in pcts if p >= 9.5)
            if dt_count is None:
                dt_count = sum(1 for p in pcts if p <= -5)
        return {
            'zt_count': int(zt_count or 0),
            'dt_count': int(dt_count or 0),
            'max_lb': None,
            'zt_df': pd.DataFrame(),
            'strong_df': pd.DataFrame(),
            'zbgc_df': pd.DataFrame(),
            'source': 'db_capture_fallback',
        }


def get_board_data_fallback(limit=5):
    try:
        board = adu.ak_call(ak.stock_board_industry_name_em, timeout=25, attempts=3).copy()
    except Exception:
        return []
    try:
        flow = adu.ak_call(ak.stock_sector_fund_flow_rank, indicator='今日', sector_type='行业资金流', timeout=25, attempts=3).copy()
    except Exception:
        flow = pd.DataFrame(columns=['名称', '今日主力净流入-净额', '今日主力净流入-净占比'])
    for c in ['涨跌幅', '总市值', '换手率', '上涨家数', '下跌家数', '领涨股票-涨跌幅']:
        if c in board.columns:
            board[c] = pd.to_numeric(board[c], errors='coerce')
    for c in ['今日主力净流入-净额', '今日主力净流入-净占比']:
        if c in flow.columns:
            flow[c] = pd.to_numeric(flow[c], errors='coerce')
    flow_map = {str(r['名称']): dict(r) for _, r in flow.iterrows()}
    scored = []
    for _, r in board.iterrows():
        name = str(r['板块名称'])
        total_mv = safe_float(r.get('总市值')) or 0
        pct = safe_float(r.get('涨跌幅')) or 0
        up = safe_float(r.get('上涨家数')) or 0
        down = safe_float(r.get('下跌家数')) or 0
        breadth = (up + 1) / (down + 1)
        flow_row = flow_map.get(name)
        inflow = safe_float(flow_row.get('今日主力净流入-净额')) if flow_row else None
        inflow_bonus = 0 if inflow is None else min(inflow / 1e8, 20)
        missing_flow_penalty = -4 if flow_row is None else 0
        score = pct * 3 + breadth + min(math.log10(max(total_mv, 1)), 14) + inflow_bonus + missing_flow_penalty
        if total_mv >= 3e11 and pct >= 1:
            scored.append((score, name, dict(r), flow_row or {}))
    top = sorted(scored, reverse=True)[:limit]
    return [
        {
            'sector_name': name,
            'pct_change': safe_float(br.get('涨跌幅')),
            'up_count': int(safe_float(br.get('上涨家数')) or 0),
            'down_count': int(safe_float(br.get('下跌家数')) or 0),
            'leader_name': br.get('领涨股票'),
            'leader_code': normalize_code(br.get('领涨股票代码') or br.get('领涨股票-代码')),
            'net_inflow': safe_float(flow_row.get('今日主力净流入-净额')),
            'net_inflow_pct': safe_float(flow_row.get('今日主力净流入-净占比')),
            'turnover_rate': safe_float(br.get('换手率')),
            'raw_board': br,
            'raw_flow': flow_row,
        }
        for _, name, br, flow_row in top
    ]


def get_sector_constituents(sector_name):
    cons = adu.ak_call(ak.stock_board_industry_cons_em, symbol=sector_name, timeout=25, attempts=3).copy()
    keep = [c for c in ['代码', '名称', '最新价', '涨跌幅', '成交额', '换手率', '振幅'] if c in cons.columns]
    cons = cons[keep]
    if '代码' in cons.columns:
        cons['代码'] = cons['代码'].astype(str).str.zfill(6)
        cons = cons[cons['代码'].map(is_main_board_code)].copy()
    for c in ['最新价', '涨跌幅', '成交额', '换手率', '振幅']:
        if c in cons.columns:
            cons[c] = pd.to_numeric(cons[c], errors='coerce')
    return cons


def get_daily_metrics(code):
    symbol = ('sh' if str(code).startswith('6') else 'sz') + str(code)
    try:
        df = adu.ak_call(ak.stock_zh_a_daily, symbol=symbol, start_date='20251001', end_date=TODAY.replace('-', ''), adjust='qfq', timeout=25, attempts=3)
        source = 'akshare_daily'
    except Exception:
        df = adu.fetch_hist_df_with_fallback(code, '20251001', TODAY.replace('-', ''), adjust='qfq')
        source = df.attrs.get('source', 'hist_fallback')
    if df is not None and not df.empty:
        for w in [5, 10, 20, 60]:
            df[f'ma{w}'] = df['close'].rolling(w).mean()
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest
        win = df.tail(min(60, len(df)))
        high = float(win['high'].max())
        low = float(win['low'].min())
        pos = None if high == low else (float(latest['close']) - low) / (high - low)
        return {
            'close': float(latest['close']),
            'chg_1d': (float(latest['close']) / float(prev['close']) - 1) * 100 if float(prev['close']) else None,
            'ma5': safe_float(latest.get('ma5')),
            'ma10': safe_float(latest.get('ma10')),
            'ma20': safe_float(latest.get('ma20')),
            'ma60': safe_float(latest.get('ma60')),
            'amount': safe_float(latest.get('amount')),
            'turnover_daily': safe_float(latest.get('turnover')),
            'pos_60d': pos * 100 if pos is not None else None,
            'source': source,
        }
    quote = adu.fetch_quote_with_fallback(code)
    return {
        'close': safe_float(quote.get('latest')),
        'chg_1d': safe_float(quote.get('pct') or quote.get('change_pct')),
        'ma5': None,
        'ma10': None,
        'ma20': None,
        'ma60': None,
        'amount': safe_float(quote.get('amount')),
        'turnover_daily': None,
        'pos_60d': None,
        'source': quote.get('source') or 'quote_fallback',
    }


def detect_sector_lb(limit_df, sector_name):
    if limit_df is None or limit_df.empty:
        return None, None
    sector_cols = [c for c in limit_df.columns if '行业' in str(c) or '所属' in str(c) or '板块' in str(c)]
    lb_col = next((c for c in ['连板数', '连板高度', '几天几板', '涨停统计'] if c in limit_df.columns), None)
    name_col = next((c for c in ['名称', '股票简称'] if c in limit_df.columns), None)
    if not sector_cols or not lb_col or not name_col:
        return None, None
    mask = False
    for c in sector_cols:
        mask = mask | limit_df[c].astype(str).str.contains(sector_name, na=False)
    sub = limit_df[mask]
    if sub.empty:
        return None, None
    vals = pd.to_numeric(sub[lb_col], errors='coerce')
    if vals.notna().any():
        idx = vals.idxmax()
        return str(sub.loc[idx, name_col]), int(vals.loc[idx])
    return None, None


def classify_market(limit_stats, amount_stats, latest_capture):
    ctx = ase.classify_market_hard(limit_stats, amount_stats, latest_capture or {})
    return ctx['market_phase'], ctx['env']


def sector_stage(pct, turnover, inflow):
    pct = safe_float(pct) or 0
    turnover = safe_float(turnover) or 0
    inflow = safe_float(inflow)
    if pct >= 3 and turnover >= 3 and (inflow is None or inflow > 0):
        return '主升'
    if pct >= 1.5 and turnover >= 2 and (inflow is None or inflow >= 0):
        return '修复'
    if pct > 0:
        return '轮动'
    return '退潮'


def market_resonance(sector_pct, sh_pct, sz_pct):
    sector_pct = safe_float(sector_pct) or 0
    sh_pct = safe_float(sh_pct) or 0
    sz_pct = safe_float(sz_pct) or 0
    positive_index = max(sh_pct, sz_pct)
    if sector_pct >= 2 and positive_index > 0:
        return '是'
    if sector_pct >= 1 and positive_index >= -0.2:
        return '弱共振/结构性共振'
    return '弱共振/分化'


def infer_sector_leader(sector_name, constituents, sector_row):
    leader_name = sector_row.get('leader_name') or (sector_row.get('raw_board') or {}).get('领涨股票')
    leader_code = sector_row.get('leader_code') or normalize_code((sector_row.get('raw_board') or {}).get('领涨股票代码'))
    if leader_name and leader_code:
        return leader_name, leader_code, '板块快照领涨股'
    if constituents is not None and not constituents.empty:
        cons = constituents.copy()
        cons['score'] = cons['涨跌幅'].fillna(0) * 0.6 + (cons['成交额'].fillna(0) / 1e8) * 0.4
        best = cons.sort_values(['score', '涨跌幅', '成交额'], ascending=False).iloc[0]
        return str(best['名称']), str(best['代码']), '板块成分股综合得分'
    return '数据缺失', None, '数据缺失'


def infer_sector_catalyst(sector_name, inflow, pct_change):
    inflow = safe_float(inflow)
    pct_change = safe_float(pct_change)
    if inflow is not None and inflow > 0 and (pct_change or 0) >= 1:
        return '板块涨幅与资金净流入同步改善，盘中表现更像资金驱动的结构性强化；具体消息催化需继续核验'
    if (pct_change or 0) >= 1:
        return '板块本身走强，但资金与消息催化的确定性一般；更适合作为盘后核验方向'
    return '当前更像轮动或存量博弈，催化强度一般'


def choose_candidates(sectors, parsed_watchlist, code_to_sector=None, market_ctx=None):
    """候选池先合并，再做硬过滤与 A/B/C 分层。"""
    code_to_sector = code_to_sector or {}
    seeded = {}
    for item in parsed_watchlist:
        code = item.get('code')
        if code and is_main_board_code(code):
            cached = code_to_sector.get(code) or {}
            seeded[code] = {
                'sector': cached.get('sector_name') or item.get('sector') or '待补充',
                'code': code,
                'name': item.get('name') or code,
                'role': cached.get('role') or item.get('role') or '待判断',
                'stage': item.get('stage') or None,
                'from_watchlist': True,
            }
    result = list(seeded.values())
    for sector in sectors:
        cons = sector.get('constituents')
        if cons is None or cons.empty:
            continue
        shortlist = cons.sort_values('成交额', ascending=False).head(4)
        for _, row in shortlist.iterrows():
            code = str(row['代码'])
            if code in seeded:
                continue
            cached = code_to_sector.get(code) or {}
            db_role = cached.get('role')
            if db_role in ('龙头', '中军', '补涨'):
                role = db_role
            else:
                role = '中军' if (safe_float(row.get('成交额')) or 0) >= 2e9 else '补涨'
                if safe_float(row.get('涨跌幅')) and safe_float(row.get('涨跌幅')) >= 9.5:
                    role = '龙头'
            result.append({
                'sector': cached.get('sector_name') or sector['name'],
                'code': code,
                'name': str(row['名称']),
                'role': role,
                'stage': sector.get('stage') or sector_stage(sector.get('pct_change'), sector.get('turnover_rate'), sector.get('net_inflow')),
                'spot': row.to_dict(),
                'from_watchlist': False,
            })
    unique = {}
    for item in result:
        unique.setdefault(item['code'], item)
    filtered = list(unique.values())
    for item in filtered:
        if 'spot' not in item:
            item['spot'] = {}
            for sector in sectors:
                cons = sector.get('constituents')
                if cons is None or cons.empty:
                    continue
                sub = cons[cons['代码'].astype(str) == item['code']]
                if not sub.empty:
                    row = sub.iloc[0]
                    item['spot'] = row.to_dict()
                    if item.get('sector') in (None, '待补充'):
                        item['sector'] = sector['name']
                    if not item.get('stage'):
                        item['stage'] = sector.get('stage')
                    break
    filtered.sort(key=lambda x: safe_float((x.get('spot') or {}).get('成交额')) or 0, reverse=True)
    # 收盘摘要是 17:00/17:30 的交付关键路径，不能为了候选池扩展
    # 对过多股票逐只抓日线。最终正文最多展示 6 只，因此这里也只
    # 对前 6 只补日线指标；更完整的候选跟踪由 ashare-strategy-tracker-local
    # 后台任务维护。
    top_candidates = filtered[:6]
    for item in top_candidates:
        try:
            item['metrics'] = get_daily_metrics(item['code'])
        except Exception:
            item['metrics'] = None
        item['filter'] = ase.candidate_hard_filter(item, item.get('metrics') or {}, market_phase=(market_ctx or {}).get('market_phase'))
        item['tier'] = item['filter']['tier']
        item['rr_value'] = item['filter']['rr_value']
    top_candidates.sort(key=lambda x: (x.get('tier', 'C'), -(ase.stage_weight(x.get('stage')) + ase.role_weight(x.get('role'))), -(safe_float((x.get('spot') or {}).get('成交额')) or 0)), reverse=False)
    return top_candidates


def load_parsed_watchlist_from_latest_summary():
    path = ROOT / TODAY / 'latest-summary.md'
    if not path.exists():
        path = ROOT / TODAY / 'close-summary.md'
    if not path or not path.exists():
        return []
    items = []
    lines = path.read_text(encoding='utf-8').splitlines()
    for line in lines:
        line = line.strip()
        # latest-summary watchlist bullet format
        if line.startswith('- ') and '（' in line and '）' in line and '，' in line:
            # e.g. - 中国卫星（600118，candidate）：5.14% ...
            try:
                left = line[2:].split('）：', 1)[0] + '）'
                name = left.split('（', 1)[0].strip()
                inside = left.split('（', 1)[1].rstrip('）')
                parts = [p.strip() for p in inside.split('，')]
                code = normalize_code(parts[0])
                source_group = parts[1] if len(parts) > 1 else 'candidate'
                if code:
                    items.append({'code': code, 'name': name, 'source_group': source_group})
            except Exception:
                pass
        # close-summary candidate header format
        if line.startswith('### ') and '（' in line and '）' in line:
            try:
                body = line[4:]
                name = body.split('（', 1)[0].strip()
                code = normalize_code(body.split('（', 1)[1].rstrip('）'))
                if code:
                    items.append({'code': code, 'name': name, 'source_group': 'candidate'})
            except Exception:
                pass
    unique = {}
    for item in items:
        unique.setdefault(item['code'], item)
    return list(unique.values())


def build_close_summary_report_metadata(now=None, target_date=None, data_warnings=None, capture_info=None):
    """Build a small, testable data-time metadata block for the close summary report."""
    now_dt = now or datetime.now().astimezone()
    target = target_date or TODAY
    warnings = [str(w).strip() for w in (data_warnings or []) if str(w).strip()]
    completeness = '存在缺失/存在降级' if warnings else '正常'
    missing_note = '；'.join(warnings) if warnings else '无'
    snapshot_note = '使用目标交易日可获得的盘中/收盘快照'
    if capture_info:
        extras = []
        run_id = capture_info.get('run_id')
        captured_at = capture_info.get('captured_at')
        trade_date = capture_info.get('trade_date')
        if run_id is not None:
            extras.append(f'run_id={run_id}')
        if captured_at:
            extras.append(f'captured_at={captured_at}')
        if trade_date and str(trade_date) != str(target):
            extras.append(f'trade_date={trade_date}')
        if extras:
            snapshot_note += '；' + '；'.join(extras)
    return '\n'.join([
        f'> 数据日期：{target}',
        f'> 生成时间：{now_dt.strftime("%Y-%m-%d %H:%M:%S")}',
        '> 报告类型：收盘摘要',
        '> 数据阶段：收盘后数据',
        '> 行情日期要求：必须为当日收盘/盘中快照数据',
        '> 是否允许回退前一交易日行情：否',
        f'> 快照数据说明：{snapshot_note}',
        f'> 数据完整性：{completeness}',
        f'> 缺失说明：{missing_note}',
        '> 备注：本报告用于盘后复盘与候选股研究，不构成买卖建议',
    ])


def build_markdown(index_spot, amount_stats, limit_stats, latest_capture, sectors, candidates, intraday_watchlist, sector_tiers=None, scoreboard=None, now=None, target_date=None, data_warnings=None, capture_info=None):
    market_ctx = ase.classify_market_hard(limit_stats, amount_stats, latest_capture or {})
    style, env = market_ctx['market_phase'], market_ctx['env']
    sh_pct = safe_float(index_spot.get('上证指数', {}).get('pct'))
    sz_pct = safe_float(index_spot.get('深证成指', {}).get('pct'))
    report_date = target_date or TODAY
    lines = [f'# A股收盘摘要 - {report_date}', '']
    lines.extend(build_close_summary_report_metadata(
        now=now,
        target_date=report_date,
        data_warnings=data_warnings,
        capture_info=capture_info,
    ).splitlines())
    lines.append('')
    lines.append('## 1. 市场总览')
    for label in INDEX_LABEL_ORDER:
        if label in index_spot:
            item = index_spot[label]
            lines.append(f'- {label}：{fmt_pct(item.get("pct"))}，成交额 {fmt_yi(item.get("amount"))}')
    lines.append(
        f'- 大盘量能：两市合计约 {fmt_yi(amount_stats.get("today"))}，'
        f'较上一交易日{"放量" if (safe_float(amount_stats.get("delta")) or 0) >= 0 else "缩量"} '
        f'{fmt_yi(abs(safe_float(amount_stats.get("delta")) or 0))}，变化 {fmt_pct(amount_stats.get("delta_pct"))}'
    )
    if latest_capture:
        lines.append(f'- 涨跌分布：上涨 {latest_capture.get("up_count", "数据缺失")} 家，下跌 {latest_capture.get("down_count", "数据缺失")} 家，平盘 {latest_capture.get("flat_count", "数据缺失")} 家')
    lines.append(f'- 涨跌停数量：涨停 {limit_stats.get("zt_count", "数据缺失")} 家，跌停 {limit_stats.get("dt_count", "数据缺失")} 家')
    lines.append(f'- 连板情况：市场最高连板约 {limit_stats.get("max_lb", "数据缺失")} 板')
    lines.append(f'- 市场环境硬规则：{ " / ".join(market_ctx.get("hard_rules") or ["信号不足"]) }')
    lines.append(f'- 市场风格判断：{style}；环境：{env}；综合分 {market_ctx.get("score")}')
    lines.append('')

    lines.append('## 2. 板块分析')
    for s in sectors:
        br = s.get('board', {})
        fr = s.get('flow', {})
        pct_change = s.get('pct_change') if s.get('pct_change') is not None else br.get('涨跌幅')
        turnover = s.get('turnover_rate') if s.get('turnover_rate') is not None else br.get('换手率')
        inflow = s.get('net_inflow') if s.get('net_inflow') is not None else fr.get('今日主力净流入-净额')
        lb_name, lb_num = detect_sector_lb(limit_stats.get('zt_df'), s['name'])
        leader_name, leader_code, leader_basis = infer_sector_leader(s['name'], s.get('constituents'), s)
        resonance = market_resonance(pct_change, sh_pct, sz_pct)
        stage = sector_stage(pct_change, turnover, inflow)
        s['stage'] = stage
        lines.append(f'### {s["name"]}')
        lines.append(f'- 板块涨幅：{fmt_pct(pct_change)}')
        inflow_pct = s.get('net_inflow_pct') if s.get('net_inflow_pct') is not None else fr.get('今日主力净流入-净占比')
        lines.append(f'- 板块流入：主力净流入 {fmt_yi(inflow)}，净占比 {fmt_pct(inflow_pct)}')
        lines.append(f'- 板块量能：换手率 {fmt_pct(turnover)}，上涨家数 {int(s.get("up_count") or br.get("上涨家数") or 0)} / 下跌家数 {int(s.get("down_count") or br.get("下跌家数") or 0)}')
        lines.append(f'- 板块龙头：{leader_name}{f"（{leader_code}）" if leader_code else ""}')
        lines.append(f'- 板块内最高连板：{lb_name + "，约" + str(lb_num) + "连板" if lb_name else "数据缺失/未匹配到明确行业映射"}')
        lines.append(f'- 板块与大盘是否共振：{resonance}')
        lines.append(f'- 板块逻辑催化：{infer_sector_catalyst(s["name"], inflow, pct_change)}')
        lines.append(f'- 板块阶段：{stage}')
        lines.append('')

    if sector_tiers:
        tiered_sectors = [s for s in sectors if s['name'] in sector_tiers]
        if tiered_sectors:
            lines.append('## 2.1 板块梯队（来自盘中成分股缓存）')
            lines.append('- 梯队说明：龙头 = 板块综合最强领涨股；中军 = 成交额大、板块代表性强的票；补涨 = 跟随板块上涨但非龙头/中军。')
            for s in tiered_sectors:
                tier = sector_tiers.get(s['name']) or []
                role_map = {}
                for item in tier:
                    role_map.setdefault(item.get('role', '补涨'), []).append(item)
                for role_label in ['龙头', '中军', '补涨']:
                    for item in role_map.get(role_label, [])[:3]:
                        lines.append(f'- [{role_label}] {item.get("name")}（{item.get("code")}）：{fmt_pct(item.get("pct_change"))}，成交额 {fmt_yi(item.get("amount"))}')
                lines.append('')

    lines.append('## 3. 个股筛选（硬过滤后）')
    lines.append('- 候选仅保留主板股，并先经过四道硬约束：资金体量适配、盈亏比、流动性、板块阶段。')
    if intraday_watchlist:
        tracked = '、'.join([f"{i.get('name')}({i.get('code')})" for i in intraday_watchlist[:6]])
        lines.append(f'- 盘中已持续跟踪的观察池：{tracked}')
    lines.append('')

    grouped = {'A': [], 'B': [], 'C': []}
    for c in candidates:
        grouped.setdefault(c.get('tier') or 'C', []).append(c)

    lines.append('### A层：可执行池')
    if not grouped['A']:
        lines.append('- 今日无满足硬过滤的 A 层候选，次日计划默认以观察为主。')
    else:
        for c in grouped['A'][:4]:
            m = c.get('metrics') or {}
            spot = c.get('spot') or {}
            filt = c.get('filter') or {}
            entry_plan = ase.choose_recommended_entry(c, metrics=m)
            lines.append(f'#### {c["name"]}（{c["code"]}）')
            lines.append(f'- 所属板块：{c.get("sector") or "待补充"}；角色：{c.get("role") or "待判断"}；板块阶段：{c.get("stage") or "待确认"}')
            lines.append(f'- 趋势结构：MA5 {fmt_num(m.get("ma5"))} / MA10 {fmt_num(m.get("ma10"))} / MA20 {fmt_num(m.get("ma20"))} / MA60 {fmt_num(m.get("ma60"))}，60日位置 {fmt_num(m.get("pos_60d"))}%')
            lines.append(f'- 流动性与体量：成交额 {fmt_yi(spot.get("成交额") or m.get("amount"))}；一手资金占账户约 {fmt_num((filt.get("lot_ratio") or 0)*100)}%')
            lines.append(f'- 盈亏比：{fmt_num(filt.get("rr_value"))}；硬过滤摘要：{filt.get("summary")}')
            lines.append(f'- 模拟买点：{fmt_num(entry_plan.get("entry_ref"))}（区间 {fmt_num(entry_plan.get("entry_low"))} ~ {fmt_num(entry_plan.get("entry_high"))}；来源 {entry_plan.get("entry_source") or "数据缺失"}）')
            lines.append(f'- 明日只看：回踩承接、板块共振、量价同步，不满足即降级到观察池。')
            lines.append('')

    lines.append('### B层：观察池')
    if not grouped['B']:
        lines.append('- 无。')
    else:
        for c in grouped['B'][:4]:
            filt = c.get('filter') or {}
            entry_plan = ase.choose_recommended_entry(c, metrics=c.get('metrics') or {})
            lines.append(f'- {c["name"]}（{c["code"]}，{c.get("sector") or "待补充"}，{c.get("stage") or "待确认"}，{c.get("role") or "待判断"}）：{filt.get("summary")}；模拟买点 {fmt_num(entry_plan.get("entry_ref"))}（{fmt_num(entry_plan.get("entry_low"))}~{fmt_num(entry_plan.get("entry_high"))}）')

    lines.append('')
    lines.append('### C层：仅记录')
    if not grouped['C']:
        lines.append('- 无。')
    else:
        for c in grouped['C'][:6]:
            filt = c.get('filter') or {}
            lines.append(f'- {c["name"]}（{c["code"]}）：{ "、".join(filt.get("hard_fail") or [filt.get("summary")]) }')
    lines.append('')

    lines.append('## 4. 次日计划')
    a_candidates = grouped['A']
    focus_sectors = []
    for c in a_candidates:
        sec = c.get('sector')
        if sec and sec not in focus_sectors:
            focus_sectors.append(sec)
    focus_sectors = focus_sectors[:3] or [s['name'] for s in sectors[:3]]
    lines.append(f'- 明天看好的 3 个板块：{"、".join(focus_sectors) if focus_sectors else "数据缺失"}')
    for sec in focus_sectors[:3]:
        picks = [c for c in a_candidates if c.get('sector') == sec][:2]
        pick_text = '、'.join([f'{c["name"]}（{c["code"]}）' for c in picks]) if picks else '暂无 A 层备选，保留观察'
        lines.append(f'- {sec}：备选股 {pick_text}')
    lines.append('- 硬规则：RR < 1.5 不进 A 层；RR < 1 或一手成本超过账户总资金直接剔除。')
    lines.append('- 执行顺序：先环境，再板块，再 A 层个股；若没有 A 层，次日默认只观察。')
    lines.append('- 风险提示：轮动/退潮环境下，B/C 层不生成细致执行模板，避免假精细。')
    lines.append('')

    lines.append('## 4.1 近20交易日候选记分板')
    if scoreboard and scoreboard.get('rows'):
        lines.append(f'- 统计窗口：近 {scoreboard.get("window")} 个交易日（当前可用 {scoreboard.get("available_days")} 个交易日）')
        if scoreboard.get('overall_avg_ret_20d') is not None:
            lines.append(f'- 全部候选平均 20 日收益：{fmt_pct(scoreboard.get("overall_avg_ret_20d"))}')
        for row in scoreboard['rows']:
            stats = row.get('stats') or {}
            parts = []
            for horizon in [1, 3, 5, 10, 20]:
                stat = stats.get(horizon) or {}
                parts.append(f'{horizon}日胜率 {fmt_pct(stat.get("hit_rate"))} / 平均收益 {fmt_pct(stat.get("avg_ret"))}')
            parts.append(f'20日最佳波动均值 {fmt_pct(row.get("avg_best_ret_20"))}')
            parts.append(f'20日最差波动均值 {fmt_pct(row.get("avg_worst_ret_20"))}')
            lines.append(f'- {row["tier"]}层：样本 {row["count"]}，' + '，'.join(parts))
        detail_rows = scoreboard.get('detail_rows') or []
        tracked_rows = [item for item in detail_rows if (item.get('days_tracked') or 0) > 0 or item.get('current_ret') is not None]
        display_rows = tracked_rows[:12] if tracked_rows else detail_rows[:12]
        if display_rows:
            lines.append('- 个股跟踪明细（按入选日期倒序，模拟买点从次一交易日起算）：')
            for item in display_rows:
                lines.append(
                    f'  - {item.get("trade_date")} {item.get("name")}（{item.get("code")}，{item.get("tier")}层，{item.get("sector") or "待补充"}）：'
                    f'模拟买点 {fmt_num(item.get("entry_ref"))}，入场日 {item.get("entry_trade_date") or "待开盘"}，'
                    f'当前已跟踪 {item.get("days_tracked") or 0}/20 日，当前收益 {fmt_pct(item.get("current_ret"))}，'
                    f'20日内最佳 {fmt_pct(item.get("best_ret_20"))}，最差 {fmt_pct(item.get("worst_ret_20"))}。'
                )
    else:
        lines.append('- 暂无足够历史样本，后续会随交易日滚动补齐到 20 日跟踪。')
    lines.append('')

    lines.append('## 5. 自我复盘')
    lines.append('- 今天最好的交易：')
    lines.append('- 今天最差的交易：')
    lines.append('- 今天不该做的票：')
    lines.append('- 明天要避免的错误：')
    return '\n'.join(lines) + '\n'


def build_index_map(index_rows):
    out = {}
    for row in index_rows:
        out[str(row.get('index_name'))] = {
            'code': row.get('index_code'),
            'pct': safe_float(row.get('pct_change')),
            'amount': safe_float(row.get('amount')),
            'latest_value': safe_float(row.get('latest_value')),
        }
    return out


def build_sector_constituent_cache(sector_constituent_rows):
    """
    Build two structures from raw DB rows:
    - code_to_sector: {code -> {sector_name, role, name, ...}}
    - sector_tiers:   {sector_name -> [sorted constituent dicts by role hierarchy]}
    """
    code_to_sector = {}
    sector_tiers = {}
    for row in sector_constituent_rows:
        code = str(row.get('code') or '').strip()
        if not code:
            continue
        sector_name = str(row.get('sector_name') or '')
        raw = {}
        try:
            raw = json.loads(row.get('raw_json') or '{}')
        except Exception:
            pass
        item = {
            'code': code,
            'name': row.get('name'),
            'latest_price': safe_float(row.get('latest_price')),
            'pct_change': safe_float(row.get('pct_change')),
            'amount': safe_float(row.get('amount')),
            'turnover_rate': safe_float(row.get('turnover_rate')),
            'role': row.get('role') or '补涨',
            'is_sector_leader': bool(row.get('is_sector_leader')),
            'raw': raw,
            'sector_name': sector_name,
        }
        # 同一只股票可能同时出现在多个行业/概念成分缓存里。
        # code_to_sector 只保留首次映射用于个股反查；但 sector_tiers 必须
        # 保留每个板块自己的成分列表，否则后续会误判“该板块无缓存”，
        # 再去调用慢速 stock_board_industry_cons_em，导致收盘任务超时。
        code_to_sector.setdefault(code, item)
        sector_tiers.setdefault(sector_name, []).append(item)
    # Sort each sector's list: leader first, then by amount desc
    for sector_name in sector_tiers:
        sector_tiers[sector_name] = sorted(
            sector_tiers[sector_name],
            key=lambda x: (not x['is_sector_leader'], -(x['amount'] or 0)),
        )
    return code_to_sector, sector_tiers


def load_recent_sector_cache_rows(before_date=None):
    before_date = before_date or TODAY
    sector_rows = load_db_rows(
        "SELECT * FROM sector_snapshots WHERE trade_date < ? ORDER BY trade_date DESC, captured_at DESC, id DESC LIMIT 600",
        (before_date,),
    )
    constituent_rows = load_db_rows(
        "SELECT * FROM sector_constituent_snapshots WHERE trade_date < ? ORDER BY trade_date DESC, captured_at DESC, id DESC LIMIT 12000",
        (before_date,),
    )
    return sector_rows, constituent_rows


def infer_sector_rows_from_cache(run_id, limit=5):
    stock_rows = load_db_rows(
        "SELECT code, name, pct_change, amount, turnover_rate FROM stock_snapshots WHERE run_id = ? ORDER BY amount DESC",
        (run_id,),
    )
    if not stock_rows:
        return [], [], {}
    recent_sector_rows, cached_constituent_rows = load_recent_sector_cache_rows(before_date=TODAY)
    code_to_sector, sector_tiers = build_sector_constituent_cache(cached_constituent_rows)
    if not code_to_sector:
        return [], [], {}
    sector_meta = {}
    for row in recent_sector_rows:
        sector_name = str(row.get('sector_name') or '').strip()
        if not sector_name or sector_name in sector_meta:
            continue
        raw_board = {}
        raw_flow = {}
        try:
            raw = json.loads(row.get('raw_json') or '{}')
            if isinstance(raw, dict):
                raw_board = raw.get('raw') or raw
                raw_flow = raw.get('flow_raw') or {}
        except Exception:
            pass
        sector_meta[sector_name] = {
            'net_inflow': safe_float(row.get('net_inflow')),
            'net_inflow_pct': safe_float(row.get('net_inflow_pct')) or safe_float(raw_flow.get('今日主力净流入-净占比')),
            'turnover_rate': safe_float(row.get('turnover_rate')) or safe_float(raw_board.get('换手率')),
            'raw_board': raw_board,
            'raw_flow': raw_flow,
        }
    work = []
    for row in stock_rows:
        code = normalize_code(row.get('code'))
        ctx = code_to_sector.get(code)
        if not code or not ctx or not is_main_board_code(code):
            continue
        work.append({
            'code': code,
            'name': row.get('name'),
            'pct_change': safe_float(row.get('pct_change')),
            'amount': safe_float(row.get('amount')),
            'turnover_rate': safe_float(row.get('turnover_rate')),
            'sector_name': ctx.get('sector_name'),
            'role': ctx.get('role'),
        })
    if not work:
        return [], cached_constituent_rows, code_to_sector
    df = pd.DataFrame(work)
    ranked = []
    for sector_name, sub in df.groupby('sector_name'):
        sub = sub.sort_values(['pct_change', 'amount'], ascending=False)
        leader = sub.iloc[0]
        up = int((sub['pct_change'].fillna(0) > 0).sum())
        down = int((sub['pct_change'].fillna(0) < 0).sum())
        pct = safe_float(sub['pct_change'].head(5).mean()) or 0
        turnover = safe_float(sub['turnover_rate'].head(5).mean())
        score = pct * 5 + (up - down) * 0.8 + min((safe_float(sub['amount'].sum()) or 0) / 1e8, 25)
        meta = sector_meta.get(sector_name) or {}
        ranked.append((score, {
            'sector_name': sector_name,
            'pct_change': pct,
            'up_count': up,
            'down_count': down,
            'leader_name': leader.get('name'),
            'leader_code': leader.get('code'),
            'net_inflow': meta.get('net_inflow'),
            'net_inflow_pct': meta.get('net_inflow_pct'),
            'turnover_rate': turnover if turnover is not None else meta.get('turnover_rate'),
            'raw_board': {**(meta.get('raw_board') or {}), 'source': 'stock_snapshot_grouped_from_cached_sector', '换手率': turnover if turnover is not None else meta.get('turnover_rate')},
            'raw_flow': meta.get('raw_flow') or {},
        }))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:limit]], cached_constituent_rows, code_to_sector


def merge_sector_rows(db_sector_rows, fallback_rows):
    if db_sector_rows:
        sectors = []
        for row in db_sector_rows:
            raw_board = {}
            raw_flow = {}
            try:
                raw = json.loads(row.get('raw_json') or '{}')
                if isinstance(raw, dict):
                    raw_board = raw.get('raw') or raw
                    raw_flow = raw.get('flow_raw') or {}
            except Exception:
                pass
            sectors.append({
                'name': row.get('sector_name'),
                'pct_change': safe_float(row.get('pct_change')),
                'up_count': row.get('up_count'),
                'down_count': row.get('down_count'),
                'leader_name': row.get('leader_name'),
                'leader_code': normalize_code(row.get('leader_code')),
                'net_inflow': safe_float(row.get('net_inflow')),
                'net_inflow_pct': safe_float(row.get('net_inflow_pct')) or safe_float(raw_flow.get('今日主力净流入-净占比')),
                'turnover_rate': safe_float(row.get('turnover_rate')) or safe_float(raw_board.get('换手率')),
                'board': raw_board,
                'flow': raw_flow,
            })
        return sectors
    return [
        {
            'name': r.get('sector_name'),
            'pct_change': r.get('pct_change'),
            'up_count': r.get('up_count'),
            'down_count': r.get('down_count'),
            'leader_name': r.get('leader_name'),
            'leader_code': r.get('leader_code'),
            'net_inflow': r.get('net_inflow'),
            'net_inflow_pct': r.get('net_inflow_pct'),
            'turnover_rate': r.get('turnover_rate'),
            'board': r.get('raw_board') or {},
            'flow': r.get('raw_flow') or {},
        }
        for r in fallback_rows
    ]


def sector_tier_to_constituents(tier_rows):
    if not tier_rows:
        return pd.DataFrame(columns=['代码', '名称', '最新价', '涨跌幅', '成交额', '换手率'])
    rows = []
    for item in tier_rows:
        code = normalize_code(item.get('code'))
        if not code or not is_main_board_code(code):
            continue
        rows.append({
            '代码': code,
            '名称': item.get('name'),
            '最新价': safe_float(item.get('latest_price')),
            '涨跌幅': safe_float(item.get('pct_change')),
            '成交额': safe_float(item.get('amount')),
            '换手率': safe_float(item.get('turnover_rate')),
        })
    return pd.DataFrame(rows, columns=['代码', '名称', '最新价', '涨跌幅', '成交额', '换手率'])


def main():
    target_date = TODAY
    asof_time = datetime.now().astimezone()
    if adu.skip_cron_if_not_a_share_trading_day(target_date, task='ashare-close-summary-feishu'):
        return
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    latest_capture = read_latest_capture(target_date=target_date, asof_time=asof_time) or {}
    if not latest_capture:
        raise RuntimeError('No latest capture found in ashare_monitor.db for today')

    run_id = latest_capture.get('id')
    db_index_rows, db_sector_rows, db_watchlist_rows, db_sector_constituent_rows = load_snapshot_layers(run_id, target_date=target_date)
    if not db_index_rows:
        db_index_rows = load_latest_layer_rows('index_snapshots', target_date, 'id ASC', asof_time=asof_time)
    if not db_sector_rows:
        db_sector_rows = load_latest_layer_rows('sector_snapshots', target_date, 'id ASC', asof_time=asof_time)
    if not db_watchlist_rows:
        db_watchlist_rows = load_latest_layer_rows('watchlist_snapshots', target_date, 'id ASC', asof_time=asof_time)
    if not db_sector_constituent_rows:
        db_sector_constituent_rows = load_latest_layer_rows('sector_constituent_snapshots', target_date, 'sector_name ASC, is_sector_leader DESC, amount DESC', asof_time=asof_time)

    latest_date, prev_date = get_trade_dates(target_date=target_date)
    today_yyyymmdd = latest_date.replace('-', '')
    prev_yyyymmdd = prev_date.replace('-', '')
    amount_stats = get_exchange_amounts(today_yyyymmdd, prev_yyyymmdd, asof_time=asof_time)
    limit_stats = get_limit_stats(today_yyyymmdd, latest_capture=latest_capture, run_id=run_id, target_date=target_date)

    if db_index_rows:
        index_spot = build_index_map(db_index_rows)
    else:
        # fallback only if DB layer missing
        index_spot = build_index_map([])
        mapping = {'sh000001': '上证指数', 'sz399001': '深证成指', 'sz399006': '创业板指', 'sh000688': '科创50', 'sh000300': '沪深300', 'sh000852': '中证1000'}
        try:
            spot_df = adu.ak_call(ak.stock_zh_index_spot_sina, timeout=10, attempts=2)
            spot_df['代码'] = spot_df['代码'].astype(str)
            for code, label in mapping.items():
                sub = spot_df[spot_df['代码'] == code]
                if not sub.empty:
                    rec = sub.iloc[0].to_dict()
                    index_spot[label] = {'code': code, 'pct': safe_float(rec.get('涨跌幅')), 'amount': safe_float(rec.get('成交额'))}
        except Exception:
            items, _ = adu.fetch_index_quotes(mapping)
            for item in items:
                index_spot[item['index_name']] = {
                    'code': item['index_code'],
                    'pct': safe_float(item.get('pct_change')),
                    'amount': safe_float(item.get('amount')),
                    'latest_value': safe_float(item.get('latest_value')),
                }

    if not db_sector_constituent_rows:
        inferred_sector_rows, cached_constituent_rows, inferred_code_map = infer_sector_rows_from_cache(run_id, limit=5)
        if cached_constituent_rows:
            db_sector_constituent_rows = cached_constituent_rows
    else:
        inferred_sector_rows, cached_constituent_rows, inferred_code_map = [], [], {}

    # Build sector constituent cache from DB first so we can reuse the cached
    #盘中成分股层，避免收盘摘要阶段再做一轮重型行业成分抓取。
    code_to_sector, sector_tiers = build_sector_constituent_cache(db_sector_constituent_rows)
    for code, ctx in inferred_code_map.items():
        code_to_sector.setdefault(code, ctx)

    fallback_sectors = []
    if not db_sector_rows:
        fallback_sectors = get_board_data_fallback(limit=5)
        if not fallback_sectors:
            fallback_sectors = inferred_sector_rows
    sectors = merge_sector_rows(db_sector_rows, fallback_sectors)
    for sector in sectors:
        cached_tier = sector_tiers.get(sector['name']) or []
        if cached_tier:
            sector['constituents'] = sector_tier_to_constituents(cached_tier)
            continue
        try:
            sector['constituents'] = get_sector_constituents(sector['name'])
        except Exception:
            sector['constituents'] = pd.DataFrame()

    parsed_watchlist = load_parsed_watchlist_from_latest_summary()
    if db_watchlist_rows:
        intraday_watchlist = [
            {
                'code': normalize_code(r.get('code')),
                'name': r.get('name'),
                'source_group': r.get('source_group'),
                'latest_price': safe_float(r.get('latest_price')),
                'pct_change': safe_float(r.get('pct_change')),
                'volume_ratio': safe_float(r.get('volume_ratio')),
                'turnover_rate': safe_float(r.get('turnover_rate')),
                'intraday_note': r.get('intraday_note'),
            }
            for r in db_watchlist_rows
        ]
    else:
        intraday_watchlist = parsed_watchlist

    market_ctx = ase.classify_market_hard(limit_stats, amount_stats, latest_capture or {})
    candidates = choose_candidates(sectors, parsed_watchlist, code_to_sector, market_ctx=market_ctx)
    # 候选跟踪维护已拆到独立 cron：ashare-strategy-tracker-local。
    # 这里若再 run_tracking_maintenance，会重复调用大量个股日线接口，
    # 是 ashare-close-summary-feishu 经常超过 cron script timeout 的主因。
    tracking_maintenance = {'skipped': True, 'reason': 'handled_by_ashare_strategy_tracker_local'}
    ase.record_candidates(TODAY, candidates, market_ctx=market_ctx)
    scoreboard = ase.recent_scoreboard(window=20, refresh=False)
    markdown = build_markdown(
        index_spot,
        amount_stats,
        limit_stats,
        latest_capture,
        sectors,
        candidates,
        intraday_watchlist,
        sector_tiers,
        scoreboard=scoreboard,
        target_date=TODAY,
        capture_info=latest_capture,
    )
    if ledger_lib is not None:
        try:
            appendix = ledger_lib.build_close_summary_appendix(TODAY)
            if appendix:
                markdown = markdown.rstrip() + '\n\n' + appendix + '\n'
        except Exception:
            pass
    CLOSE_SUMMARY.write_text(markdown, encoding='utf-8')
    market_style, strategy_environment = classify_market(limit_stats, amount_stats, latest_capture)
    context = {
        'ok': True,
        'trade_date': TODAY,
        'close_summary_path': str(CLOSE_SUMMARY),
        'context_json_path': str(CONTEXT_JSON),
        'market_style': market_style,
        'strategy_environment': strategy_environment,
        'top_sectors': [s['name'] for s in sectors[:3]],
        'candidate_stocks': [f"{c['name']}({c['code']})" for c in candidates[:6]],
        'source_run_id': run_id,
        'used_db_layers': {
            'capture_runs': True,
            'index_snapshots': bool(db_index_rows),
            'sector_snapshots': bool(db_sector_rows),
            'watchlist_snapshots': bool(db_watchlist_rows),
            'sector_constituent_snapshots': bool(db_sector_constituent_rows),
        },
        'sector_tiers_available': bool(sector_tiers),
        'tracking_maintenance': tracking_maintenance,
        'notes': 'close-summary.md 已由脚本生成；优先使用盘中数据库快照，再补必要的收盘口径。板块梯队来自 sector_constituent_snapshots；候选跟踪库已按次一交易日模拟买点维护到 20 日窗口。',
    }
    CONTEXT_JSON.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(context, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
