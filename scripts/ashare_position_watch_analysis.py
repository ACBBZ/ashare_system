#!/usr/bin/env python3
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

import ashare_data_utils as adu

try:
    import ashare_ledger_lib as ledger_lib
except Exception:
    ledger_lib = None

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
NOW_DATE = datetime.now().astimezone().date().isoformat()
DB_PATH = ROOT / 'ashare_monitor.db'
TODAY_TS = pd.Timestamp(NOW_DATE)


def _read_text(path: Path | None) -> str:
    if not path or not Path(path).exists():
        return ''
    return Path(path).read_text(encoding='utf-8')


def current_day_dir() -> Path:
    return ROOT / NOW_DATE


def current_day_has_close_summary() -> bool:
    day_dir = current_day_dir()
    for name in ('close-summary.md', 'latest-summary.md'):
        path = day_dir / name
        if path.exists() and path.stat().st_size > 0:
            return True
    return False


def preferred_analysis_day_dir() -> Path | None:
    day_dir = current_day_dir()
    if current_day_has_close_summary():
        return day_dir
    return None


def prior_review_day_dirs(limit=3):
    dirs = adu.list_review_day_dirs(ROOT, before_date=NOW_DATE)
    return list(reversed(dirs[-limit:]))


def validate_review_source(path: Path | None, expected_day_dir: Path | None, label: str):
    expected_day = expected_day_dir.name if expected_day_dir else None
    actual_day = path.parent.name if path else None
    return {
        'label': label,
        'expected_day': expected_day,
        'actual_day': actual_day,
        'is_expected_day': bool(path and expected_day and actual_day == expected_day),
        'used_fallback': bool(path and expected_day and actual_day != expected_day),
        'path': str(path) if path else None,
    }


def is_main_board_code(code: str) -> bool:
    code = str(code).strip()
    if code.startswith(('688', '689', '300', '301', '8', '4')):
        return False
    return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003'))


def is_fund_like(name: str, code: str | None = None) -> bool:
    name = str(name or '')
    code = str(code or '')
    return ('ETF' in name.upper()) or ('LOF' in name.upper()) or code.startswith(('15', '16', '50', '51', '56', '58'))


def _db_rows(query, params=()):
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def find_latest_run_id():
    rows = _db_rows(
        "SELECT id FROM capture_runs WHERE trade_date = ? ORDER BY captured_at DESC LIMIT 1",
        (NOW_DATE,),
    )
    if not rows:
        # fall back to most recent capture run
        rows = _db_rows("SELECT id FROM capture_runs ORDER BY captured_at DESC LIMIT 1")
    return rows[0]['id'] if rows else None


def find_latest_run_id_with_sector_constituents():
    """Prefer the newest capture run that actually wrote sector constituent rows."""
    rows = _db_rows(
        """
        SELECT c.id
        FROM capture_runs c
        WHERE c.trade_date = ?
          AND EXISTS (
              SELECT 1 FROM sector_constituent_snapshots s
              WHERE s.run_id = c.id
          )
        ORDER BY c.captured_at DESC
        LIMIT 1
        """,
        (NOW_DATE,),
    )
    if not rows:
        rows = _db_rows(
            """
            SELECT c.id
            FROM capture_runs c
            WHERE EXISTS (
                SELECT 1 FROM sector_constituent_snapshots s
                WHERE s.run_id = c.id
            )
            ORDER BY c.captured_at DESC
            LIMIT 1
            """
        )
    return rows[0]['id'] if rows else None


def load_sector_constituent_cache():
    """
    Returns (code_to_sector, sector_tiers, sector_stage_map)
    built from the latest sector_constituent_snapshots in DB.

    code_to_sector : {code -> {sector_name, role, pct_change, amount, name, ...}}
    sector_tiers   : {sector_name -> [sorted members: leader > zhongjun > buzhang]}
    sector_stage_map: {sector_name -> stage_label}
    """
    run_id = find_latest_run_id_with_sector_constituents() or find_latest_run_id()
    if not run_id:
        return {}, {}, {}
    try:
        rows = _db_rows(
            "SELECT * FROM sector_constituent_snapshots WHERE run_id = ? ORDER BY sector_name ASC, is_sector_leader DESC, amount DESC",
            (run_id,),
        )
    except sqlite3.OperationalError:
        rows = []
    code_to_sector = {}
    sector_tiers = {}
    for row in rows:
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
            'sector_name': sector_name,
            'latest_price': _safe_float(row.get('latest_price')),
            'pct_change': _safe_float(row.get('pct_change')),
            'amount': _safe_float(row.get('amount')),
            'turnover_rate': _safe_float(row.get('turnover_rate')),
            'role': row.get('role') or '补涨',
            'is_sector_leader': bool(row.get('is_sector_leader')),
            'raw': raw,
        }
        code_to_sector[code] = item
        sector_tiers.setdefault(sector_name, []).append(item)
    # Sort tiers: leader first, then by amount desc
    for sn in sector_tiers:
        sector_tiers[sn] = sorted(
            sector_tiers[sn],
            key=lambda x: (not x['is_sector_leader'], -(x['amount'] or 0)),
        )
    # Build sector_stage_map from sector_snapshots
    stage_map = {}
    sector_rows = _db_rows(
        "SELECT sector_name, pct_change, up_count, down_count, net_inflow, raw_json FROM sector_snapshots WHERE run_id = ?",
        (run_id,),
    )
    for row in sector_rows:
        sn = row.get('sector_name') or ''
        raw = {}
        try:
            raw = json.loads(row.get('raw_json') or '{}')
        except Exception:
            pass
        turnover = _safe_float(raw.get('换手率'))
        stage_map[sn] = _infer_stage(
            _safe_float(row.get('pct_change')),
            turnover,
            _safe_float(row.get('net_inflow')),
        )
    return code_to_sector, sector_tiers, stage_map


def _safe_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return None
        return f
    except Exception:
        return None


def _infer_stage(pct, turnover, inflow):
    pct = pct or 0
    turnover = turnover or 0
    inflow = inflow or 0
    if pct >= 3 and turnover >= 3 and inflow >= 0:
        return '主升'
    if pct >= 1.5 and turnover >= 2 and inflow >= 0:
        return '修复'
    if pct > 0:
        return '轮动'
    return '退潮'


def parse_close_summary_sector_tiers(text: str):
    """Fallback parser for `## 2.1 板块梯队` in close-summary markdown."""
    section_m = re.search(r'## 2\.1 板块梯队（来自盘中成分股缓存）\n(.*?)(\n## 3\.|\Z)', text, flags=re.S)
    if not section_m:
        return {}, {}, {}
    block = section_m.group(1)
    sector_tiers = {}
    code_to_sector = {}
    sector_stage_map = {}
    current_sector = None
    current_stage = None

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            current_sector = None
            current_stage = None
            continue
        m_sector = re.match(r'^###\s+(.+?)（(.+?)）$', line)
        if m_sector:
            current_sector = m_sector.group(1).strip()
            current_stage = m_sector.group(2).strip()
            sector_stage_map[current_sector] = current_stage
            sector_tiers.setdefault(current_sector, [])
            continue
        m_item = re.match(r'^- \[(龙头|中军|补涨)\]\s+(.+?)（(\d{6})）[:：]([^，]+)，成交额\s+(.+)$', line)
        if not m_item or not current_sector:
            continue
        role, name, code, pct_text, amt_text = m_item.groups()
        item = {
            'code': code,
            'name': name.strip(),
            'latest_price': None,
            'pct_change': _safe_float(str(pct_text).replace('%', '').replace('+', '')),
            'amount': _safe_float(str(amt_text).replace('亿', '')),
            'turnover_rate': None,
            'role': role,
            'is_sector_leader': role == '龙头',
            'raw': {},
        }
        sector_tiers.setdefault(current_sector, []).append(item)
        code_to_sector[code] = {**item, 'sector_name': current_sector}

    return code_to_sector, sector_tiers, sector_stage_map


