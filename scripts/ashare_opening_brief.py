#!/usr/bin/env python3
import contextlib
import io
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import akshare as ak
import pandas as pd

import ashare_data_utils as adu
import ashare_strategy_engine as ase

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
DB_PATH = ROOT / 'ashare_monitor.db'
TODAY = datetime.now().astimezone().date().isoformat()
DAY_DIR = ROOT / TODAY
NOTE_PATH = DAY_DIR / 'opening-brief.md'
CONTEXT_PATH = DAY_DIR / 'opening-brief-context.json'
TRENDRADAR_ROOT = Path('/home/admin/Notes/market/trendradar-output')
CACHE_DIR = Path('/home/admin/.hermes/cache/ashare-opening-brief')
CST = datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8))
NOW = datetime.now(tz=CST)
PAST_12H = NOW - timedelta(hours=12)
PAST_24H = NOW - timedelta(hours=24)
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Hermes/1.0'

BOARD_NEWS_KEYWORDS = {
    '元件': ['pcb', '覆铜板', '服务器', 'ai硬件', '交换机', 'cpo', '光模块', '算力'],
    '印制电路板': ['pcb', '覆铜板', '服务器', 'ai硬件', '交换机', 'cpo', '光模块', '算力'],
    '锂电池': ['锂电', '电池', '储能', '新能源车', '固态电池', '磷酸铁锂'],
    '电池': ['锂电', '电池', '储能', '新能源车', '固态电池', '磷酸铁锂'],
    '电池化学品': ['电解液', '锂电', '电池', '储能', '新能源车'],
    '煤炭开采': ['煤炭', '动力煤', '煤价', '焦煤', '火电'],
    '动力煤': ['煤炭', '动力煤', '煤价', '火电'],
    '有色金属': ['黄金', '白银', '铜', '铝', '有色', '贵金属'],
    '贵金属': ['黄金', '白银', '贵金属'],
    '军工': ['军工', '卫星', '航天', '导弹', '国防'],
}


def ensure_dir():
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_json_cache(name: str, max_age_seconds: int):
    path = CACHE_DIR / name
    if not path.exists():
        return None
    try:
        if time.time() - path.stat().st_mtime > max_age_seconds:
            return None
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_json_cache(name: str, payload):
    path = CACHE_DIR / name
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def normalize_code(code: str | None) -> str | None:
    s = str(code or '').strip().lower()
    for prefix in ('sh', 'sz', 'bj'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    digits = ''.join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else None


def is_main_board_code(code: str | None) -> bool:
    code = str(code or '').strip()
    if code.startswith(('300', '301', '688', '689', '8', '4')):
        return False
    return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003'))


def is_fund_like(name: str, code: str | None = None) -> bool:
    name = str(name or '').upper()
    code = str(code or '')
    return 'ETF' in name or 'LOF' in name or code.startswith(('15', '16', '50', '51', '56', '58'))


def fmt_ts(dt: datetime | None) -> str:
    if not dt:
        return '未知时间'
    return dt.astimezone(CST).strftime('%m-%d %H:%M')


def safe_float(v):
    try:
        if v is None:
            return None
        if isinstance(v, str):
            v = v.replace(',', '').replace('%', '').strip()
            if not v:
                return None
        out = float(v)
        if pd.isna(out):
            return None
        return out
    except Exception:
        return None


def read_text(path: Path | None) -> str:
    return path.read_text(encoding='utf-8') if path and path.exists() else ''


def load_recent_sector_context_before_today():
    if not DB_PATH.exists():
        return {}, {}, {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        sector_rows = cur.execute(
            "SELECT sector_name, pct_change, net_inflow, raw_json FROM sector_snapshots WHERE trade_date < ? ORDER BY trade_date DESC, captured_at DESC, id DESC LIMIT 600",
            (TODAY,),
        ).fetchall()
        constituent_rows = cur.execute(
            "SELECT sector_name, code, name, role, is_sector_leader FROM sector_constituent_snapshots WHERE trade_date < ? ORDER BY trade_date DESC, captured_at DESC, id DESC LIMIT 12000",
            (TODAY,),
        ).fetchall()
        conn.close()
    except Exception:
        return {}, {}, {}

    sector_map = {}
    stage_map = {}
    role_map = {}
    for row in sector_rows:
        sector_name = str(row['sector_name'] or '').strip()
        if not sector_name or sector_name in stage_map:
            continue
        raw = {}
        try:
            raw = json.loads(row['raw_json'] or '{}')
        except Exception:
            raw = {}
        pct = safe_float(row['pct_change'])
        inflow = safe_float(row['net_inflow'])
        stage = '主升' if pct is not None and pct >= 3 else ('修复' if pct is not None and pct >= 1.5 else ('轮动' if pct is not None and pct > 0 else '退潮'))
        stage_map[sector_name] = stage
    for row in constituent_rows:
        code = normalize_code(row['code'])
        sector_name = str(row['sector_name'] or '').strip()
        if not code or not sector_name:
            continue
        sector_map.setdefault(code, sector_name)
        role = str(row['role'] or '').strip() or ('龙头' if int(safe_float(row['is_sector_leader']) or 0) else None)
        if role and code not in role_map:
            role_map[code] = role
    return sector_map, stage_map, role_map


def preferred_review_day_dir() -> Path | None:
    return adu.preferred_review_day_dir(ROOT, TODAY)


def pick_first_existing(*paths: Path | None) -> Path | None:
    return adu.pick_first_existing(*paths)


def latest_close_summary() -> Path | None:
    return adu.latest_review_file(ROOT, 'close-summary.md', TODAY, preferred_names=['close-summary.md', 'latest-summary.md'])


def latest_holding_summary() -> Path | None:
    day_dir = preferred_review_day_dir()
    if day_dir:
        analysis_files = sorted(day_dir.glob('持仓股与候选股分析-*.md'))
        if analysis_files:
            return analysis_files[-1]
        picked = pick_first_existing(day_dir / 'close-summary.md', day_dir / 'holding-pnl-1505.md')
        if picked:
            return picked
    for path in sorted(ROOT.glob('20*-*-*/持仓股与候选股分析-*.md'), reverse=True):
        text = read_text(path)
        if '当前持仓记录：' in text:
            return path
    for path in sorted(ROOT.glob('20*-*-*/close-summary.md'), reverse=True):
        text = read_text(path)
        if '当前持仓记录：' in text:
            return path
    return None


def latest_position_analysis() -> Path | None:
    return adu.latest_review_file(ROOT, '持仓股与候选股分析-*.md', TODAY)


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


def parse_candidates(text: str):
    items = []
    seen = set()

    def add_item(name: str, code: str | None):
        code = normalize_code(code)
        if not code or not is_main_board_code(code) or code in seen:
            return
        items.append({'name': str(name).strip(), 'code': code, 'group': '前一日候选股', 'asset_type': 'stock'})
        seen.add(code)

    sec_m = re.search(r'## 3\. 个股筛选（.*?）\n(.+?)(?:\n## 4\.|\Z)', text, flags=re.S)
    if sec_m:
        block = sec_m.group(1)
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith('- '):
                continue
            m = re.match(r'-\s*([\u4e00-\u9fa5A-Za-z0-9]+)（(\d{6})[，,）]', line)
            if m:
                add_item(m.group(1), m.group(2))

    for name, code in re.findall(r'^##\s+候选：([^（\n]+)（(\d{6})）', text, flags=re.M):
        add_item(name, code)

    return items


def parse_holdings(text: str):
    holdings = []
    m = re.search(r'当前持仓记录：(.+)', text)
    if not m:
        return holdings
    part = m.group(1).strip()
    if not part or '无' in part:
        return holdings
    pattern = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)\s*([0-9]+成)（成本\s*([0-9.]+)，\s*([0-9]+)股）')
    for name, weight, cost, shares in pattern.findall(part):
        holdings.append({
            'name': name.strip(),
            'weight': weight,
            'cost': safe_float(cost),
            'shares': int(shares),
            'group': '持仓股',
        })
    return holdings


def parse_candidate_sector_map(text: str):
    out = {}
    m = re.search(r'## 3\. 个股筛选（最重要）\n(.+?)\n## 4\.', text, flags=re.S)
    if not m:
        return out
    block = m.group(1)
    for sec in re.split(r'\n### ', block):
        sec = sec.strip()
        if not sec:
            continue
        lines = sec.splitlines()
        mm = re.match(r'([\u4e00-\u9fa5A-Za-z0-9]+)（(\d{6})）', lines[0].strip())
        if not mm:
            continue
        code = normalize_code(mm.group(2))
        sector = None
        for line in lines[1:]:
            m2 = re.search(r'所属板块：(.+)', line)
            if m2:
                sector = m2.group(1).strip()
                break
        out[code] = sector
    return out


def parse_board_stage_map(text: str):
    out = {}
    m = re.search(r'## 2\. 板块分析\n(.+?)\n## 3\.', text, flags=re.S)
    if m:
        block = m.group(1)
        pattern = re.compile(r'###\s+(.+?)\n(.*?)(?=\n###\s+|\Z)', flags=re.S)
        for sector, body in pattern.findall(block):
            sector = sector.strip()
            mm = re.search(r'板块是主升、分歧、修复还是退潮：(.+)', body)
            out[sector] = mm.group(1).strip() if mm else None
    for sector, stage in re.findall(r'^###\s+(.+?)（(主升|修复|轮动|分歧|退潮)）', text, flags=re.M):
        out[sector.strip()] = stage.strip()
    return out


def parse_plan_sectors(text: str):
    m = re.search(r'明天看好的 3 个板块：(.+)', text)
    if not m:
        return []
    raw = m.group(1)
    banned = {'数据缺失', '暂无', '无', '待补充', '待确认'}
    items = []
    seen = set()
    for token in re.split(r'[、，, ]+', raw):
        token = token.strip()
        if not token or token in banned or token in seen:
            continue
        seen.add(token)
        items.append(token)
    return items


def clean_md_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r'\*+', '', text)
    return text.strip()


def parse_level_list(value: str | None):
    text = clean_md_text(value) or ''
    if not text or '暂无' in text or '未' in text:
        return []
    nums = []
    for token in re.split(r'[、,，/ ]+', text):
        token = token.strip()
        if not token:
            continue
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', token)
        if not m:
            continue
        try:
            nums.append(float(m.group(1)))
        except Exception:
            continue
    return nums


def parse_range_pair(value: str | None):
    text = clean_md_text(value) or ''
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*[-~～]\s*([0-9]+(?:\.[0-9]+)?)', text)
    if not m:
        return (None, None)
    return (safe_float(m.group(1)), safe_float(m.group(2)))


