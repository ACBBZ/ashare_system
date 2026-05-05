#!/usr/bin/env python3
import contextlib
import io
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

import ashare_data_utils as adu

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
LEDGER_DIR = ROOT / 'ledger'
DB_PATH = LEDGER_DIR / 'ashare_ledger.db'
REPORT_NAME = 'holding-pnl-1505.md'
CONTEXT_NAME = 'holding-pnl-1505-context.json'


def now_local():
    return datetime.now().astimezone()


def today_str():
    return now_local().date().isoformat()


def ensure_dirs():
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / today_str()).mkdir(parents=True, exist_ok=True)


def get_conn():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            trade_time TEXT,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            fees REAL NOT NULL DEFAULT 0,
            note TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date, trade_time, id);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, trade_date, trade_time, id);

        CREATE TABLE IF NOT EXISTS daily_position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            avg_cost REAL,
            market_price REAL,
            market_value REAL,
            weight_text TEXT,
            realized_pnl_cum REAL,
            unrealized_pnl REAL,
            total_pnl_cum REAL,
            source TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(trade_date, symbol)
        );

        CREATE TABLE IF NOT EXISTS daily_reports (
            trade_date TEXT PRIMARY KEY,
            note_path TEXT NOT NULL,
            context_path TEXT NOT NULL,
            summary_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def safe_float(v):
    try:
        if v is None:
            return None
        if isinstance(v, str):
            v = v.replace(',', '').replace('%', '').strip()
            if not v:
                return None
        return float(v)
    except Exception:
        return None


def normalize_code(code):
    s = str(code or '').strip().lower()
    for prefix in ('sh', 'sz', 'bj'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    digits = ''.join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else None


def is_fund_like(name, code=None):
    text = str(name or '').upper()
    code = str(code or '')
    return 'ETF' in text or 'LOF' in text or code.startswith(('15', '16', '50', '51', '56', '58'))


def infer_asset_type(name, code=None):
    return 'fund' if is_fund_like(name, code) else 'stock'


def resolve_symbol_by_name(name):
    name = str(name or '').strip()
    if not name:
        return None
    try:
        positions = compute_positions(as_of_date=today_str())
        for pos in positions.values():
            if str(pos.get('name') or '').strip() == name:
                return pos.get('symbol')
    except Exception:
        pass
    try:
        if is_fund_like(name):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                df = adu.ak_call(ak.fund_name_em, timeout=20, attempts=3)
            q = name.replace('（', '(').replace('）', ')').replace(' ', '')
            base = re.sub(r'(ETF|LOF|A|C|\(|\)|期货)', '', q, flags=re.I)
            sub = df[df['基金简称'].astype(str).str.contains(q, na=False)]
            if sub.empty and base:
                sub = df[df['基金简称'].astype(str).str.contains(base, na=False)]
            if not sub.empty:
                return normalize_code(sub.iloc[0]['基金代码'])
        else:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                df = adu.ak_call(ak.stock_info_a_code_name, timeout=20, attempts=3)
            sub = df[df['name'].astype(str) == name]
            if not sub.empty:
                return normalize_code(sub.iloc[0]['code'])
    except Exception:
        pass
    return None


def add_trade(trade_date, symbol, name, side, quantity, price, fees=0, note='', source='manual', trade_time=None, asset_type=None):
    init_db()
    symbol = normalize_code(symbol)
    if not symbol:
        raise ValueError('symbol/code required')
    asset_type = asset_type or infer_asset_type(name, symbol)
    quantity = int(quantity)
    price = float(price)
    fees = float(fees or 0)
    if quantity <= 0:
        raise ValueError('quantity must be > 0')
    if side not in {'buy', 'sell'}:
        raise ValueError('side must be buy or sell')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO trades(trade_date, trade_time, symbol, name, asset_type, side, quantity, price, fees, note, source, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_date,
            trade_time,
            symbol,
            name,
            asset_type,
            side,
            quantity,
            price,
            fees,
            note,
            source,
            now_local().isoformat(),
        ),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def load_trades(as_of_date=None):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    if as_of_date:
        cur.execute(
            "SELECT * FROM trades WHERE trade_date <= ? ORDER BY trade_date ASC, COALESCE(trade_time, ''), id ASC",
            (as_of_date,),
        )
    else:
        cur.execute("SELECT * FROM trades ORDER BY trade_date ASC, COALESCE(trade_time, ''), id ASC")
    rows = [row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def load_trades_for_date(trade_date):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE trade_date = ? ORDER BY COALESCE(trade_time, ''), id ASC", (trade_date,))
    rows = [row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def compute_positions(as_of_date=None):
    trades = load_trades(as_of_date=as_of_date)
    positions = {}
    for t in trades:
        symbol = t['symbol']
        pos = positions.setdefault(
            symbol,
            {
                'symbol': symbol,
                'name': t['name'],
                'asset_type': t['asset_type'],
                'quantity': 0,
                'avg_cost': 0.0,
                'realized_pnl_cum': 0.0,
            },
        )
        pos['name'] = t['name'] or pos['name']
        pos['asset_type'] = t['asset_type'] or pos['asset_type']
        qty = int(t['quantity'])
        price = float(t['price'])
        fees = float(t.get('fees') or 0)
        if t['side'] == 'buy':
            total_cost = pos['quantity'] * pos['avg_cost'] + qty * price + fees
            pos['quantity'] += qty
            pos['avg_cost'] = (total_cost / pos['quantity']) if pos['quantity'] else 0.0
        else:
            if qty > pos['quantity']:
                raise ValueError(f"sell quantity exceeds holding for {pos['name']}({symbol})")
            pos['realized_pnl_cum'] += (price - pos['avg_cost']) * qty - fees
            pos['quantity'] -= qty
            if pos['quantity'] == 0:
                pos['avg_cost'] = 0.0
    return positions


def _find_col(df, candidates):
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _table_quote_map(df):
    out = {}
    if df is None or df.empty:
        return out
    code_col = _find_col(df, ['代码', '基金代码'])
    price_col = _find_col(df, ['最新价', '最新', '现价'])
    name_col = _find_col(df, ['名称', '基金简称'])
    pct_col = _find_col(df, ['涨跌幅'])
    if not code_col or not price_col:
        return out
    work = df.copy()
    work[code_col] = work[code_col].astype(str).str.zfill(6)
    for _, row in work.iterrows():
        code = str(row[code_col]).zfill(6)
        out[code] = {
            'name': str(row[name_col]) if name_col else code,
            'price': safe_float(row[price_col]),
            'pct_change': safe_float(row[pct_col]) if pct_col else None,
        }
    return out


def _stock_bid_ask_quote(code):
    code = str(code).zfill(6)
    try:
        df = adu.ak_call(ak.stock_bid_ask_em, symbol=code, timeout=15, attempts=3)
        if df is not None and not df.empty:
            kv = {str(r['item']): r['value'] for _, r in df.iterrows()}
            price = safe_float(kv.get('最新'))
            if price is not None:
                return {
                    'name': code,
                    'price': price,
                    'pct_change': safe_float(kv.get('涨幅')),
                    'source': 'ak_stock_bid_ask_em',
                }
    except Exception:
        pass
    quote = adu.fetch_quote_with_fallback(code)
    return {
        'name': quote.get('name') or code,
        'price': safe_float(quote.get('latest')),
        'pct_change': safe_float(quote.get('pct') or quote.get('change_pct')),
        'source': quote.get('source'),
    }


def fetch_quote_map(symbol_items):
    quotes = {}
    stock_codes = [x['symbol'] for x in symbol_items if x.get('asset_type') == 'stock']
    fund_codes = [x['symbol'] for x in symbol_items if x.get('asset_type') == 'fund']
    for code in stock_codes:
        try:
            q = _stock_bid_ask_quote(code)
            if q:
                quotes[str(code).zfill(6)] = q
        except Exception:
            pass
    if fund_codes:
        for fn in (ak.fund_lof_spot_em, ak.fund_etf_spot_em):
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    quotes.update(_table_quote_map(adu.ak_call(fn, timeout=20, attempts=3)))
            except Exception:
                pass
        for code in fund_codes:
            code = str(code).zfill(6)
            if code in quotes and safe_float(quotes[code].get('price')) is not None:
                continue
            try:
                quote = adu.fetch_quote_with_fallback(code)
                quotes[code] = {
                    'name': quote.get('name') or code,
                    'price': safe_float(quote.get('latest')),
                    'pct_change': safe_float(quote.get('pct') or quote.get('change_pct')),
                    'source': quote.get('source'),
                }
            except Exception:
                pass
    return quotes


def weight_to_text(market_value, total_market_value):
    if not total_market_value:
        return '0成'
    ratio = max(0.0, min(0.99, (market_value or 0) / total_market_value))
    tenth = max(1, round(ratio * 10)) if market_value and market_value > 0 else 0
    return f'{tenth}成'


def upsert_daily_snapshots(trade_date, rows):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_position_snapshots WHERE trade_date = ?", (trade_date,))
    for row in rows:
        cur.execute(
            """
            INSERT INTO daily_position_snapshots(
                trade_date, symbol, name, asset_type, quantity, avg_cost, market_price, market_value,
                weight_text, realized_pnl_cum, unrealized_pnl, total_pnl_cum, source, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                name=excluded.name,
                asset_type=excluded.asset_type,
                quantity=excluded.quantity,
                avg_cost=excluded.avg_cost,
                market_price=excluded.market_price,
                market_value=excluded.market_value,
                weight_text=excluded.weight_text,
                realized_pnl_cum=excluded.realized_pnl_cum,
                unrealized_pnl=excluded.unrealized_pnl,
                total_pnl_cum=excluded.total_pnl_cum,
                source=excluded.source,
                created_at=excluded.created_at
            """,
            (
                trade_date,
                row['symbol'],
                row['name'],
                row['asset_type'],
                row['quantity'],
                row['avg_cost'],
                row['market_price'],
                row['market_value'],
                row['weight_text'],
                row['realized_pnl_cum'],
                row['unrealized_pnl'],
                row['total_pnl_cum'],
                row['source'],
                now_local().isoformat(),
            ),
        )
    conn.commit()
    conn.close()


def load_snapshot_rows(trade_date=None):
    init_db()
    trade_date = trade_date or today_str()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM daily_position_snapshots WHERE trade_date = ? ORDER BY market_value DESC, symbol ASC", (trade_date,))
    rows = [row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def load_latest_snapshot_rows():
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) AS trade_date FROM daily_position_snapshots")
    row = cur.fetchone()
    conn.close()
    trade_date = row['trade_date'] if row and row['trade_date'] else None
    return trade_date, (load_snapshot_rows(trade_date) if trade_date else [])


def format_holdings_line(rows):
    if not rows:
        return '当前持仓记录：无。'
    parts = []
    for row in rows:
        parts.append(f"{row['name']} {row.get('weight_text') or '0成'}（成本 {row.get('avg_cost') or 0:.3f}，{int(row.get('quantity') or 0)}股）")
    return '当前持仓记录：' + '，'.join(parts) + '。'


def build_daily_snapshot(trade_date=None):
    trade_date = trade_date or today_str()
    positions = compute_positions(as_of_date=trade_date)
    open_positions = [p for p in positions.values() if int(p['quantity']) > 0]
    quote_map = fetch_quote_map(open_positions)
    rows = []
    total_market_value = 0.0
    total_realized_all = sum((safe_float(p.get('realized_pnl_cum')) or 0) for p in positions.values())
    for pos in open_positions:
        q = quote_map.get(pos['symbol'], {})
        market_price = safe_float(q.get('price'))
        market_value = (market_price * pos['quantity']) if market_price is not None else None
        if market_value is not None:
            total_market_value += market_value
        unrealized = ((market_price - pos['avg_cost']) * pos['quantity']) if market_price is not None else None
        total_pnl = (pos['realized_pnl_cum'] + unrealized) if unrealized is not None else pos['realized_pnl_cum']
        rows.append(
            {
                'symbol': pos['symbol'],
                'name': pos['name'],
                'asset_type': pos['asset_type'],
                'quantity': pos['quantity'],
                'avg_cost': pos['avg_cost'],
                'market_price': market_price,
                'market_value': market_value,
                'realized_pnl_cum': pos['realized_pnl_cum'],
                'unrealized_pnl': unrealized,
                'total_pnl_cum': total_pnl,
                'source': 'ledger_daily_report',
            }
        )
    for row in rows:
        row['weight_text'] = weight_to_text(row.get('market_value'), total_market_value)
    upsert_daily_snapshots(trade_date, rows)
    return rows, total_realized_all


def fmt_money(v):
    v = safe_float(v)
    return '—' if v is None else f'{v:.2f}'


def build_daily_report_markdown(trade_date=None):
    trade_date = trade_date or today_str()
    rows, total_realized_all = build_daily_snapshot(trade_date)
    trades = load_trades_for_date(trade_date)
    day_dir = ROOT / trade_date
    day_dir.mkdir(parents=True, exist_ok=True)
    note_path = day_dir / REPORT_NAME
    context_path = day_dir / CONTEXT_NAME

    total_market_value = sum((safe_float(r.get('market_value')) or 0) for r in rows)
    total_unrealized = sum((safe_float(r.get('unrealized_pnl')) or 0) for r in rows)
    total_realized = total_realized_all
    total_pnl = total_realized + total_unrealized

    lines = [f'# 持仓盈亏账本 - {trade_date} {now_local().strftime("%H:%M")}', '', '```text', '股票       成本     现价     持仓   累计盈亏']
    if not rows:
        lines.append(f"当前空仓   -        -        0      {fmt_money(total_pnl)}")
    else:
        for r in rows:
            lines.append(
                f"{r['name']:<8} {fmt_money(r['avg_cost']):>7} {fmt_money(r['market_price']):>8} {int(r['quantity']):>6} {fmt_money(r['total_pnl_cum']):>10}"
            )
    lines.append('```')
    markdown = '\n'.join(lines) + '\n'
    note_path.write_text(markdown, encoding='utf-8')

    summary = {
        'trade_date': trade_date,
        'note_path': str(note_path),
        'context_path': str(context_path),
        'holding_count': len(rows),
        'total_market_value': total_market_value,
        'total_unrealized': total_unrealized,
        'total_realized': total_realized,
        'total_pnl': total_pnl,
        'holdings_line': format_holdings_line(rows),
    }
    context_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO daily_reports(trade_date, note_path, context_path, summary_json, created_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(trade_date) DO UPDATE SET
            note_path=excluded.note_path,
            context_path=excluded.context_path,
            summary_json=excluded.summary_json,
            created_at=excluded.created_at
        """,
        (trade_date, str(note_path), str(context_path), json.dumps(summary, ensure_ascii=False), now_local().isoformat()),
    )
    conn.commit()
    conn.close()
    return summary


def latest_report_summary(trade_date=None):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    if trade_date:
        cur.execute("SELECT * FROM daily_reports WHERE trade_date = ?", (trade_date,))
    else:
        cur.execute("SELECT * FROM daily_reports ORDER BY trade_date DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    data = row_to_dict(row)
    try:
        data['summary'] = json.loads(data.get('summary_json') or '{}')
    except Exception:
        data['summary'] = {}
    return data


def build_close_summary_appendix(trade_date=None):
    trade_date = trade_date or today_str()
    report = latest_report_summary(trade_date)
    rows = load_snapshot_rows(trade_date)
    trades = load_trades_for_date(trade_date)
    if not rows and not trades and not report:
        return ''
    total_market_value = sum((safe_float(r.get('market_value')) or 0) for r in rows)
    total_unrealized = sum((safe_float(r.get('unrealized_pnl')) or 0) for r in rows)
    total_realized = safe_float(report.get('summary', {}).get('total_realized')) if report else None
    if total_realized is None:
        total_realized = 0.0
    total_pnl = safe_float(report.get('summary', {}).get('total_pnl')) if report else None
    if total_pnl is None:
        total_pnl = total_realized + total_unrealized
    lines = ['## 4.1 今日持仓与操作账本']
    if trades:
        lines.append('- 今日操作：')
        for t in trades:
            side = '买入' if t['side'] == 'buy' else '卖出'
            note = f"；备注：{t.get('note')}" if t.get('note') else ''
            lines.append(f"  - {side} {t['name']}（{t['symbol']}） {int(t['quantity'])}股，价格 {fmt_money(t['price'])}{note}")
    else:
        lines.append('- 今日操作：无新增记录')
    lines.append(f'- {format_holdings_line(rows)}')
    lines.append(f'- 当前总市值：{fmt_money(total_market_value)}；总浮盈亏：{fmt_money(total_unrealized)}；累计已实现盈亏：{fmt_money(total_realized)}；累计总盈亏：{fmt_money(total_pnl)}')
    lines.append('')
    return '\n'.join(lines)


def parse_holdings_line(text):
    pattern = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)\s*([0-9]+成)（成本\s*([0-9.]+)，\s*([0-9]+)股）')
    holdings = []
    for name, weight, cost, shares in pattern.findall(text or ''):
        holdings.append({
            'name': name.strip(),
            'weight': weight,
            'cost': float(cost),
            'shares': int(shares),
        })
    return holdings


def parse_trade_message(text, default_trade_date=None, default_trade_time=None):
    text = str(text or '').strip()
    if not text:
        return []
    normalized = text.replace('，', ',').replace('。', ',').replace('；', ',').replace('股康强电子', '股 康强电子').replace('股白银LOF', '股 白银LOF')
    chunks = [c.strip(' ,\n\t') for c in re.split(r'\n+', normalized) if c.strip(' ,\n\t')]
    if len(chunks) == 1:
        chunks = [c.strip() for c in re.split(r'(?:(?<=止损),\s*|(?<=止盈),\s*|(?<=减仓),\s*|(?<=加仓),\s*)', normalized) if c.strip(' ,\n\t')]
    trades = []
    for chunk in chunks:
        side = None
        if '买入' in chunk:
            side = 'buy'
        elif '卖出' in chunk:
            side = 'sell'
        if not side:
            continue

        date_match = re.search(r'(20\d{2}-\d{2}-\d{2})', chunk)
        trade_date = date_match.group(1) if date_match else default_trade_date or today_str()
        time_match = re.search(r'([01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', chunk)
        trade_time = time_match.group(0) if time_match else default_trade_time

        symbol_match = re.search(r'(\d{6})', chunk)
        symbol = normalize_code(symbol_match.group(1)) if symbol_match else None

        price_match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*卖出', chunk)
        if not price_match:
            price_match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*买入', chunk)
        if not price_match:
            price_match = re.search(r'([0-9]+(?:\.[0-9]+)?)', chunk)
        quantity_match = re.search(r'(\d+)\s*股', chunk)

        note_parts = []
        if '止损' in chunk:
            note_parts.append('止损')
        if '止盈' in chunk:
            note_parts.append('止盈')
        if '清仓' in chunk:
            note_parts.append('清仓')
        if '做T' in chunk or '做t' in chunk.lower():
            note_parts.append('做T')

        if not quantity_match or not price_match:
            continue
        quantity = int(quantity_match.group(1))
        price = float(price_match.group(1))

        name = None
        if symbol:
            m = re.search(rf'{symbol}\s*([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)', chunk)
            if m:
                name = m.group(1).strip(' ,')
        if not name:
            m = re.search(rf'卖出\s*{quantity}\s*股\s*([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)', chunk)
            if not m:
                m = re.search(rf'买入\s*{quantity}\s*股\s*([\u4e00-\u9fa5A-Za-z0-9()（）\-]+)', chunk)
            if m:
                name = m.group(1).strip(' ,')
        if not name:
            m = re.search(r'([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9()（）\-]*)', chunk)
            if m:
                name = m.group(1).strip(' ,')
        if not name:
            name = symbol or '未知标的'

        if not symbol:
            symbol = resolve_symbol_by_name(name)
        trades.append({
            'trade_date': trade_date,
            'trade_time': trade_time,
            'symbol': symbol,
            'name': name,
            'side': side,
            'quantity': quantity,
            'price': price,
            'fees': 0,
            'note': '，'.join(note_parts),
            'asset_type': infer_asset_type(name, symbol),
            'source': 'natural_language',
            'raw_text': chunk,
        })
    return trades


def record_trade_message(text, default_trade_date=None, default_trade_time=None, source='natural_language'):
    parsed = parse_trade_message(text, default_trade_date=default_trade_date, default_trade_time=default_trade_time)
    inserted = []
    for item in parsed:
        trade_id = add_trade(
            trade_date=item['trade_date'],
            trade_time=item.get('trade_time'),
            symbol=item['symbol'],
            name=item['name'],
            side=item['side'],
            quantity=item['quantity'],
            price=item['price'],
            fees=item.get('fees', 0),
            note=item.get('note', ''),
            source=source,
            asset_type=item.get('asset_type'),
        )
        inserted.append({**item, 'trade_id': trade_id})
    return inserted