def find_latest_close_summary() -> Path:
    day_dir = preferred_analysis_day_dir()
    if not day_dir:
        raise FileNotFoundError(f'No same-day close summary found under {current_day_dir()}')
    path = adu.pick_first_existing(day_dir / 'close-summary.md', day_dir / 'latest-summary.md')
    if not path:
        raise FileNotFoundError(f'No same-day close summary found under {day_dir}')
    return path


def find_latest_holding_summary() -> Path | None:
    day_dir = preferred_analysis_day_dir()
    if not day_dir:
        return None
    picked = adu.pick_first_existing(day_dir / 'holding-pnl-1505.md', day_dir / 'close-summary.md', day_dir / 'latest-summary.md')
    if picked:
        return picked
    analysis_files = [p for p in sorted(day_dir.glob('持仓股与候选股分析-*.md')) if p.name != f'持仓股与候选股分析-{NOW_DATE}.md']
    if analysis_files:
        return analysis_files[-1]
    return None


def parse_candidates(text: str):
    items = []
    seen = set()

    def add_item(name: str, code: str | None):
        code = str(code or '').strip()
        if not code or not is_main_board_code(code) or code in seen:
            return
        items.append({'name': str(name).strip(), 'code': code, 'group': '候选股', 'asset_type': 'stock'})
        seen.add(code)

    m = re.search(r'## 3\. 个股筛选（(?:最重要|硬过滤后)）\n(.+?)\n## 4\.', text, flags=re.S)
    if m:
        block = m.group(1)
        entries = re.split(r'\n### ', '\n' + block)
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            lines = entry.splitlines()
            header = lines[0].replace('### ', '').strip()
            mm = re.match(r'([\u4e00-\u9fa5A-Za-z0-9]+)（(\d{6})）', header)
            if mm:
                add_item(mm.group(1), mm.group(2))
            else:
                mm = re.match(r'-\s*([\u4e00-\u9fa5A-Za-z0-9]+)（(\d{6})[，,）]', header)
                if mm:
                    add_item(mm.group(1), mm.group(2))
            for line in lines[1:]:
                bullet = line.strip()
                mm = re.match(r'-\s*([\u4e00-\u9fa5A-Za-z0-9]+)（(\d{6})[，,）]', bullet)
                if mm:
                    add_item(mm.group(1), mm.group(2))

    for name, code in re.findall(r'^##\s+候选：([^（\n]+)（(\d{6})）', text, flags=re.M):
        add_item(name, code)

    return items


def parse_holdings(text: str):
    holdings = []
    m = re.search(r'当前持仓记录：(.+)', text)
    if not m:
        return holdings
    part = m.group(1)
    pattern = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)\s*([0-9]+成)（成本\s*([0-9.]+)，\s*([0-9]+)股）')
    for name, weight, cost, shares in pattern.findall(part):
        holdings.append({
            'name': name.strip(),
            'cost': float(cost),
            'shares': int(shares),
            'weight': weight,
        })
    return holdings


def load_holdings_from_ledger():
    if ledger_lib is None:
        return [], None, None, None
    report = None
    rows = []
    report_trade_date = None
    try:
        report = ledger_lib.latest_report_summary(NOW_DATE)
    except Exception:
        report = None
    if report:
        report_trade_date = report.get('trade_date') or NOW_DATE
    if report_trade_date != NOW_DATE:
        report = None
        report_trade_date = None
    try:
        rows = ledger_lib.load_snapshot_rows(NOW_DATE)
    except Exception:
        rows = []
    holdings = []
    for row in rows:
        holdings.append({
            'name': row.get('name'),
            'code': str(row.get('symbol') or '').zfill(6),
            'cost': float(row.get('avg_cost') or 0),
            'shares': int(row.get('quantity') or 0),
            'weight': row.get('weight_text') or '未知仓位',
            'asset_type': row.get('asset_type') or ('fund' if is_fund_like(row.get('name'), row.get('symbol')) else 'stock'),
        })
    holding_source = report.get('note_path') if report else None
    holdings_line = report.get('summary', {}).get('holdings_line') if report else None
    if holdings_line and '当前持仓记录：无' in str(holdings_line):
        return [], holding_source, holdings_line, report_trade_date
    return holdings, holding_source, holdings_line, report_trade_date


def parse_board_stage_map(text: str):
    out = {}
    m = re.search(r'## 2\. 板块分析\n(.+?)\n## 3\.', text, flags=re.S)
    if not m:
        return out
    block = m.group(1)
    pattern = re.compile(r'###\s+(.+?)\n(.*?)(?=\n###\s+|\Z)', flags=re.S)
    for sector, body in pattern.findall(block):
        sector = sector.strip()
        mm = re.search(r'板块是主升、分歧、修复还是退潮：(.+)', body)
        if not mm:
            mm = re.search(r'板块阶段：(.+)', body)
        out[sector] = mm.group(1).strip() if mm else None
    return out


def parse_candidate_sector_map(text: str):
    out = {}
    m = re.search(r'## 3\. 个股筛选（(?:最重要|硬过滤后)）\n(.+?)\n## 4\.', text, flags=re.S)
    if not m:
        return out
    block = m.group(1)
    parts = re.split(r'\n### ', block)
    for sec in parts:
        sec = sec.strip()
        if not sec:
            continue
        lines = sec.splitlines()
        mm = re.match(r'([\u4e00-\u9fa5A-Za-z]+)（(\d{6})）', lines[0].strip())
        if mm:
            code = mm.group(2)
            sector = None
            for line in lines[1:]:
                m2 = re.search(r'所属板块：(.+)', line)
                if m2:
                    sector = m2.group(1).strip().split('；', 1)[0].split('/', 1)[0].strip()
                    break
            out[code] = sector
        for line in lines:
            bullet = line.strip()
            m3 = re.match(r'-\s*([\u4e00-\u9fa5A-Za-z0-9]+)（(\d{6})[，,]\s*([^，,）]+)', bullet)
            if m3:
                out[m3.group(2)] = m3.group(3).strip()
    return out


def load_recent_review_context(limit=3):
    rows = []
    for day_dir in prior_review_day_dirs(limit=limit):
        close_path = adu.pick_first_existing(day_dir / 'close-summary.md', day_dir / 'latest-summary.md')
        if not close_path:
            continue
        text = _read_text(close_path)
        if not text:
            continue
        rows.append({
            'trade_date': day_dir.name,
            'path': str(close_path),
            'candidates': parse_candidates(text),
            'sector_map': parse_candidate_sector_map(text),
            'stage_map': parse_board_stage_map(text),
        })
    return rows


def build_candidate_history(recent_rows):
    history = {}
    for row in recent_rows:
        trade_date = row.get('trade_date')
        sector_map = row.get('sector_map') or {}
        stage_map = row.get('stage_map') or {}
        for item in row.get('candidates') or []:
            code = str(item.get('code') or '').strip()
            if not code:
                continue
            bucket = history.setdefault(code, {
                'name': item.get('name'),
                'count': 0,
                'days': [],
                'sectors': [],
                'stages': [],
            })
            bucket['count'] += 1
            if trade_date and trade_date not in bucket['days']:
                bucket['days'].append(trade_date)
            sector = sector_map.get(code)
            if sector and sector not in bucket['sectors']:
                bucket['sectors'].append(sector)
            stage = stage_map.get(sector) if sector else None
            if stage and stage not in bucket['stages']:
                bucket['stages'].append(stage)
    return history