def extract_analysis_lookup(text: str):
    lookup = {}
    if not text:
        return lookup
    pattern = re.compile(r'^##\s+(持仓|候选)：([^（\n]+)（(\d{6})）\n(.*?)(?=^##\s+(?:持仓|候选)：|\Z)', flags=re.M | re.S)
    for sec_type, name, code, body in pattern.findall(text):
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        info = {
            'section_type': sec_type,
            'name': name.strip(),
            'code': normalize_code(code),
            'sector': None,
            'stage': None,
            'trend': None,
            'short_support': None,
            'mid_support': None,
            'short_pressure': None,
            'mid_pressure': None,
            'pred_low': None,
            'pred_high': None,
            'rr': None,
            'strategy': None,
            'stop_loss_rule': None,
            'take_profit_rule': None,
        }
        for line in lines:
            if line.startswith('- 所属板块：'):
                part = line.split('：', 1)[1]
                bits = [x.strip() for x in part.split('；') if x.strip()]
                if bits:
                    info['sector'] = bits[0]
                for bit in bits[1:]:
                    if '板块阶段' in bit:
                        info['stage'] = bit.split('：', 1)[-1].strip()
            elif line.startswith('- 趋势判断：'):
                info['trend'] = clean_md_text(line.split('：', 1)[1])
            elif line.startswith('- 关键支撑位：'):
                levels = parse_level_list(line.split('：', 1)[1])
                if levels:
                    info['short_support'] = levels[0]
                if len(levels) > 1:
                    info['mid_support'] = levels[1]
            elif line.startswith('- 关键压力位：'):
                levels = parse_level_list(line.split('：', 1)[1])
                if levels:
                    info['short_pressure'] = levels[0]
                if len(levels) > 1:
                    info['mid_pressure'] = levels[1]
            elif line.startswith('- 明日走势预判：'):
                pred = line.split('：', 1)[1]
                low, high = parse_range_pair(pred)
                info['pred_low'] = low
                info['pred_high'] = high
            elif line.startswith('- 盈亏比参考：') or line.startswith('- 盈亏比：'):
                info['rr'] = clean_md_text(line.split('：', 1)[1])
            elif line.startswith('- 操作策略：'):
                info['strategy'] = clean_md_text(line.split('：', 1)[1])
            elif '放弃（减仓）' in line:
                cells = [clean_md_text(x) for x in line.strip('|').split('|')]
                if len(cells) >= 2:
                    info['take_profit_rule'] = cells[1]
            elif '失效（止损）' in line:
                cells = [clean_md_text(x) for x in line.strip('|').split('|')]
                if len(cells) >= 2:
                    info['stop_loss_rule'] = cells[1]
        lookup[info['code']] = info
    return lookup


def resolve_holding_codes(holdings):
    resolved = []
    stock_map = load_json_cache('stock_name_code_map.json', max_age_seconds=7 * 24 * 3600) or {}
    if not stock_map:
        try:
            stock_df = adu.ak_call(ak.stock_info_a_code_name, timeout=20, attempts=3)
            stock_map = {str(r['name']).strip(): str(r['code']).zfill(6) for _, r in stock_df.iterrows()}
            save_json_cache('stock_name_code_map.json', stock_map)
        except Exception:
            stock_map = {}
    fund_df = None
    fund_records = load_json_cache('fund_name_records.json', max_age_seconds=7 * 24 * 3600)
    for item in holdings:
        name = item['name']
        code = None
        asset_type = 'fund' if is_fund_like(name) else 'stock'
        if asset_type == 'stock':
            code = stock_map.get(name)
        else:
            try:
                if fund_df is None and fund_records is not None:
                    fund_df = pd.DataFrame(fund_records)
                if fund_df is None:
                    fund_df = adu.ak_call(ak.fund_name_em, timeout=20, attempts=3)
                    save_json_cache('fund_name_records.json', fund_df.to_dict(orient='records'))
                q = name.replace('（', '(').replace('）', ')').replace(' ', '')
                base = re.sub(r'(ETF|LOF|A|C|\(|\)|期货)', '', q, flags=re.I)
                sub = fund_df[fund_df['基金简称'].astype(str).str.contains(q, na=False)]
                if sub.empty and base:
                    sub = fund_df[fund_df['基金简称'].astype(str).str.contains(base, na=False)]
                if not sub.empty:
                    sub = sub.copy()
                    sub['score'] = 0
                    sub.loc[sub['基金简称'].astype(str).str.contains('LOF', case=False, na=False), 'score'] += 2
                    sub.loc[sub['基金简称'].astype(str).str.contains('A', case=False, na=False), 'score'] += 1
                    sub.loc[sub['基金简称'].astype(str).str.contains('C', case=False, na=False), 'score'] -= 1
                    sub = sub.sort_values(['score'], ascending=False)
                    code = str(sub.iloc[0]['基金代码']).zfill(6)
            except Exception:
                code = None
        resolved.append({**item, 'code': normalize_code(code), 'asset_type': asset_type})
    return resolved


def load_review_context():
    expected_day_dir = preferred_review_day_dir()
    close_path = latest_close_summary()
    hold_path = latest_holding_summary()
    analysis_path = latest_position_analysis()
    close_text = read_text(close_path)
    hold_text = read_text(hold_path)
    analysis_text = read_text(analysis_path)

    fallback_close_path = None
    fallback_close_text = ''
    if close_path is None or close_path.name != 'close-summary.md':
        close_files = sorted(ROOT.glob('20*-*-*/close-summary.md'))
        if close_files:
            fallback_close_path = close_files[-1]
            if fallback_close_path != close_path:
                fallback_close_text = read_text(fallback_close_path)

    review_text = '\n\n'.join([x for x in [close_text, analysis_text, fallback_close_text] if x])
    analysis_lookup = extract_analysis_lookup(analysis_text)
    db_sector_map, db_stage_map, db_role_map = load_recent_sector_context_before_today()

    candidate_sector_map = {}
    for src in [close_text, analysis_text, fallback_close_text]:
        if not src:
            continue
        candidate_sector_map.update({k: v for k, v in parse_candidate_sector_map(src).items() if v})
    board_stage_map = {}
    for src in [close_text, analysis_text, fallback_close_text]:
        if not src:
            continue
        board_stage_map.update({k: v for k, v in parse_board_stage_map(src).items() if v})
    plan_sectors = []
    for src in [close_text, analysis_text, fallback_close_text]:
        plan_sectors = parse_plan_sectors(src)
        if plan_sectors:
            break

    old_candidates = parse_candidates(review_text)
    holdings = resolve_holding_codes(parse_holdings(hold_text))

    for code, sector in db_sector_map.items():
        candidate_sector_map.setdefault(code, sector)
    for sector, stage in db_stage_map.items():
        board_stage_map.setdefault(sector, stage)
    for code, info in analysis_lookup.items():
        if info.get('sector'):
            candidate_sector_map.setdefault(code, info.get('sector'))
        if info.get('stage') and info.get('sector'):
            board_stage_map.setdefault(info.get('sector'), info.get('stage'))
    watchlist = []
    seen = set()
    for item in holdings + old_candidates:
        code = normalize_code(item.get('code'))
        key = code or item['name']
        if key in seen:
            continue
        seen.add(key)
        watchlist.append({**item, 'code': code, 'sector': candidate_sector_map.get(code)})
    return {
        'expected_day_dir': expected_day_dir,
        'close_path': close_path,
        'hold_path': hold_path,
        'analysis_path': analysis_path,
        'fallback_close_path': fallback_close_path,
        'source_validation': {
            'close': validate_review_source(close_path, expected_day_dir, 'close'),
            'holding': validate_review_source(hold_path, expected_day_dir, 'holding'),
            'analysis': validate_review_source(analysis_path, expected_day_dir, 'analysis'),
            'fallback_close': validate_review_source(fallback_close_path, expected_day_dir, 'fallback_close') if fallback_close_path else None,
        },
        'close_text': close_text,
        'hold_text': hold_text,
        'analysis_text': analysis_text,
        'analysis_lookup': analysis_lookup,
        'holdings': holdings,
        'old_candidates': old_candidates,
        'watchlist': watchlist,
        'candidate_sector_map': candidate_sector_map,
        'board_stage_map': board_stage_map,
        'candidate_role_map': db_role_map,
        'plan_sectors': plan_sectors,
    }


def fetch_rss(query: str, limit: int = 8):
    url = f'https://news.google.com/rss/search?q={quote(query)}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans'
    req = Request(url, headers={'User-Agent': USER_AGENT})
    xml_bytes = urlopen(req, timeout=20).read()
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall('./channel/item')[:limit]:
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        pub = item.findtext('pubDate') or ''
        source = ''
        source_el = item.find('{http://search.yahoo.com/mrss/}source')
        if source_el is not None and source_el.text:
            source = source_el.text.strip()
        try:
            pub_dt = parsedate_to_datetime(pub).astimezone(CST)
        except Exception:
            pub_dt = None
        items.append({'title': title, 'link': link, 'source': source, 'published_at': pub_dt, 'query': query, 'bucket': 'google'})
    return items


def impact_hint(title: str) -> str:
    t = title.lower()
    mapping = [
        (['fed', '利率', '加息', '降息', 'yield', '美债', '通胀', 'cpi', 'pce'], '可能影响全球风险偏好、外资流向和成长/高股息风格切换'),
        (['oil', '原油', '布伦特', 'wti', '天然气'], '可能影响能源、化工、航运与输入型通胀预期'),
        (['gold', '黄金', 'silver', '白银', 'copper', '铜', 'commodit'], '可能影响有色、资源股以及避险情绪'),
        (['hang seng', '恒生', 'hong kong', '港股'], '可能影响港股风险偏好，并对 A 股情绪形成映射'),
        (['nasdaq', 's&p', 'dow', '美股', '美国股市'], '可能影响今日开盘前的全球风险偏好判断'),
        (['中国', 'a股', '证监会', '国常会', '政策', '地产', '刺激'], '可能直接影响 A 股相关板块预期与开盘情绪'),
        (['tariff', '关税', '出口', '制裁', '贸易'], '可能影响出口链、制造业与跨境情绪'),
        (['业绩', '回购', '分红', '订单', '公告'], '可能影响相关个股与板块的催化强度'),
    ]
    for keys, hint in mapping:
        if any(k in t for k in keys):
            return hint
    return '可能影响今日 A 股开盘情绪与资金风格选择'


def parse_db_dt(value: str | None):
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=CST) if dt.tzinfo is None else dt.astimezone(CST)
        except Exception:
            continue
    return None


def read_trendradar_items():
    items = []
    finance_pat = re.compile(r'美股|纳指|标普|道指|港股|恒生|恒生科技|A股|沪指|深成指|证监会|央行|利率|降息|加息|黄金|白银|原油|铜|煤炭|电池|算力|芯片|军工|业绩|公告', re.I)
    finance_platforms = {'wallstreetcn-hot', 'cls-hot', 'thepaper'}
    finance_feeds = {'yahoo-finance', 'marketwatch-top', 'investing-com'}
    news_dbs = sorted((TRENDRADAR_ROOT / 'news').glob('*.db'), reverse=True)[:2]
    rss_dbs = sorted((TRENDRADAR_ROOT / 'rss').glob('*.db'), reverse=True)[:2]
    for db_path in news_dbs:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title, platform_id, url, updated_at FROM news_items ORDER BY updated_at DESC LIMIT 300"
            ).fetchall()
            conn.close()
            for row in rows:
                if row['platform_id'] not in finance_platforms:
                    continue
                dt = parse_db_dt(row['updated_at'])
                title = str(row['title'] or '').strip()
                if not title or not finance_pat.search(title):
                    continue
                if dt and dt < PAST_12H:
                    continue
                items.append({
                    'title': title,
                    'link': str(row['url'] or ''),
                    'source': f"TrendRadar-{row['platform_id']}",
                    'published_at': dt,
                    'bucket': 'trendradar',
                })
        except Exception:
            continue
    for db_path in rss_dbs:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title, feed_id, url, published_at, updated_at FROM rss_items ORDER BY updated_at DESC LIMIT 300"
            ).fetchall()
            conn.close()
            for row in rows:
                if row['feed_id'] not in finance_feeds:
                    continue
                title = str(row['title'] or '').strip()
                if not title or not finance_pat.search(title):
                    continue
                dt = parse_db_dt(row['published_at']) or parse_db_dt(row['updated_at'])
                if dt and dt < PAST_12H:
                    continue
                items.append({
                    'title': title,
                    'link': str(row['url'] or ''),
                    'source': f"TrendRadar-{row['feed_id']}",
                    'published_at': dt,
                    'bucket': 'trendradar',
                })
        except Exception:
            continue
    dedup = {}
    for item in items:
        key = re.sub(r'\s+', ' ', item['title']).strip()
        old = dedup.get(key)
        if old is None or (item.get('published_at') or datetime(1970, 1, 1, tzinfo=CST)) > (old.get('published_at') or datetime(1970, 1, 1, tzinfo=CST)):
            dedup[key] = item
    return list(dedup.values())


