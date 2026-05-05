#!/usr/bin/env python3
import contextlib
import io
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pandas as pd

import ashare_data_utils as adu
import ashare_strategy_engine as ase
import ashare_ledger_lib as ledger

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
DB_PATH = ROOT / 'ashare_monitor.db'
CST = datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8))
TODAY = datetime.now(tz=CST).date().isoformat()
DAY_DIR = ROOT / TODAY
OPENING_BRIEF_PATH = DAY_DIR / 'opening-brief.md'
OPENING_CONTEXT_PATH = DAY_DIR / 'opening-brief-context.json'
NOTE_PATH = DAY_DIR / 'opening-action-table-0926.md'
CONTEXT_PATH = DAY_DIR / 'opening-action-table-0926-context.json'

FUND_CACHE = {'lof': None, 'etf': None, 'index': None, 'board': None}
BOARD_CONS_CACHE = {}


# ---------- generic helpers ----------
def safe_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace(',', '').replace('%', '').replace('—', '').strip()
            if value in {'', '-', '--'}:
                return None
        out = float(value)
        if pd.isna(out):
            return None
        return out
    except Exception:
        return None


def fmt_num(value, digits=2):
    value = safe_float(value)
    return '—' if value is None else f'{value:.{digits}f}'


def fmt_pct(value, digits=2):
    value = safe_float(value)
    return '—' if value is None else f'{value:.{digits}f}%'


def round_price(value):
    value = safe_float(value)
    return None if value is None else round(value + 1e-8, 2)


def fmt_range(low, high):
    low = round_price(low)
    high = round_price(high)
    if low is None and high is None:
        return '—'
    if low is None:
        return f'≤{high:.2f}'
    if high is None:
        return f'≥{low:.2f}'
    if abs(low - high) < 0.005:
        return f'{low:.2f}'
    return f'{low:.2f}~{high:.2f}'