def candidate_strength_score(item: dict, analysis: dict | None = None):
    hist = item.get('recent_history') or {}
    score = 0
    reasons = []
    count = int(hist.get('count') or 0)
    if count:
        score += min(count * 15, 45)
        reasons.append(f'近3日出现{count}次')
    days = hist.get('days') or []
    if len(days) >= 2:
        score += 14
        reasons.append('多日连续跟踪')
    if len(days) >= 3:
        score += 10
        reasons.append('连续3日都在复盘池')
    if item.get('from_recent_reviews'):
        score -= 12
        reasons.append('今天未入主候选，仅作多日补充')
    stage = item.get('board_stage')
    if stage == '主升':
        score += 26
        reasons.append('板块主升')
    elif stage == '修复':
        score += 16
        reasons.append('板块修复')
    elif stage == '轮动':
        score += 4
        reasons.append('板块轮动')
    elif stage == '退潮':
        score -= 18
        reasons.append('板块退潮')
    role = item.get('role')
    if role == '龙头':
        score += 22
        reasons.append('板块龙头')
    elif role == '中军':
        score += 12
        reasons.append('板块中军')
    elif role == '补涨':
        score += 3
        reasons.append('板块补涨')
    if analysis:
        rr = analysis.get('rr')
        if rr is not None:
            if rr >= 2.2:
                score += 16
                reasons.append('盈亏比>=2.2')
            elif rr >= 1.5:
                score += 10
                reasons.append('盈亏比>=1.5')
            elif rr >= 1.2:
                score += 4
                reasons.append('盈亏比尚可')
            else:
                score -= 12
                reasons.append('盈亏比偏弱')
        direction = analysis.get('tomorrow_direction') or ''
        if '偏强' in direction:
            score += 12
            reasons.append('明日偏强')
        elif '区间' in direction:
            score += 3
            reasons.append('明日区间')
        elif '下行' in direction:
            score -= 12
            reasons.append('明日偏弱')
        trend = analysis.get('trend') or ''
        if '多头趋势' in trend:
            score += 10
            reasons.append('多头趋势')
        elif '空头趋势' in trend:
            score -= 10
            reasons.append('空头趋势')
        change_pct = analysis.get('change_pct')
        if change_pct is not None:
            if change_pct >= 5:
                score += 8
                reasons.append('当日强势收盘')
            elif change_pct <= -3:
                score -= 8
                reasons.append('当日明显走弱')
    if score >= 70:
        level = 'S'
    elif score >= 55:
        level = 'A'
    elif score >= 40:
        level = 'B'
    else:
        level = 'C'
    return {'score': score, 'level': level, 'reasons': reasons}


def candidate_action_tag(item: dict, analysis: dict | None = None):
    if not analysis:
        return '只观察'
    stage = item.get('board_stage')
    rr = analysis.get('rr')
    direction = analysis.get('tomorrow_direction') or ''
    change_pct = analysis.get('change_pct')
    if stage == '退潮' or '下行' in direction:
        return '高开先放弃'
    if rr is not None and rr < 1:
        return '只观察不追'
    if stage in ('主升', '修复') and '偏强' in direction:
        if change_pct is not None and change_pct >= 5:
            return '只等回踩试错'
        return '弱转强才看'
    if '区间' in direction:
        return '靠近支撑再看'
    return '只观察'


def resolve_codes(holdings):
    mapping = {}
    stock_names = [x['name'] for x in holdings if not is_fund_like(x['name'])]
    fund_names = [x['name'] for x in holdings if is_fund_like(x['name'])]
    if stock_names:
        try:
            stock_df = adu.ak_call(ak.stock_info_a_code_name, timeout=20, attempts=3)
            for name in stock_names:
                sub = stock_df[stock_df['name'] == name]
                if not sub.empty:
                    mapping[name] = (str(sub.iloc[0]['code']).zfill(6), 'stock')
        except Exception:
            pass
    if fund_names:
        try:
            fund_df = adu.ak_call(ak.fund_name_em, timeout=20, attempts=3)
            for name in fund_names:
                query = name.replace('（', '(').replace('）', ')').replace(' ', '')
                base = re.sub(r'(ETF|LOF|A|C|\(|\)|期货)', '', query, flags=re.I)
                sub = fund_df[fund_df['基金简称'].astype(str).str.contains(query, na=False)]
                if sub.empty and base:
                    sub = fund_df[fund_df['基金简称'].astype(str).str.contains(base, na=False)]
                if not sub.empty:
                    sub = sub.copy()
                    sub['__score'] = 0
                    if '基金简称' in sub.columns:
                        sub.loc[sub['基金简称'].astype(str).str.contains('LOF', na=False), '__score'] += 3
                        sub.loc[sub['基金简称'].astype(str).str.contains('A', na=False), '__score'] += 2
                        sub.loc[sub['基金简称'].astype(str).str.contains('C', na=False), '__score'] -= 1
                    sub = sub.sort_values(['__score', '基金代码'], ascending=[False, True])
                    row = sub.iloc[0]
                    mapping[name] = (str(row['基金代码']).zfill(6), 'fund')
        except Exception:
            pass
    return mapping