def news_score(item):
    title = item['title'].lower()
    score = 0
    for kw in ['fed', '利率', 'cpi', '非农', '关税', '制裁', '恒生', '港股', '中国', 'a股', '原油', '黄金', '白银', '铜', 'nasdaq', 's&p', 'dow', '政策', '证监会', '业绩', '公告']:
        if kw in title:
            score += 2
    if str(item.get('source', '')).startswith('TrendRadar'):
        score += 2
    if item.get('published_at') and item['published_at'] >= PAST_12H:
        score += 3
    return score


def sentiment_from_news(items):
    pos_words = ['上涨', '反弹', '刺激', '利好', '降息', '宽松', '新高', '回暖', '增长', '达成']
    neg_words = ['下跌', '暴跌', '紧张', '关税', '制裁', '收紧', '衰退', '通胀', '风险', '冲突']
    pos = sum(any(w in (x['title'] or '') for w in pos_words) for x in items)
    neg = sum(any(w in (x['title'] or '') for w in neg_words) for x in items)
    if neg - pos >= 2:
        return '偏谨慎'
    if pos - neg >= 2:
        return '偏乐观'
    return '中性'


def collect_news():
    queries = {
        'us': '美股 OR 纳斯达克 OR 标普500 OR 美联储 when:12h',
        'hk': '港股 OR 恒生指数 OR 香港股市 when:12h',
        'commodities': '原油 OR 黄金 OR 白银 OR 铜 OR 大宗商品 when:12h',
        'ashare': 'A股 OR 证监会 OR 中国资本市场 OR 港股通 OR 中国政策 when:12h',
    }
    items = []
    for bucket, query in queries.items():
        try:
            for row in fetch_rss(query):
                row['bucket'] = bucket
                items.append(row)
        except Exception:
            continue
        time.sleep(0.3)
    items.extend(read_trendradar_items())
    dedup = {}
    for item in items:
        key = re.sub(r'\s+', ' ', item['title']).strip()
        if key not in dedup or news_score(item) > news_score(dedup[key]):
            dedup[key] = item
    all_items = sorted(dedup.values(), key=lambda x: (news_score(x), x.get('published_at') or datetime(1970, 1, 1, tzinfo=CST)), reverse=True)
    top3 = all_items[:3]
    ashare_related = [x for x in all_items if x.get('bucket') in ('ashare', 'hk', 'trendradar') or any(k in x['title'].lower() for k in ['a股', '证监会', '港股', '恒生', '中国', '公告', '业绩'])][:8]
    sentiment = sentiment_from_news(top3)
    trendradar_count = sum(1 for x in all_items if str(x.get('source', '')).startswith('TrendRadar'))
    return {'all': all_items, 'top3': top3, 'ashare_related': ashare_related, 'sentiment': sentiment, 'trendradar_count': trendradar_count}


def collect_notice_map(codes):
    out = {c: [] for c in codes}
    date_list = sorted({NOW.strftime('%Y%m%d'), (NOW - timedelta(days=1)).strftime('%Y%m%d')})
    for d in date_list:
        try:
            df = adu.ak_call(ak.stock_notice_report, symbol='全部', date=d, timeout=25, attempts=3)
        except Exception:
            continue
        if df is None or df.empty or '代码' not in df.columns:
            continue
        df['代码'] = df['代码'].astype(str).str.zfill(6)
        df['公告日期'] = pd.to_datetime(df['公告日期'], errors='coerce')
        sub = df[df['代码'].isin(codes)].copy()
        for _, row in sub.iterrows():
            pub_dt = row['公告日期'].to_pydatetime().replace(tzinfo=CST) if pd.notna(row['公告日期']) else None
            if pub_dt and pub_dt >= PAST_24H:
                out[str(row['代码'])].append({
                    'title': str(row.get('公告标题') or ''),
                    'type': str(row.get('公告类型') or ''),
                    'time': pub_dt,
                    'url': str(row.get('网址') or ''),
                })
    return out


def collect_research_map(codes):
    # 逐票研报接口在 cron 场景下过慢，盘前任务优先保证准时交付；此处先降级为空。
    return {c: [] for c in codes}


def _bulk_spot_map(df):
    out = {}
    if df is None or df.empty:
        return out
    code_col = next((c for c in df.columns if '代码' in c), None)
    pct_col = next((c for c in df.columns if '涨跌幅' in c), None)
    if not code_col or not pct_col:
        return out
    work = df[[code_col, pct_col]].copy()
    work[code_col] = work[code_col].astype(str).str.zfill(6)
    for _, row in work.iterrows():
        out[str(row[code_col]).zfill(6)] = safe_float(row[pct_col])
    return out


def collect_price_anomalies(watchlist):
    out = {}
    stock_map = {}
    fund_map = {}
    try:
        stock_map.update(_bulk_spot_map(adu.ak_call(ak.stock_zh_a_spot_em, timeout=25, attempts=3)))
    except Exception:
        try:
            stock_map.update(_bulk_spot_map(adu.fetch_eastmoney_spot_df()))
        except Exception:
            pass
    for fetcher in (ak.fund_lof_spot_em, ak.fund_etf_spot_em):
        try:
            fund_map.update(_bulk_spot_map(adu.ak_call(fetcher, timeout=20, attempts=3)))
        except Exception:
            pass

    for item in watchlist:
        code = item.get('code')
        if not code:
            out[item['name']] = None
            continue
        key = str(code).zfill(6)
        value = stock_map.get(key) if item.get('asset_type') == 'stock' and is_main_board_code(code) else fund_map.get(key)
        if value is None:
            try:
                quote = adu.fetch_quote_with_fallback(key)
                value = safe_float(quote.get('pct') or quote.get('change_pct'))
            except Exception:
                value = None
        out[code] = value
    return out


def build_watch_anomalies(watchlist):
    # 盘前任务优先保证时效：暂不走逐票公告/研报/行情链路，避免 AkShare 慢接口导致超时。
    # 后续如需要恢复，可改成前一晚脚本预缓存后再在这里读取缓存。
    return []


def infer_news_sectors(news_items):
    sector_hits = {}
    for item in news_items:
        t = item['title'].lower()
        for sector, kws in BOARD_NEWS_KEYWORDS.items():
            if any(kw.lower() in t for kw in kws):
                sector_hits[sector] = sector_hits.get(sector, 0) + 1
    return sector_hits


def get_daily_df(code, days=120):
    code = normalize_code(code)
    symbol = ('sh' if str(code).startswith('6') else 'sz') + str(code)
    start = (NOW - timedelta(days=days * 2)).strftime('%Y%m%d')
    end = NOW.strftime('%Y%m%d')
    df = None
    try:
        df = adu.ak_call(ak.stock_zh_a_daily, symbol=symbol, start_date=start, end_date=end, adjust='qfq', timeout=25, attempts=3)
    except Exception:
        try:
            df = adu.fetch_hist_df_with_fallback(code, start, end, adjust='qfq')
        except Exception:
            df = None
    if df is None or df.empty:
        return None
    df = df.copy()
    for c in ['open', 'high', 'low', 'close', 'volume']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.dropna(subset=['close']).tail(days)


def calc_trend_metrics(df):
    if df is None or len(df) < 25:
        return None
    latest = df.iloc[-1].copy()
    close = float(latest['close'])
    ma5 = df['close'].rolling(5).mean().iloc[-1]
    ma10 = df['close'].rolling(10).mean().iloc[-1]
    ma20 = df['close'].rolling(20).mean().iloc[-1]
    high60 = float(df['high'].tail(min(60, len(df))).max())
    low60 = float(df['low'].tail(min(60, len(df))).min())
    pos60 = None if high60 == low60 else (close - low60) / (high60 - low60)
    pct1 = None
    if len(df) >= 2 and float(df.iloc[-2]['close']) != 0:
        pct1 = (close / float(df.iloc[-2]['close']) - 1) * 100
    return {
        'close': close,
        'ma5': safe_float(ma5),
        'ma10': safe_float(ma10),
        'ma20': safe_float(ma20),
        'high60': high60,
        'low60': low60,
        'pos60': pos60,
        'pct1': pct1,
    }


def infer_sector_roles(cons: pd.DataFrame):
    role_map = {}
    if cons is None or cons.empty:
        return role_map
    work = cons.copy()
    if '成交额' in work.columns:
        work['成交额'] = pd.to_numeric(work['成交额'], errors='coerce').fillna(0)
    else:
        work['成交额'] = 0
    if '涨跌幅' in work.columns:
        work['涨跌幅'] = pd.to_numeric(work['涨跌幅'], errors='coerce').fillna(0)
    else:
        work['涨跌幅'] = 0
    work['role_score'] = work['成交额'] * 0.65 + work['涨跌幅'] * 1e8 * 0.35
    work = work.sort_values(['role_score', '成交额', '涨跌幅'], ascending=False).reset_index(drop=True)
    for idx, row in work.iterrows():
        code = str(row['代码']).zfill(6)
        if idx == 0:
            role = '龙头'
        elif idx == 1 or row['成交额'] >= work['成交额'].quantile(0.7):
            role = '中军'
        elif idx <= 3 and row['涨跌幅'] >= work['涨跌幅'].median():
            role = '补涨'
        else:
            role = '跟风'
        role_map[code] = role
    return role_map


def build_execution_plan(item, metrics):
    stage = item.get('stage') or '待确认'
    role = item.get('role') or '待确认'
    close = metrics.get('close')
    ma5 = metrics.get('ma5')
    ma10 = metrics.get('ma10')
    ma20 = metrics.get('ma20')
    pos60 = metrics.get('pos60')
    focus = []
    confirm = []
    abandon = []

    if role == '龙头':
        focus.append('看是否继续带动板块共振，竞价与开盘后量能不能明显掉队')
        confirm.append('开盘后 15-30 分钟维持板块内相对强势，且分时回踩不破均价')
        abandon.append('高开低走且板块内率先转弱，或冲高后快速失去承接')
    elif role == '中军':
        focus.append('看回踩后是否有机构型承接，能否作为板块中军稳住趋势')
        confirm.append('回踩 MA5/MA10 附近能稳住并放量回拉，板块龙头未转弱')
        abandon.append('跌破 MA10 且无法快速修复，或板块龙头明显转弱')
    elif role == '补涨':
        focus.append('看是否出现弱转强，补涨节奏能否被市场承认')
        confirm.append('竞价不被核按钮，开盘后能在前高/关键均线附近放量站稳')
        abandon.append('开盘直接走弱或冲高回落，无法维持补涨强度')
    else:
        focus.append('看是否真正从跟风转为主动，避免把脉冲误判成机会')
        confirm.append('只有在板块整体加强且个股量价同步改善时再考虑纳入观察')
        abandon.append('板块不共振或个股始终弱于同板块龙头/中军')

    if close and ma5 and ma10:
        focus.append(f'优先盯住 {ma5:.2f}-{ma10:.2f} 一带是否有承接')
    if stage in ('修复', '轮动'):
        confirm.append('板块延续修复/轮动而不是一日游，早盘强度不能快速衰减')
    if stage == '主升':
        confirm.append('板块主升结构延续，龙头和中军同步走强')
    if stage == '退潮':
        abandon.append('板块仍处退潮阶段时，不做逆势接飞刀')
    if pos60 is not None and pos60 > 0.9:
        abandon.append('位置过高，若继续加速则盈亏比不足，不追高')
    if ma20 and close and close < ma20:
        abandon.append('重新跌回 MA20 下方且无修复，不纳入盘前执行名单')

    return {
        'plan_focus': '；'.join(dict.fromkeys(focus)) if focus else '看板块共振与分时承接是否匹配。',
        'confirm': '；'.join(dict.fromkeys(confirm)) if confirm else '板块走强且个股量价同步改善。',
        'abandon': '；'.join(dict.fromkeys(abandon)) if abandon else '若板块不共振或个股破位走弱则放弃。',
    }