def read_json(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def read_text(path: Path):
    return path.read_text(encoding='utf-8') if path.exists() else ''


def normalize_code(code):
    s = ''.join(ch for ch in str(code or '') if ch.isdigit())
    return s[-6:].zfill(6) if s else None


def normalize_sector_name(name: str | None) -> str:
    text = str(name or '').strip()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+$', '', text)
    text = re.sub(r'(?<![A-Za-z])[IVX]+$', '', text)
    return text or str(name or '').strip()


def is_main_board_code(code: str | None) -> bool:
    code = str(code or '')
    if code.startswith(('300', '301', '688', '689', '8', '4')):
        return False
    return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003'))


def is_fund_like(code: str | None, name: str = '') -> bool:
    code = str(code or '')
    name = str(name or '').upper()
    return 'ETF' in name or 'LOF' in name or code.startswith(('15', '16', '50', '51', '56', '58'))


def parse_heading_value(text: str, heading: str) -> str | None:
    m = re.search(rf'^- {re.escape(heading)}：(.+)$', text, flags=re.M)
    if not m:
        return None
    return m.group(1).replace('**', '').strip()


def parse_symbol(cell: str):
    m = re.search(r'(.+?)（(\d{6})）', cell or '')
    if m:
        return {'name': m.group(1).strip(), 'code': m.group(2)}
    m = re.search(r'(.+?)\((\d{6})\)', cell or '')
    if m:
        return {'name': m.group(1).strip(), 'code': m.group(2)}
    return {'name': (cell or '').strip(), 'code': None}


def calc_open_pct(open_price, prev_close):
    open_price = safe_float(open_price)
    prev_close = safe_float(prev_close)
    if open_price is None or prev_close in (None, 0):
        return None
    return (open_price - prev_close) / prev_close * 100


@contextlib.contextmanager
def suppress_output():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield


# ---------- parsing input notes ----------
def parse_plan_band(text: str):
    if not text:
        return {'buy_low': None, 'buy_high': None, 'breakout': None}
    buy = re.search(r'低吸区：([0-9.]+)~([0-9.]+)', text)
    brk = re.search(r'突破确认区：([0-9.]+)', text)
    return {
        'buy_low': safe_float(buy.group(1)) if buy else None,
        'buy_high': safe_float(buy.group(2)) if buy else None,
        'breakout': safe_float(brk.group(1)) if brk else None,
    }


def parse_tp(text: str):
    if not text:
        return {'tp1': None, 'tp2': None}
    nums = [safe_float(x) for x in re.findall(r'[0-9]+(?:\.[0-9]+)?', text)]
    nums = [x for x in nums if x is not None]
    return {
        'tp1': nums[0] if len(nums) >= 1 else None,
        'tp2': nums[1] if len(nums) >= 2 else None,
    }


def parse_markdown_table(lines: list[str], heading: str):
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    if start is None:
        return []
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start + 1 >= len(lines) or '|' not in lines[start]:
        return []
    header = [c.strip() for c in lines[start].strip().strip('|').split('|')]
    rows = []
    i = start + 2
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            break
        if stripped.startswith('### ') and stripped != heading:
            break
        if stripped.startswith('|'):
            values = [c.strip() for c in stripped.strip('|').split('|')]
            if len(values) == len(header):
                row = dict(zip(header, values))
                row['_notes'] = []
                rows.append(row)
            i += 1
            continue
        if stripped.startswith('>') and rows:
            rows[-1]['_notes'].append(stripped.lstrip('> ').strip())
            i += 1
            continue
        if stripped.startswith('## '):
            break
        i += 1
    return rows


def load_opening_tables():
    text = read_text(OPENING_BRIEF_PATH)
    lines = text.splitlines()
    holding_rows = parse_markdown_table(lines, '### 【持仓执行计划表】')
    candidate_rows = parse_markdown_table(lines, '### 【候选执行计划表】')
    return text, holding_rows, candidate_rows


def parse_opening_brief_candidate_section(text: str):
    if not text:
        return []
    section_m = re.search(r'## 3\. 基于前一日复盘 \+ 新闻生成的新候选股\n(.*?)\n## 4\.', text, flags=re.S)
    if not section_m:
        return []
    section = section_m.group(1)
    entries = re.split(r'\n### ', '\n' + section)
    rows = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        lines = entry.splitlines()
        symbol = parse_symbol(lines[0].replace('### ', '').strip())
        code = normalize_code(symbol.get('code'))
        if not code:
            continue
        item = {
            'name': symbol.get('name') or code,
            'code': code,
            'priority': 'B',
            'buy_flag': '先观察',
            'position_plan': '0%-10%',
            'probe_plan': '0%-5%',
            'add_condition': '只有板块共振且放量站稳突破位后再考虑加仓',
            'trigger': '先看竞价和开盘承接，再等回踩确认或放量突破',
            'stop': None,
            'tp1': None,
            'tp2': None,
            'buy_low': None,
            'buy_high': None,
            'breakout': None,
            'sector': None,
            'stage': None,
            'role': None,
            'notes': [],
        }
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith('- 所属板块：'):
                payload = stripped.split('：', 1)[1].strip()
                if ' / 板块阶段：' in payload:
                    sector, stage = payload.split(' / 板块阶段：', 1)
                    item['sector'] = sector.strip()
                    item['stage'] = stage.strip()
                else:
                    item['sector'] = payload
            elif stripped.startswith('- 板块角色：'):
                item['role'] = stripped.split('：', 1)[1].strip()
            elif stripped.startswith('- 计划关注点：'):
                item['notes'].append(stripped.split('：', 1)[1].strip())
            elif stripped.startswith('- 确认条件：'):
                item['trigger'] = stripped.split('：', 1)[1].strip()
            elif stripped.startswith('- 放弃条件：'):
                item['notes'].append(f"放弃条件：{stripped.split('：', 1)[1].strip()}")
        rows.append(item)
    return rows


def parse_opening_context_candidates(opening_context: dict, close_info: dict):
    if not opening_context:
        return []

    close_map = {c['code']: c for c in close_info.get('candidates', []) if c.get('code')}
    row_map = {}

    def add_item(name: str | None, code: str | None, sector: str | None = None, role: str | None = None):
        code = normalize_code(code)
        if not code:
            return
        base = close_map.get(code, {})
        record = row_map.get(code, {
            'name': name or base.get('name') or code,
            'code': code,
            'priority': 'B',
            'buy_flag': '先观察',
            'position_plan': '0%-10%',
            'probe_plan': '0%-5%',
            'add_condition': '只有板块共振且放量站稳突破位后再考虑加仓',
            'trigger': '先看竞价和开盘承接，再等回踩确认或放量突破',
            'stop': None,
            'tp1': None,
            'tp2': None,
            'buy_low': None,
            'buy_high': None,
            'breakout': None,
            'sector': base.get('sector'),
            'stage': base.get('stage'),
            'role': base.get('role'),
            'notes': [],
        })
        if name and (record.get('name') in {None, '', code}):
            record['name'] = name
        if sector and not record.get('sector'):
            record['sector'] = sector
        if role and not record.get('role'):
            record['role'] = role
        if record.get('sector') and not record.get('stage'):
            record['stage'] = close_info.get('boards', {}).get(record['sector'], {}).get('stage')
        row_map[code] = record

    for raw in opening_context.get('new_candidates') or []:
        text = str(raw or '').strip()
        symbol = parse_symbol(text)
        add_item(symbol.get('name'), symbol.get('code'))

    for raw in opening_context.get('focus') or []:
        text = str(raw or '').strip()
        code_m = re.search(r'[（(](\d{6})[，,)）]', text)
        if not code_m:
            continue
        code = code_m.group(1)
        prefix = text.split('（', 1)[0].split('(', 1)[0].strip(' -—')
        info_m = re.search(r'[（(]\d{6}[，,]\s*([^，,）)]+)\s*[，,]\s*([^）)]+)[）)]', text)
        sector = info_m.group(1).strip() if info_m else None
        role = info_m.group(2).strip() if info_m else None
        add_item(prefix or None, code, sector=sector, role=role)

    return list(row_map.values())


def preferred_review_day_dir() -> Path | None:
    return adu.preferred_review_day_dir(ROOT, TODAY)


def pick_first_existing(*paths: Path | None) -> Path | None:
    return adu.pick_first_existing(*paths)


def _has_structured_candidate_section(path: Path | None) -> bool:
    if not path or not path.exists():
        return False
    text = read_text(path)
    return bool(re.search(r'## 3\. 个股筛选（.*?）', text)) or bool(re.search(r'^##\s+候选：', text, flags=re.M))


def latest_close_summary_before_today() -> Path | None:
    picked = adu.latest_review_file(ROOT, 'close-summary.md', TODAY, preferred_names=['close-summary.md', 'latest-summary.md'])
    if _has_structured_candidate_section(picked):
        return picked

    day_dirs = adu.list_review_day_dirs(ROOT, before_date=TODAY)
    for day_dir in reversed(day_dirs):
        if picked and day_dir == picked.parent:
            continue
        candidate = adu.pick_first_existing(day_dir / 'close-summary.md', day_dir / 'latest-summary.md')
        if _has_structured_candidate_section(candidate):
            return candidate
    return picked


def latest_position_analysis_before_today() -> Path | None:
    return adu.latest_review_file(ROOT, '持仓股与候选股分析-*.md', TODAY)


def latest_holding_pnl_context_before_today() -> Path | None:
    return adu.latest_review_file(ROOT, 'holding-pnl-1505-context.json', TODAY)


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


def parse_close_summary(close_path: Path | None, analysis_path: Path | None = None):
    close_text = read_text(close_path) if close_path else ''
    analysis_text = read_text(analysis_path) if analysis_path else ''
    def _parse_single(text: str):
        if not text:
            return {'boards': {}, 'candidates': []}
        boards = {}
        board_m = re.search(r'## 2\. 板块分析\n(.*?)\n## 2\.1', text, flags=re.S)
        if not board_m:
            board_m = re.search(r'## 2\. 板块分析\n(.*?)\n## 3\.', text, flags=re.S)
        if board_m:
            section = board_m.group(1)
            chunks = re.split(r'\n### ', '\n' + section)
            for chunk in chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                lines = chunk.splitlines()
                name = lines[0].replace('### ', '').strip()
                info = {'name': name}
                for line in lines[1:]:
                    if line.startswith('- 板块涨幅：'):
                        info['pct'] = safe_float(line.split('：', 1)[1])
                    elif line.startswith('- 板块龙头：'):
                        info['leader'] = line.split('：', 1)[1].strip()
                    elif line.startswith('- 板块是主升、分歧、修复还是退潮：'):
                        info['stage'] = line.split('：', 1)[1].strip()
                    elif line.startswith('- 板块阶段：'):
                        info['stage'] = line.split('：', 1)[1].strip()
                boards[name] = info
        for sector, stage in re.findall(r'^###\s+(.+?)（(主升|修复|轮动|分歧|退潮)）', text, flags=re.M):
            boards.setdefault(sector.strip(), {'name': sector.strip()})['stage'] = stage.strip()

        candidates = []
        seen = set()

        def add_candidate(item: dict):
            code = normalize_code(item.get('code'))
            if not code or code in seen:
                return
            item['code'] = code
            if item.get('sector') and item['sector'] in boards and not item.get('stage'):
                item['stage'] = boards[item['sector']].get('stage')
            candidates.append(item)
            seen.add(code)

        cand_m = re.search(r'## 3\. 个股筛选（.*?）\n(.*?)(?:\n## 4\.|\Z)', text, flags=re.S)
        if cand_m:
            section = cand_m.group(1)
            current_tier = None
            entries = re.split(r'\n### ', '\n' + section)
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue
                lines = entry.splitlines()
                header = lines[0].replace('### ', '').strip()
                if header.startswith('A层'):
                    current_tier = 'A'
                elif header.startswith('B层'):
                    current_tier = 'B'
                elif header.startswith('C层'):
                    current_tier = 'C'
                symbol = parse_symbol(header)
                if symbol.get('code'):
                    item = {'name': symbol['name'], 'code': symbol['code'], 'tier': current_tier}
                    for line in lines[1:]:
                        if line.startswith('- 所属板块：'):
                            item['sector'] = line.split('：', 1)[1].strip()
                        elif line.startswith('- 板块地位：'):
                            item['role'] = line.split('：', 1)[1].strip()
                        elif line.startswith('- 趋势结构：'):
                            item['trend'] = line.split('：', 1)[1].strip()
                        elif line.startswith('- 是否符合“低位趋势成形 + 催化 + 承接”：'):
                            item['fit'] = line.split('：', 1)[1].strip()
                        elif line.startswith('- 明日关注点：'):
                            item['watch'] = line.split('：', 1)[1].strip()
                    add_candidate(item)
                else:
                    m = re.match(r'-\s*([^（]+)（(\d{6})[，,）]', header)
                    if m:
                        add_candidate({'name': m.group(1).strip(), 'code': m.group(2), 'tier': current_tier})

                for line in lines[1:]:
                    bullet = line.strip()
                    m = re.match(r'-\s*([^（\n]+)（(\d{6})(?:，([^，）]+))?(?:，([^，）]+))?(?:，([^，）]+))?', bullet)
                    if not m:
                        continue
                    item = {
                        'name': m.group(1).strip(),
                        'code': m.group(2),
                        'tier': current_tier,
                    }
                    if m.group(3):
                        item['sector'] = m.group(3).strip()
                    if m.group(4):
                        item['stage'] = m.group(4).strip()
                    if m.group(5):
                        item['role'] = m.group(5).strip()
                    add_candidate(item)

        for name, code in re.findall(r'^##\s+候选：([^（\n]+)（(\d{6})）', text, flags=re.M):
            sec_m = re.search(rf'^##\s+候选：{re.escape(name)}（{code}）\n(.*?)(?=^##\s+(?:候选|组合级结论)|\Z)', text, flags=re.M | re.S)
            item = {'name': name.strip(), 'code': code}
            if sec_m:
                body = sec_m.group(1)
                for line in body.splitlines():
                    line = line.strip()
                    if line.startswith('- 所属板块：'):
                        part = line.split('：', 1)[1]
                        bits = [x.strip() for x in part.split('；') if x.strip()]
                        if bits:
                            item['sector'] = bits[0]
                        for bit in bits[1:]:
                            if '板块阶段' in bit:
                                item['stage'] = bit.split('：', 1)[-1].strip()
                    elif line.startswith('- 趋势判断：'):
                        item['trend'] = line.split('：', 1)[1].strip()
            add_candidate(item)
        return {'boards': boards, 'candidates': candidates}

    close_info = _parse_single(close_text)
    analysis_info = _parse_single(analysis_text)
    boards = dict(close_info['boards'])
    for sector, info in analysis_info['boards'].items():
        boards.setdefault(sector, {}).update({k: v for k, v in info.items() if v and not boards.get(sector, {}).get(k)})
        boards.setdefault(sector, {'name': sector})
    merged_candidates = {}
    for item in close_info['candidates'] + analysis_info['candidates']:
        code = normalize_code(item.get('code'))
        if not code:
            continue
        base = merged_candidates.get(code, {'code': code, 'name': item.get('name')})
        for key, value in item.items():
            if value and not base.get(key):
                base[key] = value
        if base.get('sector') and base['sector'] in boards and not base.get('stage'):
            base['stage'] = boards[base['sector']].get('stage')
        merged_candidates[code] = base
    return {'boards': boards, 'candidates': list(merged_candidates.values())}


def backfill_candidates_from_close_info(items: list[dict], close_info: dict):
    close_map = {c['code']: c for c in close_info.get('candidates', [])}
    out = []
    for item in items:
        base = close_map.get(item.get('code'), {})
        merged = {**base, **item}
        merged['sector'] = item.get('sector') or base.get('sector')
        merged['stage'] = item.get('stage') or base.get('stage')
        merged['role'] = item.get('role') or base.get('role')
        out.append(merged)
    return out


# ---------- market data ----------
def get_fund_table(kind: str):
    if FUND_CACHE[kind] is not None:
        return FUND_CACHE[kind]
    with suppress_output():
        df = adu.ak_call(ak.fund_lof_spot_em if kind == 'lof' else ak.fund_etf_spot_em, timeout=20, attempts=3)
    FUND_CACHE[kind] = df
    return df


def fetch_security_quote(code: str | None, name: str = ''):
    if not code:
        return {'code': code, 'name': name}
    code = normalize_code(code)
    try:
        if is_fund_like(code, name):
            for kind in ('lof', 'etf'):
                try:
                    df = get_fund_table(kind)
                    row = df[df['代码'].astype(str).str.zfill(6) == code]
                    if row.empty:
                        continue
                    r = row.iloc[0]
                    latest = safe_float(r.get('最新价'))
                    open_price = safe_float(r.get('开盘价'))
                    prev_close = safe_float(r.get('昨收'))
                    return {
                        'code': code,
                        'name': str(r.get('名称') or name or code),
                        'auction_price': latest,
                        'latest': latest,
                        'open': open_price or latest,
                        'prev_close': prev_close,
                        'pct': safe_float(r.get('涨跌幅')),
                        'high': safe_float(r.get('最高价')),
                        'low': safe_float(r.get('最低价')),
                        'buy1': None,
                        'sell1': None,
                        'buy1_vol': None,
                        'sell1_vol': None,
                        'source': f'fund_{kind}_spot_em',
                    }
                except Exception:
                    continue
        try:
            with suppress_output():
                df = adu.ak_call(ak.stock_bid_ask_em, symbol=code, timeout=15, attempts=3)
            kv = {str(k): v for k, v in zip(df['item'], df['value'])}
            latest = safe_float(kv.get('最新'))
            open_price = safe_float(kv.get('今开')) or latest
            prev_close = safe_float(kv.get('昨收'))
            if latest is not None:
                return {
                    'code': code,
                    'name': name or code,
                    'auction_price': latest,
                    'latest': latest,
                    'open': open_price,
                    'prev_close': prev_close,
                    'pct': safe_float(kv.get('涨幅')),
                    'high': safe_float(kv.get('最高')),
                    'low': safe_float(kv.get('最低')),
                    'avg': safe_float(kv.get('均价')),
                    'volume_ratio': safe_float(kv.get('量比')),
                    'buy1': safe_float(kv.get('buy_1')),
                    'sell1': safe_float(kv.get('sell_1')),
                    'buy1_vol': safe_float(kv.get('buy_1_vol')),
                    'sell1_vol': safe_float(kv.get('sell_1_vol')),
                    'source': 'stock_bid_ask_em',
                }
        except Exception:
            pass
        quote = adu.fetch_quote_with_fallback(code)
        latest = safe_float(quote.get('latest'))
        open_price = safe_float(quote.get('open')) or latest
        prev_close = safe_float(quote.get('prev_close'))
        return {
            'code': code,
            'name': quote.get('name') or name or code,
            'auction_price': latest,
            'latest': latest,
            'open': open_price,
            'prev_close': prev_close,
            'pct': safe_float(quote.get('pct') or quote.get('change_pct')),
            'high': safe_float(quote.get('high')),
            'low': safe_float(quote.get('low')),
            'avg': None,
            'volume_ratio': None,
            'buy1': safe_float(quote.get('buy1')),
            'sell1': safe_float(quote.get('sell1')),
            'buy1_vol': safe_float(quote.get('buy1_vol')),
            'sell1_vol': safe_float(quote.get('sell1_vol')),
            'source': quote.get('source'),
        }
    except Exception as exc:
        return {'code': code, 'name': name or code, 'error': str(exc)}


def fetch_index_snapshot():
    mapping = {
        'sh000001': ('000001', '上证指数'),
        'sz399001': ('399001', '深证成指'),
        'sz399006': ('399006', '创业板指'),
    }

    def _extract(df, code_map, latest_key='最新价', open_key='今开', prev_close_key='昨收', pct_key='涨跌幅'):
        out = []
        for full_code, (short_code, label) in code_map.items():
            row = df[df['代码'].astype(str) == short_code]
            if row.empty:
                row = df[df['代码'].astype(str) == full_code]
            if row.empty:
                row = df[df['名称'].astype(str) == label]
            if row.empty:
                continue
            r = row.iloc[0]
            out.append({
                'name': label,
                'latest': safe_float(r.get(latest_key) or r.get('最新点位') or r.get('收盘')),
                'open': safe_float(r.get(open_key)),
                'prev_close': safe_float(r.get(prev_close_key)),
                'pct': safe_float(r.get(pct_key)),
            })
        return out

    try:
        with suppress_output():
            df = adu.ak_call(ak.stock_zh_index_spot_em, timeout=20, attempts=3)
        rows = _extract(df, mapping)
        if len(rows) >= 3 and any(safe_float(x.get('open')) is not None for x in rows):
            return rows
    except Exception:
        pass

    try:
        with suppress_output():
            df = adu.ak_call(ak.stock_zh_index_spot_sina, timeout=20, attempts=3)
        rows = _extract(df, mapping)
        if len(rows) >= 3:
            return rows
    except Exception:
        pass

    db_rows = fetch_index_snapshot_from_db(mapping)
    if db_rows:
        return db_rows

    return []


def fetch_index_snapshot_from_db(mapping=None, target_date=None, asof_time=None):
    mapping = mapping or {
        'sh000001': ('000001', '上证指数'),
        'sz399001': ('399001', '深证成指'),
        'sz399006': ('399006', '创业板指'),
    }
    target_date = target_date or TODAY
    asof_time = asof_time or datetime.now(tz=CST)
    asof_text = asof_time.isoformat() if hasattr(asof_time, 'isoformat') else str(asof_time)
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        captured_at = cur.execute(
            """
            SELECT MAX(captured_at)
            FROM index_snapshots
            WHERE trade_date = ? AND captured_at <= ?
            """,
            (target_date, asof_text),
        ).fetchone()[0]
        if not captured_at:
            conn.close()
            return []
        rows = cur.execute(
            """
            SELECT index_code, index_name, latest_value, pct_change, raw_json
            FROM index_snapshots
            WHERE trade_date = ? AND captured_at = ?
            ORDER BY id ASC
            """,
            (target_date, captured_at),
        ).fetchall()
        conn.close()

        out = []
        for full_code, (_short_code, label) in mapping.items():
            match = None
            for row in rows:
                if str(row['index_code'] or '') == full_code or str(row['index_name'] or '') == label:
                    match = row
                    break
            if match is None:
                continue
            raw = json.loads(match['raw_json']) if match['raw_json'] else {}
            out.append({
                'name': label,
                'latest': safe_float(raw.get('最新价') or raw.get('最新点位') or match['latest_value']),
                'open': safe_float(raw.get('今开') or raw.get('开盘') or raw.get('open')),
                'prev_close': safe_float(raw.get('昨收') or raw.get('前收盘') or raw.get('prev_close')),
                'pct': safe_float(raw.get('涨跌幅') or match['pct_change']),
                'source': 'index_snapshots_db',
            })
        return out if len(out) >= 3 else []
    except Exception:
        return []


def get_board_table():
    if FUND_CACHE['board'] is not None:
        return FUND_CACHE['board']
    with suppress_output():
        df = adu.ak_call(ak.stock_board_industry_name_em, timeout=25, attempts=3)
    FUND_CACHE['board'] = df
    return df


def fetch_strong_boards_from_db(limit=6, target_date=None, asof_time=None):
    target_date = target_date or TODAY
    asof_time = asof_time or datetime.now(tz=CST)
    asof_text = asof_time.isoformat() if hasattr(asof_time, 'isoformat') else str(asof_time)
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        captured_at = cur.execute(
            """
            SELECT MAX(captured_at)
            FROM sector_snapshots
            WHERE trade_date = ? AND captured_at <= ?
            """,
            (target_date, asof_text),
        ).fetchone()[0]
        if not captured_at:
            conn.close()
            return []
        cur.execute(
            """
            SELECT sector_name, pct_change, up_count, down_count, leader_name, raw_json, captured_at
            FROM sector_snapshots
            WHERE trade_date = ? AND captured_at = ?
            ORDER BY id DESC
            LIMIT 80
            """,
            (target_date, captured_at),
        )
        ranked = []
        seen = set()
        for row in cur.fetchall():
            name = row['sector_name']
            canonical_name = normalize_sector_name(name)
            if not canonical_name or canonical_name in seen:
                continue
            seen.add(canonical_name)
            raw = json.loads(row['raw_json']) if row['raw_json'] else {}
            pct = safe_float(row['pct_change']) or 0
            up = int(safe_float(row['up_count']) or 0)
            down = int(safe_float(row['down_count']) or 0)
            leader_pct = safe_float(raw.get('领涨股票-涨跌幅')) or 0
            turnover = safe_float(raw.get('换手率')) or 0
            if up < down:
                continue
            score = pct * 12 + (up - down) * 0.8 + leader_pct * 2 + turnover
            ranked.append({
                'name': name,
                'canonical_name': canonical_name,
                'pct': pct,
                'turnover': turnover,
                'up': up,
                'down': down,
                'leader': row['leader_name'],
                'leader_pct': leader_pct,
                'score': score,
                'source': 'sector_snapshots_db',
                'captured_at': row['captured_at'],
            })
        conn.close()
        ranked.sort(key=lambda x: (x['score'], x['pct'], x['up']), reverse=True)
        return ranked[:limit]
    except Exception:
        return []


def fetch_strong_boards(limit=6):
    try:
        df = get_board_table().copy()
    except Exception:
        return fetch_strong_boards_from_db(limit=limit)
    df['pct_num'] = df['涨跌幅'].apply(safe_float).fillna(0)
    df['up_num'] = df['上涨家数'].apply(safe_float).fillna(0)
    df['down_num'] = df['下跌家数'].apply(safe_float).fillna(0)
    df['leader_pct_num'] = df['领涨股票-涨跌幅'].apply(safe_float).fillna(0)
    df['turnover_num'] = df['换手率'].apply(safe_float).fillna(0)
    df = df[(df['up_num'] >= df['down_num']) & ((df['pct_num'] >= 1.0) | (df['leader_pct_num'] >= 8.0))].copy()
    df['强度分'] = (
        df['pct_num'] * 12
        + (df['up_num'] - df['down_num']) * 0.8
        + df['leader_pct_num'] * 2
        + df['turnover_num']
    )
    df = df.sort_values(['强度分', 'pct_num', 'up_num'], ascending=False)
    boards = []
    seen = set()
    for _, row in df.iterrows():
        name = str(row['板块名称'])
        canonical_name = normalize_sector_name(name)
        if not canonical_name or canonical_name in seen:
            continue
        seen.add(canonical_name)
        boards.append({
            'name': name,
            'canonical_name': canonical_name,
            'pct': safe_float(row['涨跌幅']),
            'turnover': safe_float(row['换手率']),
            'up': int(safe_float(row['上涨家数']) or 0),
            'down': int(safe_float(row['下跌家数']) or 0),
            'leader': str(row['领涨股票']),
            'leader_pct': safe_float(row['领涨股票-涨跌幅']),
            'score': safe_float(row['强度分']),
        })
        if len(boards) >= limit:
            break
    return boards or fetch_strong_boards_from_db(limit=limit)


def get_board_constituents(board_name: str):
    if board_name in BOARD_CONS_CACHE:
        return BOARD_CONS_CACHE[board_name]
    with suppress_output():
        df = adu.ak_call(ak.stock_board_industry_cons_em, symbol=board_name, timeout=25, attempts=3)
    BOARD_CONS_CACHE[board_name] = df
    return df


def get_board_live_snapshot(board_name: str, existing_codes=None, max_rows=4):
    existing_codes = set(existing_codes or [])
    try:
        df = get_board_constituents(board_name).copy()
    except Exception:
        return []
    df['代码'] = df['代码'].astype(str).str.zfill(6)
    df['涨跌幅_num'] = df['涨跌幅'].apply(safe_float)
    df['成交额_num'] = df['成交额'].apply(safe_float)
    df['换手率_num'] = df['换手率'].apply(safe_float)
    df['今开_num'] = df['今开'].apply(safe_float)
    df['昨收_num'] = df['昨收'].apply(safe_float)
    df['最新价_num'] = df['最新价'].apply(safe_float)
    df = df[df['代码'].apply(is_main_board_code)]
    df = df.sort_values(['涨跌幅_num', '成交额_num', '换手率_num'], ascending=False)
    rows = []
    for idx, (_, row) in enumerate(df.iterrows()):
        code = row['代码']
        if code in existing_codes:
            continue
        rows.append({
            'board': board_name,
            'code': code,
            'name': str(row['名称']),
            'latest': safe_float(row['最新价']),
            'open': safe_float(row['今开']),
            'prev_close': safe_float(row['昨收']),
            'pct': safe_float(row['涨跌幅']),
            'amount': safe_float(row['成交额']),
            'turnover': safe_float(row['换手率']),
            'high': safe_float(row['最高']),
            'low': safe_float(row['最低']),
            'role': '龙头' if idx == 0 else ('中军' if idx <= 2 else '补涨'),
        })
        if len(rows) >= max_rows:
            break
    return rows


# ---------- position/candidate normalization ----------
def load_ledger_holdings():
    rows = ledger.load_snapshot_rows(TODAY)
    if rows:
        return rows, TODAY

    context_path = latest_holding_pnl_context_before_today()
    context = read_json(context_path) if context_path else {}
    if context and safe_float(context.get('holding_count')) == 0:
        trade_date = context.get('trade_date') or (context_path.parent.name if context_path else None)
        return [], trade_date

    latest_date, latest_rows = ledger.load_latest_snapshot_rows()
    if latest_rows:
        return latest_rows, latest_date
    return [], context.get('trade_date') if context else None


def load_holding_pnl_context_summary():
    path = latest_holding_pnl_context_before_today()
    context = read_json(path) if path else {}
    if not context:
        return None
    return {
        'trade_date': context.get('trade_date') or (path.parent.name if path else None),
        'summary': {
            'holdings_line': context.get('holdings_line') or '当前持仓记录：无。',
            'total_pnl': context.get('total_pnl'),
        },
        'source_path': str(path) if path else None,
    }


def normalize_holding_plan_rows(table_rows: list[dict]):
    rows = []
    for row in table_rows:
        symbol = parse_symbol(row.get('标的', ''))
        if not symbol.get('code'):
            continue
        band = parse_plan_band(row.get('计划委托区间', ''))
        tp = parse_tp(row.get('止盈1/止盈2', ''))
        sector = None
        role = None
        stage = None
        for note in row.get('_notes', []):
            m = re.search(r'：.+? / (.+?) / (.+?) / (.+?)；', note)
            if m:
                sector, stage, role = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                break
        rows.append({
            'name': symbol['name'],
            'code': symbol['code'],
            'priority': row.get('优先级', ''),
            'action_plan': row.get('动作', ''),
            'position_plan': row.get('计划仓位比例', ''),
            'trigger': row.get('触发条件', ''),
            'stop': safe_float(row.get('止损')),
            'tp1': tp['tp1'],
            'tp2': tp['tp2'],
            'buy_low': band['buy_low'],
            'buy_high': band['buy_high'],
            'breakout': band['breakout'],
            'sector': sector,
            'stage': stage,
            'role': role,
            'notes': row.get('_notes', []),
        })
    return rows


def merge_holding_plan_with_ledger(table_rows: list[dict], ledger_rows: list[dict], close_candidates: list[dict]):
    plan_map = {r['code']: r for r in normalize_holding_plan_rows(table_rows)}
    close_map = {c['code']: c for c in close_candidates}
    merged = []
    for row in ledger_rows:
        code = normalize_code(row.get('symbol'))
        name = str(row.get('name') or code)
        base = close_map.get(code, {})
        merged.append({
            'name': name,
            'code': code,
            'quantity': int(safe_float(row.get('quantity')) or 0),
            'avg_cost': safe_float(row.get('avg_cost')),
            'weight_text': row.get('weight_text') or '',
            **base,
            **plan_map.get(code, {
                'priority': '持仓',
                'action_plan': '按持仓保护原则处理',
                'position_plan': row.get('weight_text') or '按现有仓位',
                'trigger': '先看开盘 3-5 分钟能否守住均价/昨收，再决定减仓或做T',
                'stop': safe_float(row.get('avg_cost')),
                'tp1': None,
                'tp2': None,
                'buy_low': None,
                'buy_high': None,
                'breakout': None,
                'notes': [],
            }),
        })
    return merged


def normalize_candidate_rows(table_rows: list[dict], close_candidates: list[dict]):
    close_map = {c['code']: c for c in close_candidates}
    rows = []
    for row in table_rows:
        symbol = parse_symbol(row.get('标的', ''))
        if not symbol.get('code'):
            continue
        band = parse_plan_band(row.get('计划委托区间', ''))
        tp = parse_tp(row.get('止盈1/止盈2', ''))
        sector = None
        role = None
        stage = None
        for note in row.get('_notes', []):
            m = re.search(r'：.+? / (.+?) / (.+?) / (.+?)；', note)
            if m:
                sector, stage, role = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                break
        item = close_map.get(symbol['code'], {}).copy()
        item.update({
            'name': symbol['name'],
            'code': symbol['code'],
            'priority': row.get('优先级', ''),
            'buy_flag': row.get('是否买入', ''),
            'position_plan': row.get('计划仓位比例', ''),
            'probe_plan': row.get('首笔试仓比例', ''),
            'add_condition': row.get('二次加仓条件', ''),
            'trigger': row.get('触发条件', ''),
            'stop': safe_float(row.get('止损')),
            'tp1': tp['tp1'],
            'tp2': tp['tp2'],
            'buy_low': band['buy_low'],
            'buy_high': band['buy_high'],
            'breakout': band['breakout'],
            'sector': sector or item.get('sector'),
            'stage': stage or item.get('stage'),
            'role': role or item.get('role'),
            'notes': row.get('_notes', []),
        })
        rows.append(item)
    return rows


def enrich_quotes(items: list[dict]):
    out = []
    for item in items:
        quote = fetch_security_quote(item.get('code'), item.get('name'))
        out.append({
            **item,
            'quote': quote,
            'auction_price': safe_float(quote.get('auction_price')),
            'open_price': safe_float(quote.get('open')),
            'prev_close': safe_float(quote.get('prev_close')),
            'latest': safe_float(quote.get('latest')),
            'open_pct': calc_open_pct(quote.get('open'), quote.get('prev_close')),
            'buy1': safe_float(quote.get('buy1')),
            'sell1': safe_float(quote.get('sell1')),
            'buy1_vol': safe_float(quote.get('buy1_vol')),
            'sell1_vol': safe_float(quote.get('sell1_vol')),
        })
    return out


# ---------- trading logic ----------
def calc_auction_strength(item):
    buy1_vol = safe_float(item.get('buy1_vol'))
    sell1_vol = safe_float(item.get('sell1_vol'))
    open_pct = safe_float(item.get('open_pct'))
    latest = safe_float(item.get('latest'))
    open_price = safe_float(item.get('open_price'))
    if buy1_vol is not None and sell1_vol is not None and sell1_vol > 0:
        ratio = buy1_vol / sell1_vol
        if ratio >= 1.8:
            return f'竞价偏强（买一/卖一量比约 {ratio:.2f}）'
        if ratio <= 0.7:
            return f'竞价偏弱（买一/卖一量比约 {ratio:.2f}）'
    if open_pct is not None:
        if open_pct >= 2:
            return f'竞价偏强（高开 {open_pct:.2f}%）'
        if open_pct <= -1.5:
            return f'竞价偏弱（低开 {open_pct:.2f}%）'
    if latest is not None and open_price is not None:
        if latest >= open_price:
            return '竞价中性偏强'
        return '竞价中性偏弱'
    return '竞价数据不足'


def derive_trade_plan(item, mode='candidate'):
    open_price = safe_float(item.get('open_price')) or safe_float(item.get('auction_price')) or safe_float(item.get('latest'))
    latest = safe_float(item.get('latest')) or open_price
    prev_close = safe_float(item.get('prev_close'))
    high = safe_float(item.get('quote', {}).get('high')) or max(v for v in [open_price, latest] if v is not None)
    low = safe_float(item.get('quote', {}).get('low')) or min(v for v in [open_price, latest] if v is not None)

    if mode == 'holding':
        support = item.get('buy_high') or item.get('buy_low') or open_price or prev_close or latest
        if support is None:
            return {}
        plan = {
            'buy_low': round_price(support * 0.995),
            'buy_high': round_price(support * 1.005),
            'stop': round_price(item.get('stop') or min(support * 0.985, (prev_close or support) * 0.985)),
            'sell1': round_price(item.get('tp1') or max(open_price or 0, latest or 0) * 1.02),
            'sell2': round_price(item.get('tp2') or (max(open_price or 0, latest or 0) * 1.04)),
        }
        return ase.validate_trade_levels(plan)

    if open_price is None and latest is None:
        return {}
    ref = latest or open_price
    if latest is not None and open_price is not None and latest > open_price:
        delta = latest - open_price
        buy_low = open_price + delta * 0.35
        buy_high = open_price + delta * 0.70
    elif latest is not None and open_price is not None and latest < open_price:
        support = min(open_price, latest)
        buy_low = support * 0.995
        buy_high = support * 1.003
    else:
        buy_low = ref * 0.995
        buy_high = ref * 1.005

    if item.get('buy_low') is not None:
        buy_low = max(buy_low, safe_float(item.get('buy_low')) * 0.995)
    if item.get('buy_high') is not None:
        buy_high = min(max(buy_high, buy_low), safe_float(item.get('buy_high')) * 1.01)

    breakout = safe_float(item.get('breakout'))
    if breakout is None:
        breakout = max(high or 0, latest or 0, open_price or 0)
    stop = safe_float(item.get('stop'))
    if stop is None:
        base = min(v for v in [open_price, prev_close, low] if v is not None)
        stop = base * 0.985
    sell1 = safe_float(item.get('tp1'))
    if sell1 is None:
        sell1 = max(breakout, (latest or ref) * 1.02)
    sell2 = safe_float(item.get('tp2'))
    if sell2 is None:
        sell2 = max(sell1 * 1.02, (latest or ref) * 1.04)
    plan = {
        'buy_low': round_price(buy_low),
        'buy_high': round_price(max(buy_low, buy_high)),
        'breakout': round_price(breakout),
        'stop': round_price(stop),
        'sell1': round_price(sell1),
        'sell2': round_price(sell2),
    }
    return ase.validate_trade_levels(plan)


def holding_action(item):
    plan = derive_trade_plan(item, mode='holding')
    open_pct = safe_float(item.get('open_pct'))
    open_price = safe_float(item.get('open_price'))
    avg_cost = safe_float(item.get('avg_cost'))
    latest = safe_float(item.get('latest'))
    if open_price is None:
        return '先观察', '报价不足，先不做第一笔。', plan
    if open_pct is not None and open_pct <= -2:
        return '先减仓防守', f'低开 {open_pct:.2f}%，若 9:30 后仍站不回昨收/均价，先减 20%-30%。', plan
    if plan.get('stop') is not None and open_price < plan['stop']:
        return '先处理风险', f'开盘已落在止损线下方，回抽不过 {fmt_num(plan["stop"])} 就继续减仓。', plan
    if plan.get('sell1') is not None and latest is not None and latest >= plan['sell1'] * 0.995:
        return '冲高兑现一部分', f'接近第一止盈位 {fmt_num(plan["sell1"])}，先考虑卖 20%-30%。', plan
    if avg_cost is not None and open_price < avg_cost:
        return '观察修复，不急补', f'低于成本 {fmt_num(avg_cost)}，先看能否修复站回，再决定是否做T。', plan
    return '按计划持有/做T', '先看 3-5 分钟承接，强则持有，弱则按预案减仓。', plan


def candidate_action(item):
    plan = derive_trade_plan(item, mode='candidate')
    open_pct = safe_float(item.get('open_pct'))
    open_price = safe_float(item.get('open_price'))
    latest = safe_float(item.get('latest'))
    buy_flag = item.get('buy_flag', '')
    if open_price is None:
        return '数据不足，先观察', '先不挂单，等开盘后补看报价。', plan
    if '先观察' in buy_flag:
        if open_pct is not None and open_pct < -1.5:
            return '观察为主，默认取消', '观察票又偏弱开，不抢反弹。', plan
        return '观察为主', '保持观察票定位，只做排序不做第一笔。', plan
    if open_pct is not None and open_pct >= 4:
        return '高开过多，不追', '高开 4% 以上默认不追，等回踩到买入区才考虑。', plan
    if open_pct is not None and open_pct <= -2:
        return '弱于预期，暂不买', '低开过深，不做抄底单，只保留弱转强观察。', plan
    if latest is not None and plan.get('buy_low') is not None and latest <= plan['buy_high'] * 1.003:
        return '可列入首笔观察', f'若 9:30 后回踩 {fmt_range(plan["buy_low"], plan["buy_high"])} 获得承接，可考虑首笔。', plan
    if latest is not None and plan.get('breakout') is not None and latest >= plan['breakout'] * 0.995:
        return '接近突破位，等确认', f'只有放量站稳 {fmt_num(plan["breakout"])} 上方才试仓。', plan
    return '按计划观察', '先看板块是否继续共振，再等价格回到买入区。', plan


def score_new_candidate(item, board_rank_map):
    pct = safe_float(item.get('pct')) or 0
    turnover = safe_float(item.get('turnover')) or 0
    amount = safe_float(item.get('amount')) or 0
    open_pct = calc_open_pct(item.get('open'), item.get('prev_close')) or 0
    board_bonus = max(0, 6 - board_rank_map.get(item.get('board'), 99)) * 3
    role_bonus = {'龙头': 6, '中军': 4, '补涨': 2}.get(item.get('role'), 0)
    return pct * 8 + turnover + amount / 1e8 * 0.4 + open_pct * 2 + board_bonus + role_bonus


def pick_new_candidates(strong_boards, existing_codes, close_info, max_count=4):
    board_rank_map = {b['name']: idx + 1 for idx, b in enumerate(strong_boards)}
    pool = []
    close_map = {c['code']: c for c in close_info.get('candidates', [])}
    for board in strong_boards[:4]:
        for item in get_board_live_snapshot(board['name'], existing_codes=existing_codes, max_rows=5):
            code = item['code']
            if code in existing_codes or not is_main_board_code(code):
                continue
            base = close_map.get(code, {})
            merged = {
                **base,
                **item,
                'sector': item.get('board'),
                'stage': close_info.get('boards', {}).get(item.get('board'), {}).get('stage', '待确认'),
            }
            merged['score'] = score_new_candidate(merged, board_rank_map)
            filt = ase.candidate_hard_filter(merged, {'close': merged.get('latest')}, market_phase=merged.get('stage'))
            merged['filter'] = filt
            merged['tier'] = filt['tier']
            if filt['tier'] == 'C':
                continue
            pool.append(merged)
    dedup = {}
    for item in sorted(pool, key=lambda x: (x.get('tier') != 'A', -x['score'])):
        dedup.setdefault(item['code'], item)
    return list(dedup.values())[:max_count]


# ---------- markdown rendering ----------
def build_opening_window_warning(now=None, target_date=None):
    """Return a warning when the 09:26 report is generated outside 09:25-09:35."""
    now = now or datetime.now(tz=CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    else:
        now = now.astimezone(CST)

    window_date = now.date()
    if target_date:
        try:
            window_date = datetime.fromisoformat(str(target_date)).date()
        except ValueError:
            window_date = now.date()
    window_start = datetime.combine(window_date, datetime.min.time(), tzinfo=CST).replace(hour=9, minute=25)
    window_end = datetime.combine(window_date, datetime.min.time(), tzinfo=CST).replace(hour=9, minute=35)

    if now < window_start:
        return '⚠️ 本报告生成时间早于开盘操作窗口，部分竞价/开盘数据可能尚未完整，仅供预检查。'
    if now > window_end:
        return '⚠️ 本报告生成时间已超过开盘操作窗口，价格可能不是集合竞价/开盘阶段数据，仅供复核。'
    return ''


def build_opening_report_metadata(now=None, target_date=None, window_warning=''):
    """Build a compact top-of-report data timestamp metadata block."""
    now = now or datetime.now(tz=CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    else:
        now = now.astimezone(CST)

    data_date = target_date or now.date().isoformat()
    warning = window_warning or ''
    if '早于开盘操作窗口' in warning:
        window_status = '是（早于开盘操作窗口）'
    elif '超过开盘操作窗口' in warning:
        window_status = '是（超过开盘操作窗口）'
    else:
        window_status = '否'

    lines = [
        f'> 数据日期：{data_date}',
        f'> 生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}',
        '> 报告类型：09:26 开盘操作表',
        '> 运行窗口：09:25 - 09:35',
        f'> 是否超出窗口：{window_status}',
        '> 行情数据说明：使用当前任务可获得的开盘/竞价阶段数据',
        '> 数据完整性：正常',
        '> 备注：本报告用于开盘阶段复核，不构成买卖建议',
    ]
    return '\n'.join(lines)


def build_market_strategy(indices, strong_boards, opening_context, opening_text):
    sentiment = opening_context.get('sentiment') or '中性'
    opening_strategy = parse_heading_value(opening_text, '今日总策略') or '先看强板块与开盘承接'
    max_pos = parse_heading_value(opening_text, '建议最大总仓位') or '20%-40%'
    sh = next((x for x in indices if x['name'] == '上证指数'), None)
    if sh and safe_float(sh.get('pct')) is not None and safe_float(sh['pct']) <= -0.8:
        opening_strategy = '指数偏弱，降级为轻仓试错或空仓等待'
        max_pos = '10%-20%'
    elif strong_boards and (safe_float(strong_boards[0].get('pct')) or 0) >= 2 and sentiment != '偏谨慎':
        opening_strategy = '强板块开盘有延续，可围绕最强方向做回踩确认，不追一致性高开的后排'
    return opening_strategy, max_pos


def render_quote_line(item):
    return (
        f"{item['name']}（{item['code']}） | 竞价 {fmt_num(item.get('auction_price'))} | 今开 {fmt_num(item.get('open_price'))} | "
        f"昨收 {fmt_num(item.get('prev_close'))} | 开盘幅度 {fmt_pct(item.get('open_pct'))} | {calc_auction_strength(item)}"
    )


def action_bucket(action: str, note: str = '', mode: str = 'candidate'):
    text = f"{action} {note}"
    if any(k in text for k in ['先减仓', '先处理风险', '冲高兑现']):
        return '立即卖/减仓', '已有风险或兑现条件，优先先卖后看。'
    if any(k in text for k in ['高开过多，不追', '观察为主，默认取消', '弱于预期，暂不买']):
        return '放弃/取消', '当前不满足出手条件，今天不做这笔。'
    if any(k in text for k in ['观察为主', '按计划观察', '数据不足，先观察', '先观察']):
        return '只观察', '先不下单，只保留盯盘。'
    if any(k in text for k in ['接近突破位，等确认', '按计划持有/做T', '观察修复，不急补']):
        return '等回踩/等确认', '等价格或量能确认后再执行。'
    if any(k in text for k in ['可列入首笔观察']):
        return '可小仓试单', '可进观察名单，回踩确认后试第一笔。'
    if mode == 'holding':
        return '持有观察', '先看是否需要做T或减仓。'
    return '只观察', '默认先观察。'


def build_priority_summary(holdings, planned_candidates, new_candidates):
    lines = ['## 0. 一眼执行结论']
    bucket_groups = defaultdict(list)

    for item in holdings:
        action, note, _ = holding_action(item)
        bucket, desc = action_bucket(action, note, mode='holding')
        bucket_groups[bucket].append(f"{item['name']}（{item['code']}）")
    for item in planned_candidates:
        action, note, plan = candidate_action(item)
        bucket, desc = action_bucket(action, note, mode='candidate')
        price = fmt_range(plan.get('buy_low'), plan.get('buy_high')) if plan else '—'
        bucket_groups[bucket].append(f"{item['name']}（{item['code']}，{price}）")
    for item in new_candidates[:2]:
        tmp = {
            'name': item['name'], 'code': item['code'], 'open_price': item.get('open'), 'auction_price': item.get('latest'),
            'latest': item.get('latest'), 'prev_close': item.get('prev_close'), 'open_pct': calc_open_pct(item.get('open'), item.get('prev_close')),
            'breakout': None, 'stop': None, 'buy_low': None, 'buy_high': None, 'tp1': None, 'tp2': None,
            'quote': {'high': item.get('high'), 'low': item.get('low')}, 'buy_flag': '可试买',
        }
        action, note, plan = candidate_action(tmp)
        bucket, desc = action_bucket(action, note, mode='candidate')
        price = fmt_range(plan.get('buy_low'), plan.get('buy_high')) if plan else '—'
        bucket_groups[bucket].append(f"{item['name']}（{item['code']}，{price}）")

    order = ['立即卖/减仓', '可小仓试单', '等回踩/等确认', '只观察', '放弃/取消', '持有观察']
    has_content = False
    for bucket in order:
        items = bucket_groups.get(bucket) or []
        if not items:
            continue
        has_content = True
        lines.append(f"- {bucket}：" + '、'.join(items[:3]))
    if not has_content:
        lines.append('- 暂无明确优先动作，统一先观察。')
    lines.append('')
    return lines


def build_recommended_order_block(planned_candidates, new_candidates):
    def candidate_record(item, source):
        action, note, plan = candidate_action(item)
        bucket, bucket_note = action_bucket(action, note, mode='candidate')
        priority_map = {
            '可小仓试单': 0,
            '等回踩/等确认': 1,
            '只观察': 2,
            '放弃/取消': 3,
            '立即卖/减仓': 4,
            '持有观察': 5,
        }
        return {
            'name': item['name'],
            'code': item['code'],
            'source': source,
            'bucket': bucket,
            'action': action,
            'note': note,
            'plan': plan,
            'score': priority_map.get(bucket, 9),
        }

    rows = []
    for item in planned_candidates:
        rows.append(candidate_record(item, '原候选'))
    for item in new_candidates:
        tmp = {
            'name': item['name'],
            'code': item['code'],
            'open_price': item.get('open'),
            'auction_price': item.get('latest'),
            'latest': item.get('latest'),
            'prev_close': item.get('prev_close'),
            'open_pct': calc_open_pct(item.get('open'), item.get('prev_close')),
            'breakout': None,
            'stop': None,
            'buy_low': None,
            'buy_high': None,
            'tp1': None,
            'tp2': None,
            'quote': {'high': item.get('high'), 'low': item.get('low')},
            'buy_flag': '可试买',
        }
        rows.append(candidate_record(tmp, f"新候选/{item.get('board') or '未知板块'}"))

    rows.sort(key=lambda x: (x['score'], x['name']))
    lines = ['## 0.1 推荐下单顺序']
    actionable = rows[:4]
    if not actionable:
        lines.append('- 暂无明确推荐下单顺序，今天统一先观察。')
        lines.append('')
        return lines

    for idx, row in enumerate(actionable, start=1):
        plan = row['plan'] or {}
        buy_zone = fmt_range(plan.get('buy_low'), plan.get('buy_high'))
        breakout = fmt_num(plan.get('breakout'))
        stop = fmt_num(plan.get('stop'))
        if row['bucket'] == '可小仓试单':
            verb = f'{buy_zone} 附近回踩试单'
        elif row['bucket'] == '等回踩/等确认':
            verb = f'{buy_zone} 附近低吸，不追高，突破 {breakout} 再确认'
        elif row['bucket'] == '只观察':
            verb = f'先观察，不下单；只有回到 {buy_zone} 且承接转强再评估'
        elif row['bucket'] == '放弃/取消':
            verb = '今天先放弃，不做这笔'
        else:
            verb = row['action']
        lines.append(
            f"{idx}. {row['name']}（{row['code']}，{row['source']}）：{verb}；止损参考 {stop}。"
        )
    lines.append('')
    return lines


def build_markdown(opening_context, opening_text, close_info, indices, strong_boards, holdings, planned_candidates, new_candidates, ledger_summary, ledger_trade_date, now=None, target_date=None):
    now = now or datetime.now(tz=CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    else:
        now = now.astimezone(CST)
    now_str = now.strftime('%Y-%m-%d %H:%M')
    strategy_text, max_position_text = build_market_strategy(indices, strong_boards, opening_context, opening_text)
    focus = opening_context.get('focus') or []
    warning = build_opening_window_warning(now=now, target_date=target_date or TODAY)
    metadata = build_opening_report_metadata(now=now, target_date=target_date or TODAY, window_warning=warning)
    lines = [f'# A股 09:26 操作表 - {now_str}', '']
    if warning:
        lines.extend([warning, ''])
    if metadata:
        lines.extend([metadata, ''])
    lines.extend(build_priority_summary(holdings, planned_candidates, new_candidates))
    lines.extend(build_recommended_order_block(planned_candidates, new_candidates))

    lines.append('## 1. 市场环境与总策略')
    lines.append(f"- 盘前情绪基线：**{opening_context.get('sentiment') or '未知'}**")
    lines.append(f"- 09:26 市场总策略：**{strategy_text}**")
    lines.append(f"- 建议最大总仓位：**{max_position_text}**")
    if focus:
        lines.append('- 盘前重点盯：')
        for item in focus[:2]:
            lines.append(f'  - {item}')
    if ledger_summary:
        pnl = ledger_summary.get('summary', {}).get('total_pnl')
        lines.append(f"- 账本基线（{ledger_trade_date or ledger_summary.get('trade_date')}）：{ledger_summary.get('summary', {}).get('holdings_line', '当前持仓记录：无。')}；累计总盈亏 {fmt_num(pnl)}")
    lines.append('')

    lines.append('## 2. 大盘与开盘强势板块')
    if indices:
        for idx in indices:
            open_pct = calc_open_pct(idx.get('open'), idx.get('prev_close'))
            lines.append(f"- {idx['name']}：今开 {fmt_num(idx.get('open'))}，昨收 {fmt_num(idx.get('prev_close'))}，开盘幅度 {fmt_pct(open_pct)}，当前涨跌幅 {fmt_pct(idx.get('pct'))}")
    else:
        lines.append('- 指数数据抓取失败')
    if strong_boards:
        lines.append('- 开盘强势板块（按强度排序）：')
        for board in strong_boards[:5]:
            lines.append(
                f"  - {board['name']}：板块涨幅 {fmt_pct(board['pct'])}，上涨/下跌 {board['up']}/{board['down']}，换手 {fmt_num(board['turnover'])}%，领涨 {board['leader']} {fmt_pct(board['leader_pct'])}"
            )
    else:
        lines.append('- 强势板块数据抓取失败')
    lines.append('')

    lines.append('## 3. 持仓股开盘竞价与操作计划')
    if not holdings:
        lines.append('- 当前账本无持仓，今天先处理候选与新候选。')
    else:
        lines.append('| 标的 | 所属板块 | 竞价/开盘 | 是否立即动作 | 计划动作 | 明确价格 | 备注 |')
        lines.append('|---|---|---|---|---|---|---|')
        for item in holdings:
            action, note, plan = holding_action(item)
            bucket, _ = action_bucket(action, note, mode='holding')
            price_text = f"回补区 {fmt_range(plan.get('buy_low'), plan.get('buy_high'))}；止损 {fmt_num(plan.get('stop'))}；卖出 {fmt_range(plan.get('sell1'), plan.get('sell2'))}"
            sector_text = f"{item.get('sector') or '待补充'} / {item.get('stage') or '待确认'}"
            lines.append(
                f"| {item['name']}（{item['code']}） | {sector_text} | 竞价 {fmt_num(item.get('auction_price'))} / 今开 {fmt_num(item.get('open_price'))} / {fmt_pct(item.get('open_pct'))} | **{bucket}** | **{action}** | **{price_text}** | {note} |"
            )
    lines.append('')

    lines.append('## 4. 原候选股开盘竞价与今日操作计划')
    if not planned_candidates:
        lines.append('- 未从 09:00 开盘简报中提取到原候选股。')
    else:
        lines.append('| 标的 | 所属板块 | 竞价/开盘 | 是否立即动作 | 9:26 计划 | 明确买点 | 明确卖点/止损 |')
        lines.append('|---|---|---|---|---|---|---|')
        for item in planned_candidates:
            action, note, plan = candidate_action(item)
            bucket, bucket_note = action_bucket(action, note, mode='candidate')
            sector_text = f"{item.get('sector') or '待补充'} / {item.get('stage') or '待确认'} / {item.get('role') or '待确认'}"
            buy_text = f"{fmt_range(plan.get('buy_low'), plan.get('buy_high'))} 可试仓；突破 {fmt_num(plan.get('breakout'))} 再加" if plan else '—'
            sell_text = f"卖出 {fmt_range(plan.get('sell1'), plan.get('sell2'))}；止损 {fmt_num(plan.get('stop'))}"
            lines.append(
                f"| {item['name']}（{item['code']}） | {sector_text} | 竞价 {fmt_num(item.get('auction_price'))} / 今开 {fmt_num(item.get('open_price'))} / {fmt_pct(item.get('open_pct'))} / {calc_auction_strength(item)} | **{bucket}** | **{action}** | **{buy_text}** | **{sell_text}** |"
            )
            lines.append(f"> 执行备注：{note}；动作说明：{bucket_note}；原触发条件：{item.get('trigger') or '先看板块与承接'}。")
    lines.append('')

    lines.append('## 5. 新候选股（根据开盘强势板块动态补充）')
    if not new_candidates:
        lines.append('- 暂未识别出比原候选更强的新主板候选，今天先围绕原计划前排执行。')
    else:
        lines.append('| 新候选 | 来源板块 | 开盘表现 | 是否立即动作 | 角色 | 计划 | 明确价格 |')
        lines.append('|---|---|---|---|---|---|---|')
        for item in new_candidates:
            tmp = {
                'name': item['name'],
                'code': item['code'],
                'open_price': item.get('open'),
                'auction_price': item.get('latest'),
                'latest': item.get('latest'),
                'prev_close': item.get('prev_close'),
                'open_pct': calc_open_pct(item.get('open'), item.get('prev_close')),
                'breakout': None,
                'stop': None,
                'buy_low': None,
                'buy_high': None,
                'tp1': None,
                'tp2': None,
                'quote': {'high': item.get('high'), 'low': item.get('low')},
            }
            plan = derive_trade_plan(tmp, mode='candidate')
            action, note, _ = candidate_action(tmp | {'buy_flag': '可试买'})
            bucket, bucket_note = action_bucket(action, note, mode='candidate')
            lines.append(
                f"| {item['name']}（{item['code']}） | {item.get('board')} / {item.get('stage') or '待确认'} | 竞价 {fmt_num(item.get('latest'))} / 今开 {fmt_num(item.get('open'))} / {fmt_pct(calc_open_pct(item.get('open'), item.get('prev_close')))} / 当前 {fmt_pct(item.get('pct'))} | **{bucket}** | {item.get('role')} | **{action}** | **{fmt_range(plan.get('buy_low'), plan.get('buy_high'))} 买入；{fmt_range(plan.get('sell1'), plan.get('sell2'))} 卖出；止损 {fmt_num(plan.get('stop'))}** |"
            )
            lines.append(f"> 入选原因：来自开盘强势板块 {item.get('board')}，当前涨幅 {fmt_pct(item.get('pct'))}，成交额 {fmt_num((safe_float(item.get('amount')) or 0)/1e8)} 亿，适合纳入 09:26 新观察池。{note}；动作说明：{bucket_note}")
    lines.append('')

    lines.append('## 6. 今日执行顺序')
    lines.append('1. 先看大盘，再看最强板块，再看持仓/原候选/新候选，顺序不能反。')
    lines.append('2. 高开过多不追，必须等回踩到明确买入区再考虑首笔。')
    lines.append('3. 若价格没有进入买入区，即使逻辑看好也不下单。')
    lines.append('4. 若大盘继续走弱、强势板块掉队或个股跌破止损位，今天自动降级为轻仓试错或空仓等待。')
    lines.append('')
    return '\n'.join(lines) + '\n', strategy_text, max_position_text


def main():
    if adu.skip_cron_if_not_a_share_trading_day(TODAY, task='ashare-opening-action-table'):
        return
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    opening_context = read_json(OPENING_CONTEXT_PATH)
    opening_text, holding_table_rows, candidate_table_rows = load_opening_tables()
    expected_day_dir = preferred_review_day_dir()
    close_path = latest_close_summary_before_today()
    analysis_path = latest_position_analysis_before_today()
    close_info = parse_close_summary(close_path, analysis_path)

    ledger_rows, ledger_trade_date = load_ledger_holdings()
    ledger_summary = ledger.latest_report_summary(TODAY) or ledger.latest_report_summary(ledger_trade_date) or ledger.latest_report_summary()
    if not ledger_rows:
        holding_pnl_summary = load_holding_pnl_context_summary()
        if holding_pnl_summary and holding_pnl_summary.get('trade_date') == ledger_trade_date:
            ledger_summary = holding_pnl_summary

    holdings = enrich_quotes(merge_holding_plan_with_ledger(holding_table_rows, ledger_rows, close_info.get('candidates', [])))
    planned_candidates = enrich_quotes(normalize_candidate_rows(candidate_table_rows, close_info.get('candidates', [])))
    if not planned_candidates:
        fallback_candidates = backfill_candidates_from_close_info(
            parse_opening_brief_candidate_section(opening_text),
            close_info,
        )
        if not fallback_candidates:
            fallback_candidates = parse_opening_context_candidates(opening_context, close_info)
        planned_candidates = enrich_quotes(fallback_candidates)

    existing_codes = {x['code'] for x in holdings + planned_candidates if x.get('code')}
    indices = fetch_index_snapshot()
    strong_boards = fetch_strong_boards(limit=6)
    new_candidates = pick_new_candidates(strong_boards, existing_codes, close_info, max_count=4)

    markdown, strategy_text, max_position_text = build_markdown(
        opening_context, opening_text, close_info, indices, strong_boards, holdings, planned_candidates, new_candidates, ledger_summary, ledger_trade_date
    )
    NOTE_PATH.write_text(markdown, encoding='utf-8')

    summary = {
        'generated_at': datetime.now(tz=CST).isoformat(),
        'note_path': str(NOTE_PATH),
        'context_path': str(CONTEXT_PATH),
        'close_summary_path': str(close_path) if close_path else None,
        'position_analysis_path': str(analysis_path) if analysis_path else None,
        'source_validation': {
            'close': validate_review_source(close_path, expected_day_dir, 'close'),
            'analysis': validate_review_source(analysis_path, expected_day_dir, 'analysis'),
        },
        'candidate_count': len(planned_candidates),
        'new_candidate_count': len(new_candidates),
        'holding_count': len(holdings),
        'strong_boards': [b['name'] for b in strong_boards[:5]],
        'strategy': strategy_text,
        'max_position': max_position_text,
    }
    CONTEXT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