def enrich(df):
    df = df.copy().sort_values('date').reset_index(drop=True)
    for w in [5, 10, 20, 60, 120]:
        df[f'ma{w}'] = df['close'].rolling(w).mean()
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    rs = up.rolling(14).mean() / down.rolling(14).mean().replace(0, np.nan)
    df['rsi14'] = 100 - (100 / (1 + rs))
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    tr = pd.concat([
        (df['high'] - df['low']).abs(),
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    df['atr14'] = tr.rolling(14).mean()
    return df


def channel_stats(df, n=60):
    sub = df.tail(min(n, len(df))).copy().reset_index(drop=True)
    x = np.arange(len(sub))
    y = sub['close'].values
    slope, intercept = np.polyfit(x, y, 1)
    fit = slope * x + intercept
    resid = y - fit
    std = float(np.std(resid)) if len(resid) else 0.0
    mid = float(fit[-1])
    direction = '上升通道' if slope > 0 else ('下降通道' if slope < 0 else '横盘通道')
    return {'slope': float(slope), 'mid': mid, 'upper': mid + 2 * std, 'lower': mid - 2 * std, 'direction': direction}


def pivot_levels(df, window=2, lookback=120):
    sub = df.tail(min(lookback, len(df))).reset_index(drop=True)
    highs, lows = [], []
    for i in range(window, len(sub) - window):
        h = sub.loc[i, 'high']
        l = sub.loc[i, 'low']
        if h >= sub.loc[i - window:i + window, 'high'].max():
            highs.append(float(h))
        if l <= sub.loc[i - window:i + window, 'low'].min():
            lows.append(float(l))
    return sorted(set(lows)), sorted(set(highs))


def nearest_levels(price, supports, resistances, ma_levels):
    support_candidates = sorted(set([x for x in supports + ma_levels if x < price]))
    resistance_candidates = sorted(set([x for x in resistances + ma_levels if x > price]))
    return support_candidates[-2:][::-1], resistance_candidates[:2]


def trend_label(row):
    close = row['close']
    ma5, ma10, ma20, ma60 = row['ma5'], row['ma10'], row['ma20'], row['ma60']
    if pd.notna(ma20) and pd.notna(ma60) and close > ma5 > ma10 > ma20 > ma60:
        return '多头趋势'
    if pd.notna(ma20) and pd.notna(ma60) and close < ma5 < ma10 < ma20 < ma60:
        return '空头趋势'
    return '震荡/过渡趋势'


def tomorrow_view(row, channel, support1, resistance1, asset_type='stock'):
    close = float(row['close'])
    atr = float(row['atr14']) if pd.notna(row['atr14']) else None
    rsi = float(row['rsi14']) if pd.notna(row['rsi14']) else None
    macd = float(row['macd']) if pd.notna(row['macd']) else None
    signal = float(row['signal']) if pd.notna(row['signal']) else None
    trend = trend_label(row)
    if trend == '多头趋势' and channel['slope'] > 0 and (rsi is None or rsi < 78) and (macd is None or signal is None or macd >= signal):
        direction = '偏强震荡上行'
        strategy = f'若盘中回踩 {support1:.2f} 附近有承接，可继续持有或分批低吸；若冲到 {resistance1:.2f} 一带放量滞涨，则先兑现部分。'
    elif trend == '空头趋势' or channel['slope'] < 0:
        direction = '偏弱震荡下行'
        strategy = f'若有效跌破 {support1:.2f}，以风控优先；只有重新站回 {resistance1:.2f} 上方才算修复。'
    else:
        direction = '区间震荡'
        strategy = f'更可能围绕 {support1:.2f}-{resistance1:.2f} 震荡，靠近支撑看承接，靠近压力看兑现。'
    if atr is None or atr == 0:
        width = close * (0.015 if asset_type == 'fund' else 0.02)
        return direction, (close - width, close + width), strategy
    upper_factor = 0.9 if '上行' in direction else 0.6
    return direction, (close - 0.6 * atr, close + upper_factor * atr), strategy


def fetch_same_day_stock_quote(code: str):
    errors = []
    try:
        df = adu.ak_call(ak.stock_bid_ask_em, symbol=code, timeout=20, attempts=4)
        kv = {str(k): v for k, v in zip(df['item'], df['value'])}
        latest = adu.safe_float(kv.get('最新'))
        if latest:
            return {
                'source': 'stock_bid_ask_em',
                'latest': latest,
                'open': adu.safe_float(kv.get('今开')) or latest,
                'high': adu.safe_float(kv.get('最高')) or latest,
                'low': adu.safe_float(kv.get('最低')) or latest,
                'prev_close': adu.safe_float(kv.get('昨收')),
                'pct': adu.safe_float(kv.get('涨幅')),
                'amount': adu.safe_float(kv.get('成交额')),
            }
        errors.append('stock_bid_ask_em returned empty latest')
    except Exception as exc:
        errors.append(f'stock_bid_ask_em: {exc}')
    try:
        quote = adu.fetch_quote_with_fallback(code, refresh=True)
        latest = adu.safe_float((quote or {}).get('latest'))
        if latest:
            return quote
        errors.append('fetch_quote_with_fallback returned empty latest')
    except Exception as exc:
        errors.append(f'fetch_quote_with_fallback: {exc}')
    raise RuntimeError(' ; '.join(errors))


def fetch_same_day_fund_quote(item):
    errors = []
    for kind, func in [('lof', ak.fund_lof_spot_em), ('etf', ak.fund_etf_spot_em)]:
        try:
            df = adu.ak_call(func, timeout=20, attempts=4)
            sub = df[df['代码'].astype(str).str.zfill(6) == item['code']]
            if sub.empty:
                continue
            r = sub.iloc[0]
            latest = adu.safe_float(r.get('最新价'))
            if latest:
                return {
                    'source': f'fund_{kind}_spot_em',
                    'latest': latest,
                    'open': adu.safe_float(r.get('开盘价')) or latest,
                    'high': adu.safe_float(r.get('最高价')) or latest,
                    'low': adu.safe_float(r.get('最低价')) or latest,
                    'prev_close': adu.safe_float(r.get('昨收')),
                    'pct': adu.safe_float(r.get('涨跌幅')),
                    'amount': adu.safe_float(r.get('成交额')),
                }
        except Exception as exc:
            errors.append(f'fund_{kind}_spot_em: {exc}')
    try:
        quote = adu.fetch_quote_with_fallback(item['code'], refresh=True)
        latest = adu.safe_float((quote or {}).get('latest'))
        if latest:
            return quote
        errors.append('fetch_quote_with_fallback returned empty latest')
    except Exception as exc:
        errors.append(f'fetch_quote_with_fallback: {exc}')
    raise RuntimeError(' ; '.join(errors))


def build_missing_market_analysis(item, reason, source='same_day_quote', context='position_watch'):
    asof_validation = adu.validate_data_asof(
        NOW_DATE,
        data_date=None,
        captured_at=None,
        source=source,
        strict_today=True,
        allow_previous_close_only=False,
        context=context,
    )
    return {
        'item': item,
        'data_date': NOW_DATE,
        'close': None,
        'change_pct': None,
        'high': None,
        'low': None,
        'ma': {k: None for k in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120']},
        'rsi14': None,
        'macd': None,
        'signal': None,
        'atr14': None,
        'trend': '今日行情缺失，未生成技术判断',
        'channel': None,
        'supports': [],
        'resistances': [],
        'tomorrow_direction': '今日行情缺失，未生成技术判断',
        'tomorrow_range': None,
        'strategy': '今日行情缺失，未生成技术判断；不使用前一交易日价格替代，等待下一次有效当日行情后再给出操作判断。',
        'rr': None,
        'pnl_pct': None,
        'data_note': f'今日行情缺失，未生成技术判断；不使用前一交易日价格替代。原因：{reason}',
        'market_data_missing': True,
        'technical_missing': True,
        'asof_validation': asof_validation,
    }


def append_missing_market_data_lines(lines, analysis):
    lines.append('- 今日行情缺失，未生成技术判断。')
    lines.append('- 数据处理：不使用前一交易日价格替代；本标的今日不输出趋势、通道、支撑压力、盈亏比和明日价格区间。')
    lines.append(f"- 操作策略：{analysis['strategy']}")
    lines.append(f"- 数据备注：{analysis['data_note']}")


def build_position_watch_report_metadata(now=None, target_date=None, analyses=None):
    """Build a small, testable data-time metadata block for the position/watch report."""
    now_dt = now or datetime.now().astimezone()
    target = target_date or NOW_DATE
    analysis_list = list(analyses or [])
    missing_items = []
    for analysis in analysis_list:
        if analysis.get('market_data_missing') or analysis.get('technical_missing'):
            item = analysis.get('item') or {}
            name = item.get('name') or item.get('code') or '未知标的'
            code = item.get('code')
            missing_items.append(f'{name}（{code}）' if code else str(name))
    completeness = '存在今日行情缺失' if missing_items else '正常'
    if missing_items:
        shown = '、'.join(missing_items[:8])
        if len(missing_items) > 8:
            shown += f' 等{len(missing_items)}个标的'
        missing_note = f'部分标的今日行情缺失，未生成技术判断：{shown}'
    else:
        missing_note = '无'
    return '\n'.join([
        f'> 数据日期：{target}',
        f'> 生成时间：{now_dt.strftime("%Y-%m-%d %H:%M:%S")}',
        '> 报告类型：持仓/候选盘后分析',
        '> 行情日期要求：必须为当日行情',
        '> 是否允许回退前一交易日价格：否',
        f'> 数据完整性：{completeness}',
        f'> 缺失说明：{missing_note}',
        '> 备注：本报告用于盘后复盘与次日观察，不构成买卖建议',
    ])


def _latest_df_trade_date(df: pd.DataFrame):
    if df is None or df.empty or 'date' not in df.columns:
        return None
    try:
        return str(pd.to_datetime(df.iloc[-1]['date']).date())
    except Exception:
        return None


def _validate_analysis_data_date(data_date, source, context):
    return adu.validate_data_asof(
        NOW_DATE,
        data_date=data_date,
        captured_at=None,
        source=source,
        strict_today=True,
        allow_previous_close_only=False,
        context=context,
    )


def apply_same_day_quote(df: pd.DataFrame, quote: dict):
    if df is None or df.empty:
        return df
    latest = adu.safe_float((quote or {}).get('latest'))
    if latest is None or latest <= 0:
        return df
    prev_close = adu.safe_float((quote or {}).get('prev_close'))
    open_price = adu.safe_float((quote or {}).get('open')) or prev_close or latest
    high = adu.safe_float((quote or {}).get('high'))
    low = adu.safe_float((quote or {}).get('low'))
    amount = adu.safe_float((quote or {}).get('amount'))
    if high is None:
        high = max(x for x in [open_price, latest, prev_close] if x is not None)
    if low is None:
        low = min(x for x in [open_price, latest, prev_close] if x is not None)
    new_row = {'date': TODAY_TS, 'open': open_price, 'high': max(high, latest, open_price), 'low': min(low, latest, open_price), 'close': latest}
    if 'volume' in df.columns:
        new_row['volume'] = np.nan
    if 'amount' in df.columns:
        new_row['amount'] = amount
    if 'turnover' in df.columns:
        new_row['turnover'] = np.nan
    work = df.copy()
    work['date'] = pd.to_datetime(work['date'])
    if not work.empty and pd.Timestamp(work.iloc[-1]['date']).normalize() == TODAY_TS:
        for k, v in new_row.items():
            work.at[work.index[-1], k] = v
    else:
        work = pd.concat([work, pd.DataFrame([new_row])], ignore_index=True)
    return work.sort_values('date').reset_index(drop=True)


def analyze_stock(item):
    symbol = ('sz' if item['code'].startswith(('000', '001', '002', '003')) else 'sh') + item['code']
    quote = None
    quote_source = None
    try:
        quote = fetch_same_day_stock_quote(item['code'])
        quote_source = quote.get('source') or 'same_day_quote'
    except Exception as exc:
        quote = None
        quote_source = f'same_day_quote_failed: {exc}'
    hist_errors = []
    df = None
    source_note = ''
    try:
        df = adu.ak_call(ak.stock_zh_a_daily, symbol=symbol, start_date='20251001', end_date=NOW_DATE.replace('-', ''), adjust='qfq', timeout=35, attempts=4)
        source_note = '股票日线主路径使用 AkShare 前复权历史行情，并强制校验/拼接当天价格。'
    except Exception as exc:
        hist_errors.append(f'ak.stock_zh_a_daily: {exc}')
    if df is None or df.empty:
        try:
            df = adu.fetch_hist_df_with_fallback(item['code'], '20251001', NOW_DATE.replace('-', ''), adjust='qfq', refresh=True)
            source_note = f"股票日线已切换到 {df.attrs.get('source', 'fallback_hist')}，并强制校验/拼接当天价格。"
        except Exception as exc:
            hist_errors.append(f'hist_fallback: {exc}')
            df = None
    if quote is None and (df is None or df.empty):
        return build_missing_market_analysis(
            item,
            reason=f"无法获取今日行情；历史数据也不可用：{' ; '.join(hist_errors)}",
            source=quote_source or 'same_day_stock_quote',
            context=f"stock:{item['code']}",
        )
    if df is None or df.empty:
        quote_validation = adu.validate_data_asof(
            NOW_DATE,
            data_date=NOW_DATE,
            captured_at=datetime.now().astimezone(),
            source=quote_source,
            strict_today=True,
            allow_previous_close_only=False,
            context=f"stock_quote_only:{item['code']}",
        )
        if not quote_validation.get('ok'):
            return build_missing_market_analysis(item, reason=quote_validation.get('reason'), source=quote_source, context=f"stock_quote_only:{item['code']}")
        close = adu.safe_float((quote or {}).get('latest')) or 0.0
        return {
            'item': item,
            'data_date': NOW_DATE,
            'close': close,
            'change_pct': adu.safe_float((quote or {}).get('pct') or (quote or {}).get('change_pct')),
            'high': adu.safe_float((quote or {}).get('high')) or close,
            'low': adu.safe_float((quote or {}).get('low')) or close,
            'ma': {k: None for k in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120']},
            'rsi14': None,
            'macd': None,
            'signal': None,
            'atr14': None,
            'trend': '仅拿到当天快照，暂不输出历史结构结论',
            'channel': {'slope': 0.0, 'mid': close, 'upper': close, 'lower': close, 'direction': '当天快照模式'},
            'supports': [],
            'resistances': [],
            'tomorrow_direction': '数据不足，偏观察',
            'tomorrow_range': (close, close),
            'strategy': '已拿到当天价格，但历史结构不足；明日只看竞价、分时承接与量价是否同步，不使用前一日结论替代。',
            'rr': None,
            'pnl_pct': (close / float(item['cost']) * 100 - 100) if item.get('cost') and close else None,
            'data_note': f"{quote_source}；未能稳定拉到同口径历史K线，故仅保留当天快照。",
            'market_data_missing': False,
            'technical_missing': False,
            'asof_validation': quote_validation,
        }
    df = df.copy()
    if quote is not None:
        df = apply_same_day_quote(df, quote)
    data_validation = _validate_analysis_data_date(_latest_df_trade_date(df), quote_source or source_note or 'stock_history', f"stock:{item['code']}")
    if not data_validation.get('ok'):
        return build_missing_market_analysis(
            item,
            reason=f"{data_validation.get('reason')}；历史最新日期={_latest_df_trade_date(df) or '缺失'}；拒绝使用前一交易日价格出分析。",
            source=quote_source or source_note or 'stock_history',
            context=f"stock:{item['code']}",
        )
    df = enrich(df)
    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else row
    supports, resistances = pivot_levels(df)
    ma_levels = [float(row[c]) for c in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120'] if pd.notna(row[c])]
    sup_list, res_list = nearest_levels(float(row['close']), supports, resistances, ma_levels)
    support1 = sup_list[0] if sup_list else float(row['ma20']) if pd.notna(row['ma20']) else float(row['close']) * 0.97
    resistance1 = res_list[0] if res_list else float(row['close']) * 1.03
    ch = channel_stats(df)
    tomorrow_direction, tomorrow_range, strategy = tomorrow_view(row, ch, support1, resistance1, 'stock')
    rr = None
    risk = float(row['close']) - support1
    reward = resistance1 - float(row['close'])
    if risk > 0 and reward > 0:
        rr = reward / risk
    pnl_pct = None
    if item.get('cost'):
        pnl_pct = float(row['close']) / float(item['cost']) * 100 - 100
    return {
        'item': item,
        'data_date': str(pd.Timestamp(row['date']).date()),
        'close': float(row['close']),
        'change_pct': adu.safe_float((quote or {}).get('pct')) if quote is not None else (float(row['close']) / float(prev['close']) * 100 - 100 if float(prev['close']) else None),
        'high': adu.safe_float((quote or {}).get('high')) if quote is not None else float(row['high']),
        'low': adu.safe_float((quote or {}).get('low')) if quote is not None else float(row['low']),
        'ma': {k: float(row[k]) for k in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120']},
        'rsi14': float(row['rsi14']) if pd.notna(row['rsi14']) else None,
        'macd': float(row['macd']) if pd.notna(row['macd']) else None,
        'signal': float(row['signal']) if pd.notna(row['signal']) else None,
        'atr14': float(row['atr14']) if pd.notna(row['atr14']) else None,
        'trend': trend_label(row),
        'channel': ch,
        'supports': sup_list,
        'resistances': res_list,
        'tomorrow_direction': tomorrow_direction,
        'tomorrow_range': tomorrow_range,
        'strategy': strategy,
        'rr': rr,
        'pnl_pct': pnl_pct,
        'data_note': f"{source_note} 当天价格来自 {quote_source or 'history_today_bar'}；若主数据源缺当天bar，则已用当天实时行情强制拼接，拒绝直接沿用前一天收盘。",
    }


def analyze_fund(item):
    try:
        quote = fetch_same_day_fund_quote(item)
        quote_source = quote.get('source') or 'same_day_fund_quote'
    except Exception as exc:
        return build_missing_market_analysis(
            item,
            reason=f'无法获取今日场内基金行情：{exc}',
            source='same_day_fund_quote',
            context=f"fund:{item['code']}",
        )
    quote_validation = adu.validate_data_asof(
        NOW_DATE,
        data_date=NOW_DATE,
        captured_at=datetime.now().astimezone(),
        source=quote_source,
        strict_today=True,
        allow_previous_close_only=False,
        context=f"fund_quote:{item['code']}",
    )
    if not quote_validation.get('ok'):
        return build_missing_market_analysis(item, reason=quote_validation.get('reason'), source=quote_source, context=f"fund_quote:{item['code']}")
    try:
        nav = adu.ak_call(ak.fund_open_fund_info_em, symbol=item['code'], indicator='单位净值走势', period='1年', timeout=35, attempts=4).copy()
        source_note = '基金趋势结构基于近一年单位净值走势，并强制拼接当天场内价格。'
    except Exception:
        nav = None
        source_note = '基金净值历史拉取失败，仅保留当天场内价格。'
    if nav is None or nav.empty:
        close = adu.safe_float(quote.get('latest')) or 0.0
        return {
            'item': item,
            'data_date': NOW_DATE,
            'close': close,
            'change_pct': adu.safe_float(quote.get('pct') or quote.get('change_pct')),
            'high': adu.safe_float(quote.get('high')) or close,
            'low': adu.safe_float(quote.get('low')) or close,
            'ma': {k: None for k in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120']},
            'rsi14': None,
            'macd': None,
            'signal': None,
            'atr14': None,
            'trend': '仅拿到当天快照，暂不输出净值趋势结构结论',
            'channel': {'slope': 0.0, 'mid': close, 'upper': close, 'lower': close, 'direction': '当天快照模式'},
            'supports': [],
            'resistances': [],
            'tomorrow_direction': '数据不足，偏观察',
            'tomorrow_range': (close, close),
            'strategy': '只拿到当天场内价格，未能稳定获取净值走势；明日先看竞价、折溢价和量价承接，不用前一日净值替代今日分析。',
            'rr': None,
            'pnl_pct': (close / float(item['cost']) * 100 - 100) if item.get('cost') and close else None,
            'data_note': f"{quote_source}；未能稳定拉到净值历史，故仅保留当天场内快照。",
            'market_data_missing': False,
            'technical_missing': False,
            'asof_validation': quote_validation,
        }
    nav = nav.rename(columns={'净值日期': 'date', '单位净值': 'close'})
    nav['date'] = pd.to_datetime(nav['date'])
    nav['close'] = pd.to_numeric(nav['close'], errors='coerce')
    nav['open'] = nav['close'].shift().fillna(nav['close'])
    nav['high'] = nav[['open', 'close']].max(axis=1)
    nav['low'] = nav[['open', 'close']].min(axis=1)
    df = nav[['date', 'open', 'high', 'low', 'close']].dropna().copy()
    df = apply_same_day_quote(df, quote)
    data_validation = _validate_analysis_data_date(_latest_df_trade_date(df), quote_source or source_note or 'fund_history', f"fund:{item['code']}")
    if not data_validation.get('ok'):
        return build_missing_market_analysis(
            item,
            reason=f"{data_validation.get('reason')}；历史最新日期={_latest_df_trade_date(df) or '缺失'}；拒绝使用前一交易日价格出分析。",
            source=quote_source or source_note or 'fund_history',
            context=f"fund:{item['code']}",
        )
    df = enrich(df)
    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else row
    supports, resistances = pivot_levels(df)
    ma_levels = [float(row[c]) for c in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120'] if pd.notna(row[c])]
    ref_price = adu.safe_float(quote.get('latest')) or float(row['close'])
    sup_list, res_list = nearest_levels(float(row['close']), supports, resistances, ma_levels)
    support1 = sup_list[0] if sup_list else float(row['ma20']) if pd.notna(row['ma20']) else float(row['close']) * 0.97
    resistance1 = res_list[0] if res_list else float(row['close']) * 1.03
    ch = channel_stats(df)
    tomorrow_direction, tomorrow_range, strategy = tomorrow_view(row, ch, support1, resistance1, 'fund')
    rr = None
    risk = float(row['close']) - support1
    reward = resistance1 - float(row['close'])
    if risk > 0 and reward > 0:
        rr = reward / risk
    pnl_pct = None
    if item.get('cost'):
        pnl_pct = ref_price / float(item['cost']) * 100 - 100
    return {
        'item': item,
        'data_date': str(pd.Timestamp(row['date']).date()),
        'close': ref_price,
        'change_pct': adu.safe_float(quote.get('pct') or quote.get('change_pct')) if quote is not None else (float(row['close']) / float(prev['close']) * 100 - 100 if float(prev['close']) else None),
        'high': adu.safe_float(quote.get('high')) if quote is not None else float(row['high']),
        'low': adu.safe_float(quote.get('low')) if quote is not None else float(row['low']),
        'ma': {k: float(row[k]) for k in ['ma5', 'ma10', 'ma20', 'ma60', 'ma120']},
        'rsi14': float(row['rsi14']) if pd.notna(row['rsi14']) else None,
        'macd': float(row['macd']) if pd.notna(row['macd']) else None,
        'signal': float(row['signal']) if pd.notna(row['signal']) else None,
        'atr14': float(row['atr14']) if pd.notna(row['atr14']) else None,
        'trend': trend_label(row),
        'channel': ch,
        'supports': sup_list,
        'resistances': res_list,
        'tomorrow_direction': tomorrow_direction,
        'tomorrow_range': tomorrow_range,
        'strategy': strategy,
        'rr': rr,
        'pnl_pct': pnl_pct,
        'data_note': f"{source_note} 当天价格来自 {quote_source}；若净值历史未覆盖当天，则已用当天场内价格强制拼接，拒绝直接沿用前一天净值做盘后分析。",
    }


def fmt_levels(vals):
    return '、'.join(f'{x:.2f}' for x in vals) if vals else '暂无可靠近端位置'


def fmt_num(v, digits=2):
    return '数据缺失' if v is None else f'{v:.{digits}f}'


def main():
    if adu.skip_cron_if_not_a_share_trading_day(NOW_DATE, task='ashare-position-watch-analysis'):
        return
    # Load sector constituent cache from DB (new layer)
    code_to_sector, sector_tiers, sector_stage_map = load_sector_constituent_cache()

    expected_day_dir = preferred_analysis_day_dir()
    close_summary = find_latest_close_summary()
    text = _read_text(close_summary)
    sector_tier_source = 'db'
    if not sector_tiers:
        fb_code_to_sector, fb_sector_tiers, fb_stage_map = parse_close_summary_sector_tiers(text)
        if fb_sector_tiers:
            for code, item in fb_code_to_sector.items():
                code_to_sector.setdefault(code, item)
            sector_tiers = fb_sector_tiers
            for sector_name, stage in fb_stage_map.items():
                sector_stage_map.setdefault(sector_name, stage)
            sector_tier_source = 'close_summary_text'
        else:
            sector_tier_source = 'missing'
    holding_summary = find_latest_holding_summary()
    holding_text = _read_text(holding_summary)
    ledger_holdings, ledger_holding_source, ledger_holdings_line, ledger_trade_date = load_holdings_from_ledger()
    candidates = parse_candidates(text)
    recent_review_rows = load_recent_review_context(limit=3)
    candidate_history = build_candidate_history(recent_review_rows)
    if ledger_holding_source or ledger_holdings_line is not None:
        holdings = ledger_holdings
    else:
        holdings = parse_holdings(holding_text)
    name_to_code = resolve_codes([h for h in holdings if not h.get('code')]) if holdings else {}
    board_stage_map = parse_board_stage_map(text)
    candidate_sector_map = parse_candidate_sector_map(text)
    todays_codes = {c['code'] for c in candidates}
    for code, meta in sorted(candidate_history.items(), key=lambda kv: (-kv[1].get('count', 0), kv[0])):
        if code in todays_codes:
            continue
        if meta.get('count', 0) < 2:
            continue
        candidates.append({'name': meta.get('name') or code, 'code': code, 'group': '候选股', 'asset_type': 'stock', 'from_recent_reviews': True, 'candidate_origin': 'recent_review_supplement'})
        if meta.get('sectors'):
            candidate_sector_map.setdefault(code, meta['sectors'][0])
        todays_codes.add(code)

    stock_items = []
    for h in holdings:
        code = str(h.get('code') or '').zfill(6) if h.get('code') else None
        asset_type = h.get('asset_type')
        if not code:
            resolved = name_to_code.get(h['name'])
            if not resolved:
                continue
            code, asset_type = resolved
        # Enrich with DB-level sector context if available
        db_ctx = code_to_sector.get(code) or {}
        stock_items.append({
            'name': h['name'], 'code': code, 'group': '持仓股',
            'cost': h.get('cost'), 'shares': h.get('shares'), 'weight': h.get('weight'),
            'asset_type': asset_type,
            'sector': db_ctx.get('sector_name') or candidate_sector_map.get(code),
            'role': db_ctx.get('role'),
            'board_stage': db_ctx.get('sector_name') and sector_stage_map.get(db_ctx.get('sector_name')),
        })
    seen = {x['code'] for x in stock_items}
    for c in candidates:
        if c['code'] not in seen:
            db_ctx = code_to_sector.get(c['code']) or {}
            c['sector'] = db_ctx.get('sector_name') or candidate_sector_map.get(c['code'])
            c['board_stage'] = db_ctx.get('sector_name') and sector_stage_map.get(db_ctx.get('sector_name'))
            if not c.get('role'):
                c['role'] = db_ctx.get('role')
            c.setdefault('candidate_origin', 'today_close_summary')
            c['recent_history'] = candidate_history.get(c['code'])
            stock_items.append(c)
            seen.add(c['code'])

    analyses = []
    for x in stock_items:
        if x['asset_type'] == 'fund' or is_fund_like(x['name'], x['code']):
            analyses.append(analyze_fund(x))
        else:
            analyses.append(analyze_stock(x))
    for a in analyses:
        item = a['item']
        if item.get('group') == '候选股':
            a['candidate_score'] = candidate_strength_score(item, a)
            a['action_tag'] = candidate_action_tag(item, a)
        else:
            a['candidate_score'] = None
            a['action_tag'] = None

    data_date = max(a['data_date'] for a in analyses) if analyses else NOW_DATE
    day_dir = ROOT / NOW_DATE
    day_dir.mkdir(parents=True, exist_ok=True)
    note_path = day_dir / f'持仓股与候选股分析-{NOW_DATE}.md'
    source_validation = {
        'close': validate_review_source(close_summary, expected_day_dir, 'close'),
        'holding': validate_review_source(holding_summary, expected_day_dir, 'holding'),
        'ledger': {
            'label': 'ledger',
            'expected_day': expected_day_dir.name if expected_day_dir else None,
            'actual_day': ledger_trade_date,
            'is_expected_day': bool(ledger_trade_date and expected_day_dir and ledger_trade_date == expected_day_dir.name),
            'used_fallback': bool(ledger_trade_date and expected_day_dir and ledger_trade_date != expected_day_dir.name),
            'path': ledger_holding_source,
        },
    }

    lines = [f'# 持仓股与候选股分析 - {NOW_DATE}', '']
    lines.extend(build_position_watch_report_metadata(target_date=NOW_DATE, analyses=analyses).splitlines())
    lines.append('')
    lines.append(f'> 数据口径：技术分析基于最新可用日线/净值 **截至 {data_date}**。')
    holding_source_label = ledger_holding_source or holding_summary or close_summary
    if ledger_holdings_line:
        holding_source_note = '持仓优先来自账本日报/DB 快照；若账本缺失，再回退到 close-summary 文本。'
    else:
        holding_source_note = '持仓从最近一份含当前持仓记录的 close-summary 中提取。'
    lines.append(f'> 标的来源：持仓来源 `{holding_source_label}`；{holding_source_note} 候选股从最近 close-summary 的"次日计划"提取。')
    lines.append(f'> 来源校验：close={source_validation["close"]["actual_day"] or "缺失"}，holding={source_validation["holding"]["actual_day"] or "缺失"}，ledger={source_validation["ledger"]["actual_day"] or "缺失"}；预期分析基线日={source_validation["close"]["expected_day"] or "缺失"}。')
    lines.append('> 交易约束：你是 **短线** 风格，资金约 **16000 RMB**；因此更强调 1~2 个核心标的、分批处理、先看盈亏比再决定是否出手。')
    lines.append('> 选股范围：允许主板股票 + ETF/LOF 等场内基金产品；仍排除科创板、创业板、北交所个股。')
    source_note = {
        'db': '已命中今日/最近一次可用的盘中 `sector_constituent_snapshots`',
        'close_summary_text': '今日盘中未捕获，已退而使用 close-summary 文本',
        'missing': '今日盘中未捕获，且 close-summary 文本也无可用梯队数据',
    }.get(sector_tier_source, '数据来源未知')
    lines.append(f'> 板块梯队数据来源：优先使用今日盘中 `sector_constituent_snapshots`（{source_note}）。')
    if recent_review_rows:
        recent_days = '、'.join([row['trade_date'] for row in recent_review_rows])
        lines.append(f'> 多日复盘融合：候选分析在今天收盘基线之外，额外参考近 3 个交易日复盘：{recent_days}。仅把多日重复出现或连续跟踪的标的作为增强上下文，不允许用旧日价格替代今日价格。')
    else:
        lines.append('> 多日复盘融合：未找到近 3 个交易日复盘文件，当前仅使用今天收盘基线。')
    lines.append('')
    lines.append('## 方法说明')
    lines.append('- 上升/下降通道：近60个交易日收盘价（或基金净值）线性回归中轴 ± 2倍残差标准差。')
    lines.append('- 支撑/压力：近120个交易日枢轴低点/高点，并用 MA5/10/20/60/120 做交叉验证。')
    lines.append('- 明日预测：给出偏强/区间/偏弱的场景判断与操作预案，不作为确定性承诺。')
    lines.append('- 短线资金纪律：16000 资金体量不适合同时摊太多票，明日若开新仓，优先只做 1 个最强核心，单票先分两笔。')
    lines.append('')

    if ledger_lib is not None:
        try:
            appendix = ledger_lib.build_close_summary_appendix(NOW_DATE)
            if appendix:
                lines.append('## 今日账本操作摘要')
                lines.extend(appendix.splitlines()[1:])
                lines.append('')
        except Exception:
            pass

    # ---- 板块梯队复盘（新增章节）----
    if sector_tiers:
        lines.append('## 板块梯队复盘')
        lines.append('- 梯队说明：龙头 = 板块综合最强领涨股；中军 = 成交额大、板块代表性强的票；补涨 = 跟随板块上涨但非龙头/中军。')
        lines.append('- 以下梯队来自今日盘中采集的 `sector_constituent_snapshots`，反映的是今日收盘时点的板块成分结构与强弱。')
        lines.append('')
        for sn, tier in sector_tiers.items():
            stage = sector_stage_map.get(sn) or '阶段缺失'
            lines.append(f'### {sn}（{stage}）')
            role_map = {}
            for item in tier:
                role = item.get('role', '补涨')
                role_map.setdefault(role, []).append(item)
            for role_label in ['龙头', '中军', '补涨']:
                for item in role_map.get(role_label, [])[:4]:
                    name = item.get('name') or '?'
                    code = item.get('code') or '?'
                    pct = f"{item.get('pct_change'):+.2f}%" if item.get('pct_change') is not None else '数据缺失'
                    amt = f"{item.get('amount')/1e8:.2f}亿" if item.get('amount') else '数据缺失'
                    lines.append(f'- [{role_label}] {name}（{code}）：{pct}，成交额 {amt}')
            lines.append('')
    else:
        lines.append('## 板块梯队复盘（数据缺失）')
        lines.append('- 今日盘中未采集到 `sector_constituent_snapshots`，无法输出板块梯队。需确认今日 `ashare-background-monitor` 是否正常写入该表。')
        lines.append('')

    for a in analyses:
        item = a['item']
        title = '持仓' if item['group'] == '持仓股' else '候选'
        db_ctx = code_to_sector.get(item['code']) or {}
        sector = item.get('sector') or db_ctx.get('sector_name')
        board_stage = item.get('board_stage') or (sector and sector_stage_map.get(sector))
        lines.append(f"## {title}：{item['name']}（{item['code']}）")
        if item.get('asset_type') == 'stock':
            role = item.get('role') or db_ctx.get('role')
            role_str = f'；板块地位：{role}' if role else ''
            sector_text = sector or ('、'.join((item.get('recent_history') or {}).get('sectors') or []) if item.get('recent_history') else None) or '待补充'
            stage_text = board_stage or ('、'.join((item.get('recent_history') or {}).get('stages') or []) if item.get('recent_history') else None) or '待补充'
            lines.append(f"- 所属板块：{sector_text}；当前板块阶段：{stage_text}{role_str}")
        elif item.get('asset_type') == 'fund':
            lines.append('- 品种属性：ETF/LOF 场内基金，走势更受商品/指数本身驱动，不按A股行业板块方式归类。')
        if a.get('market_data_missing') or a.get('technical_missing'):
            append_missing_market_data_lines(lines, a)
            lines.append('')
            continue
        lines.append(f"- 最新收盘/现价：{a['close']:.2f}，涨跌幅 {a['change_pct']:+.2f}%；日内区间 {a['low']:.2f} - {a['high']:.2f}")
        lines.append(f"- 趋势判断：**{a['trend']}**")
        lines.append(f"- 通道结构：{a['channel']['direction']}；中轴 {a['channel']['mid']:.2f}，上轨 {a['channel']['upper']:.2f}，下轨 {a['channel']['lower']:.2f}")
        lines.append(f"- 均线：MA5 {a['ma']['ma5']:.2f} / MA10 {a['ma']['ma10']:.2f} / MA20 {a['ma']['ma20']:.2f} / MA60 {a['ma']['ma60']:.2f} / MA120 {a['ma']['ma120']:.2f}")
        lines.append(f"- 指标：RSI14 {fmt_num(a['rsi14'])}，MACD {fmt_num(a['macd'],3)}，Signal {fmt_num(a['signal'],3)}，ATR14 {fmt_num(a['atr14'])}")
        lines.append(f"- 关键支撑位：{fmt_levels(a['supports'])}")
        lines.append(f"- 关键压力位：{fmt_levels(a['resistances'])}")
        if item['group'] == '持仓股' and item.get('cost') is not None:
            lines.append(f"- 持仓信息：成本 {item['cost']:.3f}，仓位 {item.get('weight') or '未知'}，股数 {item.get('shares') or '未知'}；按最新现价测算浮动收益 {a['pnl_pct']:+.2f}%")
        if item['group'] == '候选股':
            origin = '今天主候选' if item.get('candidate_origin') == 'today_close_summary' else '近3日复盘补充候选'
            lines.append(f"- 候选来源分层：{origin}")
        if item['group'] == '候选股' and item.get('recent_history'):
            hist = item['recent_history']
            sector_hist = '、'.join(hist.get('sectors') or []) or '待补充'
            lines.append(f"- 近3日复盘轨迹：出现 {hist.get('count', 0)} 次；出现日期 {('、'.join(hist.get('days') or []) or '无')}；近3日关联板块 {sector_hist}")
        if item['group'] == '候选股' and a.get('candidate_score'):
            cs = a['candidate_score']
            lines.append(f"- 多日跟踪强度分：**{cs['score']} 分（{cs['level']}级）**；评分因子：{'、'.join(cs['reasons']) if cs['reasons'] else '无'}")
            lines.append(f"- 明日动作标签：**{a.get('action_tag') or '只观察'}**")
        lines.append(f"- 明日走势预判：**{a['tomorrow_direction']}**；预估波动区间 {a['tomorrow_range'][0]:.2f} - {a['tomorrow_range'][1]:.2f}")
        if a['rr'] is not None:
            lines.append(f"- 盈亏比参考：约 **{a['rr']:.2f}:1**")
        else:
            lines.append('- 盈亏比参考：当前距离最近支撑或压力过近，短线盈亏比一般，需要等分时确认。')
        if item['group'] == '候选股' and item.get('asset_type') == 'stock' and board_stage in ('主升', '修复'):
            lines.append('- 短线适配度：相对更适合小资金做短线跟随，但仍需等分时回踩承接确认。')
        elif item['group'] == '候选股':
            lines.append('- 短线适配度：更适合观察，不宜在板块分歧未明时直接追高。')
        lines.append(f"- 操作策略：{a['strategy']}")
        lines.append(f"- 数据备注：{a['data_note']}")
        lines.append('')

    strong = [a['item']['name'] for a in analyses if '上行' in a['tomorrow_direction']]
    range_names = [a['item']['name'] for a in analyses if '区间' in a['tomorrow_direction']]
    weak = [a['item']['name'] for a in analyses if '下行' in a['tomorrow_direction']]
    candidate_ranked = [a for a in analyses if a['item']['group'] == '候选股' and a.get('candidate_score')]
    candidate_ranked.sort(key=lambda a: (-(a['candidate_score']['score']), a['item']['name']))
    tradable_candidates = [a['item']['name'] for a in candidate_ranked if a['item'].get('board_stage') in ('主升', '修复') and ('上行' in a['tomorrow_direction'] or '区间' in a['tomorrow_direction'])]
    primary_candidates = [a for a in candidate_ranked if a['item'].get('candidate_origin') == 'today_close_summary']
    supplemental_candidates = [a for a in candidate_ranked if a['item'].get('candidate_origin') != 'today_close_summary']
    top_watch = []
    for a in primary_candidates + supplemental_candidates:
        if a not in top_watch:
            top_watch.append(a)
        if len(top_watch) >= 2:
            break
    lines.append('## 明日前2重点盯盘清单')
    if top_watch:
        for idx, a in enumerate(top_watch, start=1):
            cs = a['candidate_score'] or {}
            item = a['item']
            sector_text = item.get('sector') or ('、'.join((item.get('recent_history') or {}).get('sectors') or []) or '待补充')
            stage_text = item.get('board_stage') or ('、'.join((item.get('recent_history') or {}).get('stages') or []) or '待补充')
            lines.append(f"{idx}. {item['name']}（{item['code']}，{cs.get('score', 0)}分/{cs.get('level', 'C')}级，{'今天主候选' if item.get('candidate_origin') == 'today_close_summary' else '复盘补充'}）")
            lines.append(f"   - 板块/阶段：{sector_text} / {stage_text}；角色：{item.get('role') or '待补充'}")
            if a.get('market_data_missing') or a.get('technical_missing') or not a.get('tomorrow_range'):
                lines.append('   - 明日预判：今日行情缺失，未生成技术判断；区间 暂不输出')
            else:
                lines.append(f"   - 明日预判：{a.get('tomorrow_direction') or '待补充'}；区间 {a['tomorrow_range'][0]:.2f}-{a['tomorrow_range'][1]:.2f}")
            lines.append(f"   - 重点原因：{('、'.join((cs.get('reasons') or [])[:4]) if cs.get('reasons') else '无明显加分项')}")
            lines.append(f"   - 动作标签：{a.get('action_tag') or '只观察'}")
            lines.append(f"   - 执行动作：{a.get('strategy')}")
    else:
        lines.append('- 今日没有满足条件的重点盯盘候选。')
    lines.append('')
    lines.append('## 组合级结论')
    lines.append(f"- 偏强跟踪：{('、'.join(strong) if strong else '暂无')}")
    lines.append(f"- 区间观察：{('、'.join(range_names) if range_names else '暂无')}")
    lines.append(f"- 偏弱防守：{('、'.join(weak) if weak else '暂无')}")
    if primary_candidates:
        lines.append('- 今日主候选优先级（多日跟踪强度分）：')
        for a in primary_candidates[:4]:
            cs = a['candidate_score']
            lines.append(f"  - {a['item']['name']}（{a['item']['code']}）：{cs['score']} 分 / {cs['level']}级；{('、'.join(cs['reasons'][:4]) if cs['reasons'] else '无明显加分项')}")
    if supplemental_candidates:
        lines.append('- 近3日复盘补充候选优先级：')
        for a in supplemental_candidates[:4]:
            cs = a['candidate_score']
            lines.append(f"  - {a['item']['name']}（{a['item']['code']}）：{cs['score']} 分 / {cs['level']}级；{('、'.join(cs['reasons'][:4]) if cs['reasons'] else '无明显加分项')}")
    lines.append(f"- 更适合短线小资金优先看的候选：{('、'.join(tradable_candidates[:2]) if tradable_candidates else '暂无明显优先级')}")
    lines.append('- 对 16000 资金的执行建议：如果明天出手，优先只聚焦 1 只最强候选；单票先用 1/2 计划仓试单，确认承接后再补，不要三四只同时摊开。')
    lines.append('- 实盘执行上，先看开盘是否尊重支撑/压力，再看板块共振与成交量是否配合；不要把预测当成无条件指令。')
    lines.append('')
    lines.append('## 后续可扩展')
    lines.append('- 如果你把完整持仓和候选池发我（含成本、仓位、计划仓、是否可T），我可以把这份分析升级成更完整的"明日交易计划表"，包含分笔买点、止损位、止盈位、失败条件。')

    note_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'note_path={note_path}')
    print(f'data_date={data_date}')
    print(f'sector_tiers_available={bool(sector_tiers)}')
    print('source_validation=' + json.dumps(source_validation, ensure_ascii=False))
    print('stocks=' + ','.join([f"{a['item']['name']}({a['item']['code']})" for a in analyses]))


if __name__ == '__main__':
    main()