def select_new_candidates(review_ctx, news_pack):
    old_candidates = [x for x in review_ctx['old_candidates'] if x.get('code')]
    old_sector_map = review_ctx['candidate_sector_map']
    board_stage_map = review_ctx['board_stage_map']
    candidate_role_map = review_ctx.get('candidate_role_map') or {}
    analysis_lookup = review_ctx.get('analysis_lookup') or {}
    sector_priority = {}
    for sector in review_ctx['plan_sectors']:
        sector_priority[sector] = sector_priority.get(sector, 0) + 4
    for sector in old_sector_map.values():
        if sector:
            sector_priority[sector] = sector_priority.get(sector, 0) + 3
    for sector, stage in board_stage_map.items():
        sector_priority[sector] = sector_priority.get(sector, 0) + {'主升': 4, '修复': 3, '轮动': 2, '分歧': 1, '退潮': -2}.get(stage or '', 0)
    for sector, hit in infer_news_sectors(news_pack['all']).items():
        sector_priority[sector] = sector_priority.get(sector, 0) + min(hit, 3)
    ranked_items = [item for item in sorted(sector_priority.items(), key=lambda kv: kv[1], reverse=True) if item[1] > 0]
    ranked_sectors = [x[0] for x in ranked_items[:4]]

    candidates = []
    for idx, item in enumerate(old_candidates):
        code = item.get('code')
        analysis = analysis_lookup.get(normalize_code(code), {})
        sector = item.get('sector') or old_sector_map.get(code) or analysis.get('sector')
        stage = board_stage_map.get(sector) or analysis.get('stage')
        role = item.get('role') or candidate_role_map.get(code) or analysis.get('role') or ('龙头' if stage == '主升' else '补涨')
        score = 0
        reasons = []
        if sector in ranked_sectors:
            score += max(1, 5 - ranked_sectors.index(sector))
            reasons.append(f'所属板块【{sector}】与今日优先方向一致')
        elif sector:
            reasons.append(f'来自前一日已识别板块【{sector}】')
        if stage in ('主升', '修复', '轮动'):
            score += {'主升': 3, '修复': 2, '轮动': 1}[stage]
            reasons.append(f'板块阶段明确为{stage}')
        elif stage:
            reasons.append(f'板块阶段为{stage}')
        if role in ('龙头', '中军', '补涨', '跟风'):
            score += {'龙头': 2, '中军': 1, '补涨': 1, '跟风': 0}.get(role, 0)
            reasons.append(f'板块角色定位为{role}')
        if idx < 6:
            score += 1
            reasons.append('来自前一日靠前候选池')
        if analysis.get('strategy'):
            reasons.append('结合盘后分析文件中的支撑/压力与策略结论')
        candidate = {
            'name': item['name'],
            'code': code,
            'sector': sector or '待补充',
            'stage': stage or '待确认',
            'role': role or '待确认',
            'score': score,
            'is_new': False,
            'pct1': None,
            'amount': None,
            'reasons': reasons[:4] or ['延续前一日复盘候选，等待开盘确认强弱'],
        }
        candidate.update(build_execution_plan(candidate, {'close': None, 'ma5': None, 'ma10': None, 'ma20': None, 'pos60': None}))
        candidates.append(candidate)

    dedup = {}
    for item in sorted(candidates, key=lambda x: x['score'], reverse=True):
        dedup.setdefault(item['code'], item)
    final = list(dedup.values())[:4]
    return {'priority_sectors': ranked_sectors, 'candidates': final}


def summarize_news_item(item):
    return f"{item['title']}；{impact_hint(item['title'])}"


def watch_reason(entry):
    reasons = []
    for flag in entry['flags']:
        if flag['type'] == '公告':
            reasons.append('近24小时有公告')
        elif flag['type'] == '涨跌幅':
            reasons.append(f"近一个交易日涨跌幅 {flag['value']:+.2f}%")
        elif flag['type'] == '研报':
            first = flag['items'][0]
            reasons.append(f"近24小时有新研报/评级（{first.get('institution') or '机构'} {first.get('rating') or ''}）")
    return '、'.join(reasons)


def build_focus_list(news_pack, anomalies, selection):
    focus = []
    alt = []
    for item in selection['candidates'][:2]:
        tag = '新候选' if item['is_new'] else '沿用候选'
        reason = '、'.join(item['reasons'][:2]) if item['reasons'] else '结合复盘与新闻延伸出的方向'
        focus.append(f"{item['name']}（{item['code']}，{item['sector']}，{item.get('role') or '待定'}）——{tag}，{reason}")
    for item in selection['candidates'][2:4]:
        reason = '、'.join(item['reasons'][:2]) if item['reasons'] else '可作为次级观察'
        alt.append(f"{item['name']}（{item['code']}，{item['sector']}，{item.get('role') or '待定'}）——{reason}")
    if len(alt) < 2:
        for entry in anomalies[:2]:
            text = f"{entry['name']}（{entry.get('code') or '无代码'}）——{watch_reason(entry)}"
            if text not in focus and text not in alt:
                alt.append(text)
                if len(alt) >= 2:
                    break
    avoid = []
    if news_pack['sentiment'] == '偏谨慎':
        avoid.append('不要因为隔夜消息直接追高高开股，先看开盘 5-15 分钟承接。')
    else:
        avoid.append('不要把利好新闻直接等同于买点，优先看是否有量价确认。')
    if selection['priority_sectors']:
        avoid.append(f"今天优先围绕 { '、'.join(selection['priority_sectors'][:2]) } 这类强方向，不要临盘被弱题材拉走。")
    else:
        avoid.append('若新闻分歧较大，先观察板块强弱排序，再决定是否开仓。')
    return {'focus': focus[:2], 'alt': alt[:2], 'avoid': avoid[:2]}


def choose_level(*values):
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def fmt_level(value):
    if value is None:
        return '待盘中确认'
    return f'{float(value):.2f}'


def fmt_range(a, b):
    aa = fmt_level(a)
    bb = fmt_level(b)
    if aa == '待盘中确认' and bb == '待盘中确认':
        return '待盘中确认'
    if aa == bb:
        return aa
    return f'{aa}~{bb}'


def calc_rr_text(entry, stop_loss, take_profit):
    if entry.get('rr') and entry.get('rr') != '待盘中确认':
        return entry['rr']
    close = choose_level(entry.get('close'))
    sl = choose_level(stop_loss)
    tp = choose_level(take_profit)
    if close is None or sl is None or tp is None:
        return '待盘中确认'
    risk = abs(close - sl)
    reward = abs(tp - close)
    if risk <= 0:
        return '待盘中确认'
    return f"1:{reward / risk:.2f}"


def get_security_metrics(item):
    code = normalize_code(item.get('code'))
    if item.get('asset_type') != 'stock' or not code or not is_main_board_code(code):
        return {}
    try:
        return calc_trend_metrics(get_daily_df(code)) or {}
    except Exception:
        return {}


def build_trade_levels(item, analysis=None, metrics=None):
    analysis = analysis or {}
    metrics = metrics or {}
    close = choose_level(metrics.get('close'))
    short_support = choose_level(analysis.get('short_support'), metrics.get('ma5'), metrics.get('ma10'))
    mid_support = choose_level(analysis.get('mid_support'), metrics.get('ma10'), metrics.get('ma20'), short_support)
    short_pressure = choose_level(analysis.get('short_pressure'), analysis.get('pred_high'))
    if short_pressure is None and close is not None and metrics.get('high60'):
        short_pressure = min(float(metrics['high60']), close * 1.08)
    mid_pressure = choose_level(analysis.get('mid_pressure'), metrics.get('high60'), short_pressure)
    stop_loss = choose_level(mid_support, short_support)
    if stop_loss is None and item.get('cost') is not None:
        stop_loss = float(item['cost']) * 0.95
    take_profit = choose_level(short_pressure, analysis.get('pred_high'))
    if take_profit is None and close is not None:
        take_profit = close * 1.06
    return {
        'close': close,
        'short_support': short_support,
        'mid_support': mid_support,
        'short_pressure': short_pressure,
        'mid_pressure': mid_pressure,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr': analysis.get('rr'),
        'trend': analysis.get('trend'),
        'strategy': analysis.get('strategy'),
        'stop_loss_rule': analysis.get('stop_loss_rule'),
        'take_profit_rule': analysis.get('take_profit_rule'),
    }


def build_opening_playbook(levels, mode='candidate'):
    close = choose_level(levels.get('close'))
    short_support = choose_level(levels.get('short_support'))
    mid_support = choose_level(levels.get('mid_support'), short_support)
    short_pressure = choose_level(levels.get('short_pressure'))
    mid_pressure = choose_level(levels.get('mid_pressure'), short_pressure)
    stop_loss = choose_level(levels.get('stop_loss'), mid_support, short_support)
    take_profit = choose_level(levels.get('take_profit'), short_pressure, mid_pressure)

    high_open_trigger = close * 1.02 if close is not None else None
    low_open_trigger = close * 0.985 if close is not None else None
    flat_low = close * 0.995 if close is not None else None
    flat_high = close * 1.005 if close is not None else None
    auction_upper = close * 1.03 if close is not None else None
    auction_lower = close * 0.99 if close is not None else None

    buy_zone = fmt_range(mid_support, short_support)
    sell_zone = fmt_range(short_pressure, take_profit)
    stop_loss_price = fmt_level(stop_loss)
    take_profit_zone = fmt_range(short_pressure, take_profit or mid_pressure)

    if mode == 'holding':
        high_open = (
            f"高开（高于昨收约 2%，即 {fmt_level(high_open_trigger)} 以上）时：不追买；"
            f"若开盘后站稳 {fmt_level(short_pressure)} 上方并继续放量，可先持有；"
            f"若冲高回落跌回均价下并失守 {fmt_level(short_pressure)}，优先卖出/减仓 30%-50%。"
        )
        flat_open = (
            f"平开（约 {fmt_range(flat_low, flat_high)}）时：观察 5-15 分钟；"
            f"回踩 {buy_zone} 不破可继续持有或做T；"
            f"反抽到 {sell_zone} 放量滞涨时分批卖出。"
        )
        low_open = (
            f"低开（低于昨收约 1.5%，即 {fmt_level(low_open_trigger)} 以下）时：先看是否守住 {buy_zone}；"
            f"若守住并拉回均价，可少量做T；若跌破 {stop_loss_price} 仍无承接，优先止损/降仓。"
        )
        auction_rule = f"竞价阈值：高于 {fmt_level(auction_upper)} 不追加仓；低于 {fmt_level(auction_lower)} 且弱于板块时，优先按减仓预案处理。"
        minute_3 = f"开盘 3 分钟：只看是否守住 {buy_zone} 与均价；若直接跌破 {stop_loss_price} 附近且无承接，先减 20%-30%。"
        minute_5 = f"开盘 5 分钟：若反抽不过均价/短压 {fmt_level(short_pressure)}，继续减仓；若回到均价上并缩量企稳，仅保留底仓。"
        minute_15 = f"开盘 15 分钟：若仍弱于板块，累计减到 50%-70%；若站稳 {fmt_level(short_pressure)}，可转为持有或做T。"
        first_sell_ratio = '第一次卖出比例：20%-30%（失守均价或弱于板块时）'
        second_sell_ratio = '第二次卖出比例：20%-40%（15分钟内仍未修复时）'
        cancel_rule = f"撤单/取消反手条件：若减仓后重新站稳 {fmt_level(short_pressure)} 且板块回到前排，暂停继续卖出。"
    else:
        high_open = (
            f"高开（高于昨收约 2%，即 {fmt_level(high_open_trigger)} 以上）时：原则上不追；"
            f"只有高开后回踩不破均价且重新站上 {fmt_level(short_pressure)}，才考虑首笔试仓；"
            f"若高开低走跌回均价下，放弃买入。"
        )
        flat_open = (
            f"平开（约 {fmt_range(flat_low, flat_high)}）时：优先等分时回踩；"
            f"在 {buy_zone} 获得承接并回拉时买入首笔；"
            f"冲到 {sell_zone} 不放量时先卖，不追第二笔。"
        )
        low_open = (
            f"低开（低于昨收约 1.5%，即 {fmt_level(low_open_trigger)} 以下）时：只做弱转强；"
            f"若低开后快速收回并站上均价，可小仓试错；"
            f"若继续压在 {fmt_level(short_support)} 下方，取消买入计划；已买则跌破 {stop_loss_price} 止损。"
        )
        auction_rule = f"竞价阈值：高开超过 {fmt_level(auction_upper)} 不追；低于 {fmt_level(auction_lower)} 且竞价排名掉出板块前排，则从买入名单剔除。"
        minute_3 = f"开盘 3 分钟：只做观察，不抢第一笔；若快速回踩 {buy_zone} 并出现回拉，可准备首单。"
        minute_5 = f"开盘 5 分钟：确认是否站上均价；站上均价且量能不弱时，首笔买入落在 {buy_zone}；否则继续等。"
        minute_15 = f"开盘 15 分钟：若已买且重新突破 {fmt_level(short_pressure)}，才允许二次加仓；若 15 分钟仍未站稳均价，取消当天计划。"
        first_sell_ratio = '第一次卖出比例：30%-50%（冲到短压不放量时）'
        second_sell_ratio = '第二次卖出比例：余下仓位分批止盈，或失守止损位全部退出'
        cancel_rule = f"撤单/取消计划条件：竞价不及预期、开盘 15 分钟未站稳均价，或跌破 {stop_loss_price} 后无修复。"

    if mode == 'holding':
        open_scenario_levels = [
            f"高开 0%~2%：先看是否站稳均价，站稳可持有；冲高回落则按减仓预案处理。",
            f"高开 2%~4%：不追涨；若不能站稳 {fmt_level(short_pressure)}，优先分批卖出。",
            f"高开 4% 以上：视作情绪高开，默认先兑现一部分，不在第一波追加。",
            f"平开（约 {fmt_range(flat_low, flat_high)}）：围绕 {buy_zone} 看承接，承接弱就减，承接强可持有/做T。",
            f"低开 0%~2%：先看 {buy_zone} 是否守住，守住再等回拉。",
            f"低开 2% 以上：若无法快速收回均价，优先止损或大幅降仓。",
        ]
        buy_type = '持仓处理型：以持有、减仓、做T为主，不主动追新仓。'
    else:
        open_scenario_levels = [
            f"高开 0%~2%：允许观察后参与，但不抢竞价，优先等回踩均价。",
            f"高开 2%~4%：只有回踩不破均价且重返 {fmt_level(short_pressure)} 才能试仓。",
            f"高开 4% 以上：默认不追，除非是板块绝对龙头且二次回封/强转强确认。",
            f"平开（约 {fmt_range(flat_low, flat_high)}）：最适合执行回踩买，首笔优先落在 {buy_zone}。",
            f"低开 0%~2%：只做弱转强，需先收回均价再考虑试仓。",
            f"低开 2% 以上：默认放弃，当天不做抄底。",
        ]
        buy_type = (
            f"买点类型分层：回踩买={buy_zone} 获得承接并回拉；"
            f"突破买=重新站上 {fmt_level(short_pressure)} 且放量；"
            f"弱转强买=低开后收回均价；"
            f"只观察不买=15分钟未站稳均价或竞价掉出板块前排。"
        )

    return {
        'high_open': high_open,
        'flat_open': flat_open,
        'low_open': low_open,
        'buy_zone': buy_zone,
        'sell_zone': sell_zone,
        'stop_loss_price': stop_loss_price,
        'take_profit_zone': take_profit_zone,
        'auction_rule': auction_rule,
        'minute_3': minute_3,
        'minute_5': minute_5,
        'minute_15': minute_15,
        'first_sell_ratio': first_sell_ratio,
        'second_sell_ratio': second_sell_ratio,
        'cancel_rule': cancel_rule,
        'open_scenario_levels': open_scenario_levels,
        'buy_type': buy_type,
    }


def holding_action_plan(item, sentiment='中性', analysis_lookup=None):
    name = item['name']
    code = item.get('code') or '无代码'
    cost = item.get('cost')
    weight = item.get('weight') or '未知仓位'
    asset_type = item.get('asset_type') or 'stock'
    analysis = (analysis_lookup or {}).get(normalize_code(code), {})
    levels = build_trade_levels(item, analysis=analysis, metrics=get_security_metrics(item))
    action = '观察等待'
    action_tag = '观察等待'
    buy_style = '等待回踩'
    sell_style = '冲高分批'
    buy_point = '仅在回踩后确认承接时考虑，不做开盘第一笔追单。'
    sell_point = '若开盘后冲高回落且分时承接转弱，优先减仓；若全天弱于所属方向，尾盘不留恋。'
    abandon = '若跌破昨日关键低点/成本区且无快速修复，放弃加仓，只保留防守。'
    note = f'当前记录仓位：{weight}。'
    if cost is not None:
        note += f' 参考成本：{cost:.3f}。'

    if asset_type == 'fund':
        action = '高抛低吸 / 继续持有'
        action_tag = '可做T'
        buy_style = '低吸'
        sell_style = '高抛'
        buy_point = '若盘中回踩前一交易日均价附近企稳，可小仓低吸；若直接高开拉升，不追。'
        sell_point = '若高开后冲高 2%-4% 但量价不能继续放大，可分批止盈；若全天维持强势则继续持有。'
        abandon = '若回落并跌破前一交易日低点，停止做T，优先控制回撤。'
    elif '5成' in weight or '6成' in weight or '7成' in weight:
        action = '偏卖出 / 降低追高冲动'
        action_tag = '偏卖'
        buy_style = '只减不加'
        sell_style = '冲高减仓'
        buy_point = '已有较重仓位，不建议再追高；只有在板块明显转强、回踩承接清晰时才考虑极小幅补仓。'
        sell_point = '若冲高但不能形成板块共振，优先分批减仓锁定主动权；若强势涨停趋势延续则持股观察。'
        abandon = '若高位震荡转弱或跌破日内关键承接位，停止幻想反包，先减仓。'
    else:
        action = '持有观察 / 有条件加减'
        action_tag = '中性偏观察'
        buy_style = '回踩低吸'
        sell_style = '冲高减仓'
        buy_point = '若回踩成本区或昨日均价附近不破，并出现放量回拉，可小幅低吸。'
        sell_point = '若冲高到前高附近明显放量滞涨，可先卖一部分；若开盘弱于板块，先减后看。'
        abandon = '若跌破成本区 3%-5% 且承接差，放弃补仓思路。'

    if sentiment == '偏谨慎':
        action_tag = '偏防守'
        buy_style = '等待确认'
        sell_style = '冲高兑现'
        buy_point = '情绪偏谨慎，买点后置到开盘 15-30 分钟后确认，不抢竞价。'
        sell_point = '若冲高不给持续性，优先兑现，少做格局。'

    decision = '继续持有观察'
    if action_tag in ('偏卖', '偏防守'):
        decision = '偏卖/减仓'
    elif action_tag == '可做T':
        decision = '继续持有，可做T'
    elif action_tag in ('中性偏观察', '观察等待'):
        decision = '先持有，回踩再看'

    close = levels.get('close')
    stop_loss_val = levels.get('stop_loss')
    take_profit_val = levels.get('take_profit')
    short_support_val = levels.get('short_support')
    short_pressure_val = levels.get('short_pressure')
    mid_support_val = levels.get('mid_support')
    mid_pressure_val = levels.get('mid_pressure')

    priority = 'A'
    position_plan = '底仓按原计划持有，盘中只做减仓/做T，不新增总仓位'
    order_mode = '观察后处理'
    trigger = '先看开盘 15 分钟承接，再决定减仓、做T或继续持有'
    order_band = f"减仓区：{fmt_range(short_pressure_val, take_profit_val)}；低吸回补区：{fmt_range(mid_support_val, short_support_val)}"
    fail_condition = abandon
    target_position_ratio = '维持现有仓位，不主动扩仓'
    first_probe_ratio = '0%，不新开仓'
    add_condition = '仅允许做T，不做主动加仓；除非板块与个股同时转强且回踩确认。'
    pre_open_action = '开盘前 5 分钟：核对持仓是否有隔夜利空/公告；若竞价明显低于预期，提前准备反抽减仓预案。'
    post_open_15m_action = '开盘后 15 分钟：先看持仓是否强于板块；若弱于板块且反抽无量，执行减仓；若回踩短支撑企稳，再考虑做T。'
    if decision == '偏卖/减仓':
        priority = 'A1'
        position_plan = '优先卖出 30%-50%；当天不主动加仓'
        order_mode = '反抽减仓单'
        trigger = '冲高但不能继续放量，或明显弱于所属板块时执行减仓'
        target_position_ratio = '降到原仓位的 50%-70%'
        first_probe_ratio = '0%，不试仓'
        add_condition = '无二次加仓；只有午后重新转强并收复短压，才允许回补不超过原减仓量的一半。'
        pre_open_action = '开盘前 5 分钟：若竞价弱于板块、且低开接近短支撑，挂好反抽减仓观察，不抢着砍在最低点。'
        post_open_15m_action = '开盘后 15 分钟：若反抽不过均价/短压就减仓 30%-50%；若直接跌破中线支撑，放弃幻想继续降仓。'
    elif decision == '继续持有，可做T':
        priority = 'A2'
        position_plan = '底仓不动；仅用 10%-20% 仓位做T'
        order_mode = '低吸高抛T单'
        trigger = '回踩均价/短支撑企稳低吸，冲高至压力区分批卖出'
        target_position_ratio = '总仓位维持不变；机动仓 10%-20% 做T'
        first_probe_ratio = '机动仓首笔 5%-10%'
        add_condition = '仅当第一次低吸后快速回到均价上方、且板块继续共振，才追加剩余 5%-10% 机动仓。'
        pre_open_action = '开盘前 5 分钟：先确认竞价是否平稳，若高开过多不追，优先等回踩。'
        post_open_15m_action = '开盘后 15 分钟：若回踩短支撑并快速收回，可用机动仓做T；若冲高到压力区放量滞涨，先卖出T仓。'
    else:
        priority = 'B'
        position_plan = '底仓持有；仅在确认承接后试加 5%-10%'
        order_mode = '回踩试单'
        trigger = '仅当回踩成本区或短支撑不破、并出现回拉时试加'
        target_position_ratio = '维持原仓位，最多额外试加 5%-10%'
        first_probe_ratio = '试加 5%'
        add_condition = '第一次试加后必须看到 15 分钟级别站稳均价并放量，才允许再补 5%。'
        pre_open_action = '开盘前 5 分钟：只记录竞价强弱，不预挂追涨单。'
        post_open_15m_action = '开盘后 15 分钟：如果回踩不破短支撑且重新放量站回均价，可试加；否则只持有观察。'

    rr_text = calc_rr_text(levels, stop_loss_val, take_profit_val)
    opening_playbook = build_opening_playbook(levels, mode='holding')

    return {
        'title': f'{name}（{code}）',
        'action': action,
        'action_tag': action_tag,
        'decision': decision,
        'buy_style': buy_style,
        'sell_style': sell_style,
        'buy_point': buy_point,
        'sell_point': sell_point,
        'abandon': abandon,
        'note': note,
        'stop_loss': fmt_level(stop_loss_val),
        'take_profit': fmt_level(take_profit_val),
        'tp1': fmt_level(take_profit_val),
        'tp2': fmt_level(mid_pressure_val or take_profit_val),
        'short_support': fmt_level(short_support_val),
        'short_pressure': fmt_level(short_pressure_val),
        'mid_support': fmt_level(mid_support_val),
        'mid_pressure': fmt_level(mid_pressure_val),
        'rr': rr_text,
        'trend': levels.get('trend') or '待确认',
        'holding_window': '以 1-5 个交易日应对为主，整体持有周期尽量不超过 20 天',
        'stop_loss_rule': levels.get('stop_loss_rule') or '跌破中线支撑且无法快速修复时执行止损',
        'take_profit_rule': levels.get('take_profit_rule') or '冲至短线压力附近放量滞涨时分批兑现',
        'strategy': levels.get('strategy') or '先处理持仓，再决定是否开新仓。',
        'priority': priority,
        'position_plan': position_plan,
        'order_mode': order_mode,
        'trigger': trigger,
        'order_band': order_band,
        'fail_condition': fail_condition,
        'target_position_ratio': target_position_ratio,
        'first_probe_ratio': first_probe_ratio,
        'add_condition': add_condition,
        'pre_open_action': pre_open_action,
        'post_open_15m_action': post_open_15m_action,
        'opening_playbook': opening_playbook,
    }


def candidate_action_plan(item, sentiment='中性', analysis_lookup=None):
    name = item['name']
    code = item.get('code') or '无代码'
    sector = item.get('sector') or '待确认'
    stage = item.get('stage') or '待确认'
    role = item.get('role') or '待确认'
    tag = '新候选' if item.get('is_new') else '沿用候选'
    analysis = (analysis_lookup or {}).get(normalize_code(code), {})
    metrics = get_security_metrics(item)
    levels = build_trade_levels(item, analysis=analysis, metrics=metrics)
    filt = ase.candidate_hard_filter(item, metrics, market_phase=stage)

    action = '观察等待'
    action_tag = '先观察'
    buy_style = '等待回踩'
    sell_style = '不及预期就撤'
    buy_point = '优先等回踩承接，不做无量直线追高。'
    sell_point = '若上车后冲高不封、板块跟风弱，及时止盈止损。'
    abandon = '若竞价弱、开盘后 15 分钟仍无主动性，直接放弃。'
    hard_summary = filt.get('summary') or '待进一步核验'

    if filt['tier'] == 'C':
        action = '放弃为主'
        action_tag = '放弃'
        buy_style = '不参与'
        sell_style = '无'
        buy_point = '不进入执行池，只保留记录。'
        sell_point = '无'
        abandon = '触发硬过滤失败：' + '、'.join(filt.get('hard_fail') or ['不满足执行条件'])
    elif filt['tier'] == 'B':
        action = '观察等待 / 回踩确认后再买'
        action_tag = '先观察'
        buy_style = '等回踩确认'
        sell_style = '弱修复撤退'
        buy_point = '仅作观察票，不给预挂单；只有盘中转强且重新满足A层条件时再升级。'
        sell_point = '若观察中发现板块掉队或量价不匹配，直接删除出手念头。'
        abandon = f'当前只在观察池：{hard_summary}'
    else:
        action = '回踩低吸 / 强转强观察'
        action_tag = '重点盯' if role in ('龙头', '中军') else '偏买'
        buy_style = '低吸 + 强转强'
        sell_style = '冲高止盈'
        buy_point = '若开盘后回踩均价或前一日强势区间上沿不破，可优先考虑低吸；若强转强放量突破，可轻仓试错。'
        sell_point = '若冲高后无法继续放量、板块龙头掉队，先卖后看。'
        abandon = f'一旦跌破关键支撑、板块共振消失或RR不再达标，就降级出A层。当前A层依据：{hard_summary}'

    if sentiment == '偏谨慎' and filt['tier'] == 'A':
        action_tag = '观察优先'
        buy_style = '等待确认'
        sell_style = '冲高兑现'
        buy_point = '情绪偏谨慎，即使在A层也只允许等开盘15分钟后的确认买点。'

    decision = '先观察'
    if filt['tier'] == 'A' and sentiment != '偏谨慎':
        decision = '可试买'
    elif action_tag == '观察优先':
        decision = '观察为主'
    elif filt['tier'] == 'C':
        decision = '放弃'

    stop_loss_val = levels.get('stop_loss')
    take_profit_val = levels.get('take_profit')
    short_support_val = levels.get('short_support')
    short_pressure_val = levels.get('short_pressure')
    mid_support_val = levels.get('mid_support')
    mid_pressure_val = levels.get('mid_pressure')

    priority = 'B'
    position_plan = '0%，先观察，不下单'
    order_mode = '观察单'
    trigger = '只看竞价、开盘 15 分钟强弱与板块共振，不急于出手'
    order_band = f"低吸区：{fmt_range(mid_support_val, short_support_val)}；突破确认区：{fmt_range(short_pressure_val, take_profit_val)}"
    fail_condition = abandon
    target_position_ratio = '0%，先观察'
    first_probe_ratio = '0%'
    add_condition = '不开仓，自然无二次加仓。'
    pre_open_action = '开盘前 5 分钟：只做竞价强弱排序，确认是否进入重点盯名单，不预挂追涨单。'
    post_open_15m_action = '开盘后 15 分钟：只观察板块共振、量能和是否站上均价，未确认前不下单。'
    if decision == '可试买':
        priority = 'A1' if action_tag == '重点盯' else 'A2'
        position_plan = '首仓 10%-15%；确认后最多加到 20%-30%'
        order_mode = '回踩限价单 / 突破跟随单'
        trigger = 'A层 + 板块共振 + 个股回踩不破或放量突破关键压力时试仓'
        target_position_ratio = '总计划仓位 20%-30%'
        first_probe_ratio = '首笔试仓 10%-15%'
        add_condition = '首笔成交后，只有在开盘后 15 分钟内量价继续走强、且回踩不破均价/短支撑时，才允许二次加到 20%-30%。'
        pre_open_action = '开盘前 5 分钟：重点看竞价是否高于板块平均、是否有量能放大；若高开过多不追，优先等回踩。'
        post_open_15m_action = '开盘后 15 分钟：优先执行首笔试仓；若首笔后继续放量、并站稳均价与短压，再做二次加仓；否则只保留试仓。'
    elif decision == '观察为主':
        priority = 'B1'
        position_plan = '0%-10%，仅极强确认时轻仓试错'
        order_mode = '确认后试单'
        trigger = '只有开盘后量价同步转强并重新满足A层标准时，才考虑极小仓试错'
        target_position_ratio = '0%-10%'
        first_probe_ratio = '首笔 5%'
        add_condition = '只有首笔后仍维持板块前三强、且个股分时回踩不破均价，才考虑补到 10%。'
    elif decision == '放弃':
        priority = 'C'
        position_plan = '0%，不下单'
        order_mode = '取消计划'
        trigger = '硬过滤失败，不执行'
        target_position_ratio = '0%'
        first_probe_ratio = '0%'
        add_condition = '无'

    rr_text = calc_rr_text(levels, stop_loss_val, take_profit_val)
    opening_playbook = build_opening_playbook(levels, mode='candidate')

    return {
        'title': f'{name}（{code}）',
        'meta': f'{tag} / {sector} / {stage} / {role}',
        'tier': filt['tier'],
        'hard_summary': hard_summary,
        'action': action,
        'action_tag': action_tag,
        'decision': decision,
        'buy_style': buy_style,
        'sell_style': sell_style,
        'buy_point': buy_point,
        'sell_point': sell_point,
        'abandon': abandon,
        'stop_loss': fmt_level(stop_loss_val),
        'take_profit': fmt_level(take_profit_val),
        'tp1': fmt_level(take_profit_val),
        'tp2': fmt_level(mid_pressure_val or take_profit_val),
        'short_support': fmt_level(short_support_val),
        'short_pressure': fmt_level(short_pressure_val),
        'mid_support': fmt_level(mid_support_val),
        'mid_pressure': fmt_level(mid_pressure_val),
        'rr': rr_text,
        'trend': levels.get('trend') or '待确认',
        'holding_window': '先按 1-3 个交易日试错，延续强势再滚动持有，但单笔计划不超过 20 天',
        'stop_loss_rule': levels.get('stop_loss_rule') or '跌破中线支撑或买后 1-2 天无主动性时退出',
        'take_profit_rule': levels.get('take_profit_rule') or '冲至短线压力或预估上沿附近放量滞涨时分批止盈',
        'strategy': levels.get('strategy') or '优先等回踩承接或强转强确认，不做情绪化追高。',
        'priority': priority,
        'position_plan': position_plan,
        'order_mode': order_mode,
        'trigger': trigger,
        'order_band': order_band,
        'fail_condition': fail_condition,
        'target_position_ratio': target_position_ratio,
        'first_probe_ratio': first_probe_ratio,
        'add_condition': add_condition,
        'pre_open_action': pre_open_action,
        'post_open_15m_action': post_open_15m_action,
        'opening_playbook': opening_playbook,
    }


def build_market_environment(review_ctx, selection, news_pack):
    board_stage_map = review_ctx.get('board_stage_map') or {}
    stage_counts = {'主升': 0, '修复': 0, '轮动': 0, '分歧': 0, '退潮': 0}
    for stage in board_stage_map.values():
        if stage in stage_counts:
            stage_counts[stage] += 1

    top_priority = selection.get('priority_sectors', [])[:2]
    stage_labels = [board_stage_map.get(x) for x in top_priority if board_stage_map.get(x)]
    sentiment = news_pack.get('sentiment') or '中性'

    rule_score = 0
    hard_rules = []
    if stage_counts['主升'] >= 2:
        rule_score += 2
        hard_rules.append('主升板块>=2')
    if stage_counts['修复'] >= 2:
        rule_score += 1
        hard_rules.append('修复板块>=2')
    if stage_counts['退潮'] >= 2:
        rule_score -= 2
        hard_rules.append('退潮板块>=2')
    if stage_labels and all(x in ('主升', '修复') for x in stage_labels):
        rule_score += 1
        hard_rules.append('前两优先板块均非退潮')
    if sentiment == '偏谨慎':
        rule_score -= 1
        hard_rules.append('隔夜情绪偏谨慎')

    if rule_score >= 3:
        market_phase = '主升'
        action_stance = '可积极出手，但不满仓梭哈'
        max_position = '40%-60%'
        should_buy = '只做A层候选，且必须满足回踩承接或突破确认。'
        should_sell = '若龙头掉队或板块失去共振，及时卖出/降仓。'
    elif rule_score >= 1:
        market_phase = '修复'
        action_stance = '先轻仓试错，确认后再加仓'
        max_position = '20%-40%'
        should_buy = '只做A层候选里的龙头/中军，首仓不超过15%。'
        should_sell = '修复半路夭折时先卖后看。'
    elif rule_score <= -1:
        market_phase = '退潮/弱轮动'
        action_stance = '空仓等待 / 极轻仓观察'
        max_position = '0%-10%'
        should_buy = '原则上不主动买入；A层为空时直接空仓。'
        should_sell = '若有持仓，优先卖弱留强；失守关键位要果断卖。'
    else:
        market_phase = '轮动市'
        action_stance = '轻仓快进快出'
        max_position = '10%-20%'
        should_buy = '只允许A层候选轻仓试错，不做B/C层。'
        should_sell = '不及预期就卖，冲到压力位先兑现。'

    if review_ctx.get('holdings') and market_phase in ('退潮/弱轮动', '轮动市'):
        portfolio_advice = '有持仓时先处理持仓，今天优先考虑降仓、做T或空仓等待，而不是贸然开新仓。'
    elif review_ctx.get('holdings'):
        portfolio_advice = '先处理持仓，再考虑新开仓；只有持仓稳定且A层候选出现时，才允许新增仓位。'
    else:
        portfolio_advice = '当前无持仓，只有A层候选出现才允许出手；若A层为空，不为交易而交易。'

    position_ladder = [
        '退潮：0%-10%，默认空仓等待；没有A层候选不出手。',
        '轮动：10%-20%，只做A层前排，快进快出。',
        '修复：20%-40%，先轻仓试错，确认修复延续后再加仓。',
        '主升：40%-60%，只围绕A层最强核心，不分散摊仓。',
    ]
    from_empty_to_light = '从空仓切到轻仓：必须同时满足“候选=A层 + 竞价不弱 + 开盘5分钟站上均价”。'
    from_light_to_half = '从轻仓加到半仓：首笔不亏，且开盘15分钟后仍维持板块共振、个股重新突破关键压力位。'
    forbidden_trade = '即使看好也不能出手：A层为空、RR<1.5、板块退潮、竞价明显不及预期、15分钟未站稳均价时。'
    full_attack_guard = '满仓默认禁止；仅当市场为主升且A层出现2只以上高质量标的时，也只建议控制在 60%以内。'

    return {
        'market_phase': market_phase,
        'stage_counts': stage_counts,
        'top_priority': top_priority,
        'action_stance': action_stance,
        'max_position': max_position,
        'should_buy': should_buy,
        'should_sell': should_sell,
        'portfolio_advice': portfolio_advice,
        'position_ladder': position_ladder,
        'from_empty_to_light': from_empty_to_light,
        'from_light_to_half': from_light_to_half,
        'forbidden_trade': forbidden_trade,
        'full_attack_guard': full_attack_guard,
        'hard_rules': hard_rules,
        'rule_score': rule_score,
    }


def build_daily_action_plan(review_ctx, selection, news_pack):
    holdings = [x for x in review_ctx['watchlist'] if x.get('group') == '持仓股']
    analysis_lookup = review_ctx.get('analysis_lookup') or {}
    holding_plans = [holding_action_plan(x, news_pack['sentiment'], analysis_lookup=analysis_lookup) for x in holdings]
    candidate_plans = [candidate_action_plan(x, news_pack['sentiment'], analysis_lookup=analysis_lookup) for x in selection['candidates'][:4]]
    market_env = build_market_environment(review_ctx, selection, news_pack)

    attention = []
    if selection['priority_sectors']:
        attention.append(f"优先看 { '、'.join(selection['priority_sectors'][:2]) } 这两个方向的竞价强弱、开盘 15 分钟承接和是否有板块共振。")
    if news_pack['top3']:
        attention.append('重点盯昨夜今晨最强催化是否真的映射到盘面，不要只看新闻标题。')
    if review_ctx['holdings']:
        attention.append('先处理持仓，再考虑开新仓；若持仓都走弱，今天主动降低交易频率。')
    if market_env['market_phase'] in ('退潮/弱轮动', '轮动市'):
        attention.append('当前环境不支持满仓出击，若盘面不明显转强，以空仓等待或轻仓快进快出为主。')
    return {
        'holdings': holding_plans,
        'candidates': candidate_plans,
        'attention': attention[:4],
        'market_env': market_env,
    }


def build_opening_brief_report_metadata(now=None, target_date=None, data_warnings=None, previous_summary_date=None):
    """Build a small, testable data-time metadata block for the opening brief report."""
    now_dt = now or NOW
    target = target_date or TODAY
    warnings = [str(w).strip() for w in (data_warnings or []) if str(w).strip()]
    completeness = '存在缺失/存在降级' if warnings else '正常'
    missing_note = '；'.join(warnings) if warnings else '无'
    lines = [
        f'> 数据日期：{target}',
        f'> 生成时间：{now_dt.strftime("%Y-%m-%d %H:%M:%S")}',
        '> 报告类型：盘前简报',
        '> 数据阶段：盘前数据',
        '> 行情日期要求：主要使用前一交易日收盘数据 + 当日盘前新闻/公告',
        '> 是否允许使用当日开盘后行情：否',
        '> 新闻/公告时间要求：不晚于报告生成时间',
        f'> 数据完整性：{completeness}',
        f'> 缺失说明：{missing_note}',
    ]
    if previous_summary_date:
        lines.append(f'> 前一日复盘日期：{previous_summary_date}')
    lines.append('> 备注：本报告用于盘前观察与计划，不构成买卖建议')
    return '\n'.join(lines)


def render_markdown(news_pack, anomalies, selection, focus_list, review_ctx, action_plan, now=None, target_date=None, data_warnings=None, previous_summary_date=None):
    close_path = review_ctx['close_path']
    hold_path = review_ctx['hold_path']
    report_now = now or NOW
    report_date = target_date or TODAY
    lines = []
    lines.append(f"# A股开盘前简报 - {report_date}")
    lines.append('')
    lines.extend(build_opening_brief_report_metadata(
        now=report_now,
        target_date=report_date,
        data_warnings=data_warnings,
        previous_summary_date=previous_summary_date,
    ).splitlines())
    lines.append('')
    lines.append('## 1. 昨夜今晨发生了什么（含 TrendRadar 多源）')
    lines.append('### 【最重要3条】')
    if news_pack['top3']:
        for idx, item in enumerate(news_pack['top3'], 1):
            lines.append(f"{idx}. {summarize_news_item(item)}（来源：{item.get('source') or 'Google News'}，时间：{fmt_ts(item.get('published_at'))}）")
    else:
        lines.append('1. 暂未抓取到可靠的隔夜新闻。')
    lines.append('')
    lines.append('### 【A股相关】')
    if news_pack['ashare_related']:
        for item in news_pack['ashare_related'][:8]:
            lines.append(f"- {item['title']}（{item.get('source') or 'Google News'}，{fmt_ts(item.get('published_at'))}）")
    else:
        lines.append('- 暂未抓取到与 A 股/港股直接相关的高置信新闻。')
    lines.append('')
    lines.append(f"### 【一句话总结】\n- 今天的市场情绪：**{news_pack['sentiment']}**；TrendRadar 补充命中 {news_pack['trendradar_count']} 条财经/市场相关标题。")
    lines.append('')
    lines.append('## 2. 持仓股与前一日候选股异动')
    lines.append(f"- 复盘来源：{close_path or '未找到'}")
    lines.append(f"- 持仓来源：{hold_path or '未找到'}")
    if not anomalies:
        lines.append('- 无异常')
    else:
        for entry in anomalies:
            label = f"{entry['group']} · {entry['name']}（{entry.get('code') or '无代码'}）"
            if entry.get('sector'):
                label += f" · {entry['sector']}"
            lines.append(f"- {label}")
            for flag in entry['flags']:
                if flag['type'] == '公告':
                    for row in flag['items'][:2]:
                        lines.append(f"  - 公告：{row['title']}（{fmt_ts(row.get('time'))}）")
                elif flag['type'] == '涨跌幅':
                    lines.append(f"  - 涨跌幅：近一个交易日 {flag['value']:+.2f}%")
                elif flag['type'] == '研报':
                    for row in flag['items'][:2]:
                        lines.append(f"  - 研报：{row['institution']} {row['rating']}《{row['title']}》")
    lines.append('')
    lines.append('## 3. 基于前一日复盘 + 新闻生成的新候选股')
    lines.append(f"- 前一日复盘里计划关注的板块：{'、'.join(review_ctx['plan_sectors']) if review_ctx['plan_sectors'] else '未提取到'}")
    lines.append(f"- 今天优先延展的板块：{'、'.join(selection['priority_sectors']) if selection['priority_sectors'] else '未识别到明确优先板块'}")
    if not selection['candidates']:
        lines.append('- 暂未生成高置信新候选，今天先以旧候选池承接与板块强弱排序为主。')
    else:
        for item in selection['candidates']:
            tag = '新候选' if item['is_new'] else '沿用候选'
            lines.append(f"### {item['name']}（{item['code']}）")
            lines.append(f"- 标签：{tag}")
            lines.append(f"- 所属板块：{item['sector']} / 板块阶段：{item.get('stage') or '待确认'}")
            lines.append(f"- 板块角色：{item.get('role') or '待确认'}")
            if item.get('pct1') is not None:
                lines.append(f"- 前一日涨跌幅：{item['pct1']:+.2f}%")
            if item.get('amount') is not None:
                lines.append(f"- 前一日成交额：{item['amount'] / 1e8:.2f}亿")
            lines.append(f"- 入选理由：{'；'.join(item['reasons']) if item['reasons'] else '结合前一日复盘与隔夜新闻，作为盘前观察对象'}")
            lines.append(f"- 计划关注点：{item.get('plan_focus')}")
            lines.append(f"- 确认条件：{item.get('confirm')}")
            lines.append(f"- 放弃条件：{item.get('abandon')}")
    lines.append('')
    lines.append('## 4. 今日市场环境与总策略')
    lines.append(f"- 当前市场阶段判断：**{action_plan['market_env']['market_phase']}**")
    lines.append(f"- 板块阶段统计：主升 {action_plan['market_env']['stage_counts']['主升']} / 修复 {action_plan['market_env']['stage_counts']['修复']} / 轮动 {action_plan['market_env']['stage_counts']['轮动']} / 分歧 {action_plan['market_env']['stage_counts']['分歧']} / 退潮 {action_plan['market_env']['stage_counts']['退潮']}")
    lines.append(f"- 环境硬规则：{' / '.join(action_plan['market_env'].get('hard_rules') or ['信号不足'])}；综合分 {action_plan['market_env'].get('rule_score')}")
    lines.append(f"- 今日总策略：**{action_plan['market_env']['action_stance']}**")
    lines.append(f"- 建议最大总仓位：**{action_plan['market_env']['max_position']}**")
    lines.append(f"- 今天是否应该买入：{action_plan['market_env']['should_buy']}")
    lines.append(f"- 今天是否应该卖出：{action_plan['market_env']['should_sell']}")
    lines.append(f"- 组合处理建议：{action_plan['market_env']['portfolio_advice']}")
    lines.append('- 仓位档位规则：')
    for row in action_plan['market_env']['position_ladder']:
        lines.append(f"  - {row}")
    lines.append(f"- 从空仓到轻仓：{action_plan['market_env']['from_empty_to_light']}")
    lines.append(f"- 从轻仓到半仓：{action_plan['market_env']['from_light_to_half']}")
    lines.append(f"- 即使看好也不能出手：{action_plan['market_env']['forbidden_trade']}")
    lines.append(f"- 满仓/重仓保护：{action_plan['market_env']['full_attack_guard']}")
    lines.append('')
    lines.append('## 5. 今日操作计划')
    lines.append(f"- 执行计划参考分析源：{review_ctx.get('analysis_path') or '未找到持仓/候选分析文件，以下点位使用晨报内规则与均线回推'}")
    lines.append('### 【持仓股操作】')
    if not action_plan['holdings']:
        lines.append('- 当前未提取到持仓记录。')
    else:
        for row in action_plan['holdings']:
            lines.append(f"- {row['title']}：{row['action']}【操作标签：{row['action_tag']}】")
            lines.append(f"  - 买入风格：{row['buy_style']}")
            lines.append(f"  - 卖出风格：{row['sell_style']}")
            lines.append(f"  - 买点/加仓：{row['buy_point']}")
            lines.append(f"  - 卖点/减仓：{row['sell_point']}")
            lines.append(f"  - 高开应对：{row['opening_playbook']['high_open']}")
            lines.append(f"  - 平开应对：{row['opening_playbook']['flat_open']}")
            lines.append(f"  - 低开应对：{row['opening_playbook']['low_open']}")
            lines.append(f"  - 开盘情景分级：{'；'.join(row['opening_playbook']['open_scenario_levels'])}")
            lines.append(f"  - 买点类型分层：{row['opening_playbook']['buy_type']}")
            lines.append(f"  - 竞价阈值：{row['opening_playbook']['auction_rule']}")
            lines.append(f"  - 开盘3分钟动作：{row['opening_playbook']['minute_3']}")
            lines.append(f"  - 开盘5分钟动作：{row['opening_playbook']['minute_5']}")
            lines.append(f"  - 开盘15分钟动作：{row['opening_playbook']['minute_15']}")
            lines.append(f"  - 明确买入区：{row['opening_playbook']['buy_zone']}；明确卖出区：{row['opening_playbook']['sell_zone']}；止损位：{row['opening_playbook']['stop_loss_price']}")
            lines.append(f"  - 第一次卖出比例：{row['opening_playbook']['first_sell_ratio']}")
            lines.append(f"  - 第二次卖出比例：{row['opening_playbook']['second_sell_ratio']}")
            lines.append(f"  - 撤单/取消条件：{row['opening_playbook']['cancel_rule']}")
            lines.append(f"  - 放弃条件：{row['abandon']}")
            lines.append(f"  - 备注：{row['note']}")
    lines.append('')
    lines.append('### 【候选股操作】')
    full_candidates = [x for x in action_plan['candidates'] if x.get('tier') == 'A']
    light_candidates = [x for x in action_plan['candidates'] if x.get('tier') != 'A']
    if not action_plan['candidates']:
        lines.append('- 候选股暂不超过 4 个；当前未生成高置信候选。')
    else:
        if full_candidates:
            for row in full_candidates[:3]:
                lines.append(f"- {row['title']}：{row['action']}【操作标签：{row['action_tag']} / {row['tier']}层】")
                lines.append(f"  - 属性：{row['meta']}")
                lines.append(f"  - 硬过滤结论：{row.get('hard_summary')}")
                lines.append(f"  - 买入风格：{row['buy_style']}")
                lines.append(f"  - 卖出风格：{row['sell_style']}")
                lines.append(f"  - 买点/参与方式：{row['buy_point']}")
                lines.append(f"  - 卖点/退出方式：{row['sell_point']}")
                lines.append(f"  - 高开应对：{row['opening_playbook']['high_open']}")
                lines.append(f"  - 平开应对：{row['opening_playbook']['flat_open']}")
                lines.append(f"  - 低开应对：{row['opening_playbook']['low_open']}")
                lines.append(f"  - 开盘15分钟动作：{row['opening_playbook']['minute_15']}")
                lines.append(f"  - 明确买入区：{row['opening_playbook']['buy_zone']}；明确卖出区：{row['opening_playbook']['sell_zone']}；止损位：{row['opening_playbook']['stop_loss_price']}")
                lines.append(f"  - 放弃条件：{row['abandon']}")
        else:
            lines.append('- 今日无A层可执行候选，候选股一律只观察，不生成下单级细节。')
        if light_candidates:
            lines.append('  - 观察池/仅记录：')
            for row in light_candidates[:4]:
                lines.append(f"    - {row['title']}（{row.get('tier')}层）：{row.get('hard_summary')}")
    lines.append('')
    lines.append('## 6. 今日执行计划表（可下单版）')
    lines.append('> 说明：下面不是泛泛“关注清单”，而是开盘前可直接映射到委托动作的短线执行模板。先看优先级，再看仓位，再看触发条件和委托区间；若盘中不满足触发条件，一律不下单。')
    lines.append('')
    lines.append('### 【下单前核对】')
    lines.append('- 只做主板个股与场内 ETF/LOF；不碰创业板、科创板、北交所。')
    lines.append('- 先处理持仓，再考虑新开仓；若开盘 15 分钟板块不共振，计划自动降级。')
    lines.append('- 单只新开仓默认首仓 10%-15%，确认后再加；总计划持有周期默认不超过 20 天。')
    lines.append('- 执行顺序固定为：竞价 -> 开盘3分钟 -> 开盘5分钟 -> 开盘15分钟 -> 首笔/加仓/减仓；任一环节不满足条件，后续动作自动取消。')
    lines.append('')
    lines.append('### 【持仓执行计划表】')
    lines.append('| 标的 | 优先级 | 动作 | 计划仓位比例 | 首笔试仓比例 | 二次加仓条件 | 委托方式 | 触发条件 | 计划委托区间 | 止损 | 止盈1/止盈2 | 盈亏比 | 持有周期 |')
    lines.append('|---|---|---|---|---|---|---|---|---|---:|---|---|---|')
    if not action_plan['holdings']:
        lines.append('| 当前无持仓 | - | - | - | - | - | - | - | - | - | - | - | - |')
    else:
        for row in action_plan['holdings']:
            lines.append(
                f"| {row['title']} | {row['priority']} | {row['decision']} | {row['target_position_ratio']} | {row['first_probe_ratio']} | {row['add_condition']} | {row['order_mode']} | {row['trigger']} | {row['order_band']} | {row['stop_loss']} | {row['tp1']} / {row['tp2']} | {row['rr']} | {row['holding_window']} |"
            )
            lines.append(f"> {row['title']}：短线支撑/压力={row['short_support']} / {row['short_pressure']}；中线支撑/压力={row['mid_support']} / {row['mid_pressure']}。")
            lines.append(f"> 开盘前 5 分钟动作：{row['pre_open_action']}")
            lines.append(f"> 开盘后 15 分钟动作：{row['post_open_15m_action']}")
            lines.append(f"> 止损规则：{row['stop_loss_rule']}；止盈规则：{row['take_profit_rule']}；失效条件：{row['fail_condition']}。")
            lines.append(f"> 执行备注：{row['strategy']}；原始买点={row['buy_point']}；原始卖点={row['sell_point']}。")
    lines.append('')
    lines.append('### 【候选执行计划表】')
    lines.append('| 标的 | 优先级 | 是否买入 | 计划仓位比例 | 首笔试仓比例 | 二次加仓条件 | 委托方式 | 触发条件 | 计划委托区间 | 止损 | 止盈1/止盈2 | 盈亏比 | 持有周期 |')
    lines.append('|---|---|---|---|---|---|---|---|---|---:|---|---|---|')
    table_candidates = [x for x in action_plan['candidates'] if x.get('tier') == 'A']
    if not table_candidates:
        lines.append('| 暂无A层高置信候选 | B | 观察 | 0% | 0% | 无 | 观察单 | A层为空，不出手 | - | - | - | - | - |')
    else:
        for row in table_candidates[:3]:
            lines.append(
                f"| {row['title']} | {row['priority']} | {row['decision']} | {row['target_position_ratio']} | {row['first_probe_ratio']} | {row['add_condition']} | {row['order_mode']} | {row['trigger']} | {row['order_band']} | {row['stop_loss']} | {row['tp1']} / {row['tp2']} | {row['rr']} | {row['holding_window']} |"
            )
            lines.append(f"> {row['title']}：{row['meta']}；硬过滤={row.get('hard_summary')}；短线支撑/压力={row['short_support']} / {row['short_pressure']}；中线支撑/压力={row['mid_support']} / {row['mid_pressure']}。")
            lines.append(f"> 开盘前 5 分钟动作：{row['pre_open_action']}")
            lines.append(f"> 开盘后 15 分钟动作：{row['post_open_15m_action']}")
            lines.append(f"> 止损规则：{row['stop_loss_rule']}；止盈规则：{row['take_profit_rule']}；失效条件：{row['fail_condition']}。")
            lines.append(f"> 执行备注：{row['strategy']}；原始买点={row['buy_point']}；原始卖点={row['sell_point']}。")
    lines.append('')
    lines.append('## 7. 今日关注清单')
    lines.append('### 【重点盯】')
    for row in focus_list['focus']:
        lines.append(f"- {row}")
    lines.append('')
    lines.append('### 【备选观察】')
    for row in focus_list['alt']:
        lines.append(f"- {row}")
    lines.append('')
    lines.append('### 【今日避坑】')
    for row in focus_list['avoid']:
        lines.append(f"- {row}")
    lines.append('')
    lines.append('### 【今天需要特别注意】')
    for row in action_plan['attention']:
        lines.append(f"- {row}")
    lines.append('')
    lines.append('> 说明：该简报用于盘前筛重点与防情绪化操作，不构成投资建议。')
    return '\n'.join(lines)


def main():
    if adu.skip_cron_if_not_a_share_trading_day(TODAY, task='ashare-opening-brief-feishu'):
        return
    ensure_dir()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        review_ctx = load_review_context()
        news_pack = collect_news()
        anomalies = build_watch_anomalies(review_ctx['watchlist'])
        selection = select_new_candidates(review_ctx, news_pack)
        selection['candidates'] = selection['candidates'][:4]
        focus_list = build_focus_list(news_pack, anomalies, selection)
        action_plan = build_daily_action_plan(review_ctx, selection, news_pack)
        markdown = render_markdown(news_pack, anomalies, selection, focus_list, review_ctx, action_plan)
        NOTE_PATH.write_text(markdown, encoding='utf-8')
        context = {
            'generated_at': NOW.isoformat(),
            'note_path': str(NOTE_PATH),
            'context_path': str(CONTEXT_PATH),
            'watchlist_count': len(review_ctx['watchlist']),
            'anomaly_count': len(anomalies),
            'sentiment': news_pack['sentiment'],
            'top_news_titles': [x['title'] for x in news_pack['top3']],
            'trendradar_count': news_pack['trendradar_count'],
            'source_validation': review_ctx.get('source_validation'),
            'new_candidates': [f"{x['name']}({x['code']})" for x in selection['candidates'][:4]],
            'focus': focus_list['focus'],
            'attention': action_plan['attention'],
        }
        CONTEXT_PATH.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(context, ensure_ascii=False))


if __name__ == '__main__':
    main()
