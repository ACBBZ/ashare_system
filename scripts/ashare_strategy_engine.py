#!/usr/bin/env python3
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import ashare_data_utils as adu

ROOT = Path('/home/admin/Notes/market/ashare-monitor')
DB_DIR = ROOT / 'strategy'
DB_PATH = DB_DIR / 'strategy_scoreboard.db'
DEFAULT_CAPITAL = 16000.0


def safe_float(value):
    return adu.safe_float(value)


def normalize_code(code):
    return adu.normalize_code(code)


def ensure_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS candidate_tracking (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            sector TEXT,
            stage TEXT,
            role TEXT,
            tier TEXT,
            rr REAL,
            close_price REAL,
            entry_ref REAL,
            entry_low REAL,
            entry_high REAL,
            entry_trade_date TEXT,
            entry_source TEXT,
            next1_close REAL,
            next1_high REAL,
            next1_low REAL,
            next3_close REAL,
            next3_high REAL,
            next3_low REAL,
            next5_close REAL,
            next5_high REAL,
            next5_low REAL,
            next1_ret REAL,
            next3_ret REAL,
            next5_ret REAL,
            next3_best_ret REAL,
            next3_worst_ret REAL,
            days_tracked INTEGER,
            current_ret REAL,
            best_ret_20 REAL,
            worst_ret_20 REAL,
            status TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS candidate_tracking_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            horizon_day INTEGER NOT NULL,
            price_trade_date TEXT,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            close_ret REAL,
            best_ret REAL,
            worst_ret REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code, horizon_day)
        )
        '''
    )

    def ensure_columns(table_name, columns):
        existing = {row[1] for row in cur.execute(f'PRAGMA table_info({table_name})').fetchall()}
        for column_name, column_def in columns.items():
            if column_name not in existing:
                cur.execute(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}')

    ensure_columns(
        'candidate_tracking',
        {
            'entry_low': 'REAL',
            'entry_high': 'REAL',
            'entry_trade_date': 'TEXT',
            'entry_source': 'TEXT',
            'days_tracked': 'INTEGER',
            'current_ret': 'REAL',
            'best_ret_20': 'REAL',
            'worst_ret_20': 'REAL',
            'status': 'TEXT',
        },
    )

    cur.execute('CREATE INDEX IF NOT EXISTS idx_candidate_tracking_trade_date ON candidate_tracking(trade_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_candidate_tracking_tier ON candidate_tracking(tier)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_candidate_tracking_daily_horizon ON candidate_tracking_daily(horizon_day)')
    conn.commit()
    return conn


def parse_rr_value(text):
    if text is None:
        return None
    raw = str(text)
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*:\s*([0-9]+(?:\.[0-9]+)?)', raw)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))
        if abs(a - 1.0) < 0.05 and b > 0:
            return b
        if abs(b - 1.0) < 0.05 and a > 0:
            return a
        if b > 0:
            return a / b
    nums = [float(x) for x in re.findall(r'([0-9]+(?:\.[0-9]+)?)', raw)]
    if not nums:
        return None
    return nums[-1]


def stage_weight(stage):
    return {
        '主升': 4,
        '修复': 3,
        '轮动': 2,
        '分歧': 1,
        '退潮': -2,
    }.get(str(stage or '').strip(), 0)


def role_weight(role):
    return {
        '龙头': 4,
        '中军': 3,
        '补涨': 2,
        '跟风': 0,
        '沿用候选': 1,
    }.get(str(role or '').strip(), 0)


def classify_market_hard(limit_stats, amount_stats, latest_capture):
    zt = int(safe_float(limit_stats.get('zt_count')) or 0)
    dt = int(safe_float(limit_stats.get('dt_count')) or 0)
    max_lb = int(safe_float(limit_stats.get('max_lb')) or 0)
    delta_pct = safe_float(amount_stats.get('delta_pct'))
    up_count = int(safe_float((latest_capture or {}).get('up_count')) or 0)
    down_count = int(safe_float((latest_capture or {}).get('down_count')) or 0)
    total = up_count + down_count
    breadth = (up_count / total) if total else None

    hard_rules = []
    if zt >= 80:
        hard_rules.append('涨停>=80')
    if dt <= 10:
        hard_rules.append('跌停<=10')
    if max_lb >= 4:
        hard_rules.append('最高连板>=4')
    if breadth is not None and breadth >= 0.55:
        hard_rules.append('上涨占比>=55%')
    if delta_pct is not None and delta_pct >= 0:
        hard_rules.append('量能未缩')

    score = 0
    score += 2 if zt >= 80 else (1 if zt >= 50 else 0)
    score -= 2 if dt >= 25 else (-1 if dt >= 15 else 0)
    score += 2 if max_lb >= 4 else (1 if max_lb >= 3 else 0)
    score += 1 if breadth is not None and breadth >= 0.55 else (-1 if breadth is not None and breadth < 0.45 else 0)
    score += 1 if delta_pct is not None and delta_pct >= 0 else (-1 if delta_pct is not None and delta_pct <= -8 else 0)

    if score >= 4 and zt >= 70 and dt <= 12 and max_lb >= 3:
        market_phase = '主升'
        action_stance = '可积极出手，但不满仓梭哈'
        max_position = '40%-60%'
        env = '顺风'
    elif score >= 2 and zt >= 45 and dt <= 20:
        market_phase = '修复'
        action_stance = '先轻仓试错，确认后再加仓'
        max_position = '20%-40%'
        env = '中性偏顺风'
    elif score <= -1 or dt >= 25 or (breadth is not None and breadth < 0.42):
        market_phase = '退潮/弱轮动'
        action_stance = '空仓等待或极轻仓试错'
        max_position = '0%-10%'
        env = '逆风'
    else:
        market_phase = '轮动市'
        action_stance = '只做最强前排，轻仓快进快出'
        max_position = '10%-20%'
        env = '中性'

    return {
        'market_phase': market_phase,
        'action_stance': action_stance,
        'max_position': max_position,
        'env': env,
        'score': score,
        'breadth': breadth,
        'hard_rules': hard_rules,
        'zt': zt,
        'dt': dt,
        'max_lb': max_lb,
        'delta_pct': delta_pct,
        'up_count': up_count,
        'down_count': down_count,
    }


def _lot_ratio(close_price, capital=DEFAULT_CAPITAL):
    close_price = safe_float(close_price)
    if close_price is None or close_price <= 0 or capital <= 0:
        return None
    return close_price * 100 / capital


def candidate_hard_filter(item, metrics=None, *, capital=DEFAULT_CAPITAL, market_phase=None):
    metrics = metrics or {}
    spot = item.get('spot') or {}
    close_price = safe_float(metrics.get('close')) or safe_float(spot.get('最新价')) or safe_float(item.get('close'))
    rr = parse_rr_value(item.get('rr') or metrics.get('rr'))
    amount = safe_float(spot.get('成交额')) or safe_float(metrics.get('amount')) or safe_float(item.get('amount'))
    stage = item.get('stage') or '待确认'
    role = item.get('role') or '待确认'
    pos60 = safe_float(metrics.get('pos_60d') or item.get('pos60'))
    lot_ratio = _lot_ratio(close_price, capital=capital)

    hard_fail = []
    soft_flags = []
    pass_flags = []

    if close_price is None or close_price <= 0:
        soft_flags.append('价格缺失')
    else:
        if lot_ratio is not None and lot_ratio > 1.0:
            hard_fail.append('一手成本超过账户总资金')
        elif lot_ratio is not None and lot_ratio > 0.50:
            soft_flags.append('一手成本超过半仓承受度')
        elif lot_ratio is not None and lot_ratio <= 0.25:
            pass_flags.append('一手成本适合小资金试错')

    if rr is None:
        soft_flags.append('盈亏比缺失')
    elif rr < 1.0:
        hard_fail.append('盈亏比<1')
    elif rr < 1.5:
        soft_flags.append('盈亏比<1.5，仅适合观察')
    else:
        pass_flags.append('盈亏比>=1.5')

    if amount is None:
        soft_flags.append('成交额缺失')
    elif amount < 1e8:
        hard_fail.append('成交额<1亿，流动性不足')
    elif amount < 5e8:
        soft_flags.append('成交额<5亿，流动性一般')
    else:
        pass_flags.append('成交额>=5亿')

    if stage == '退潮':
        hard_fail.append('板块处于退潮')
    elif stage in ('主升', '修复'):
        pass_flags.append(f'板块阶段={stage}')
    elif stage == '轮动':
        soft_flags.append('板块仅轮动，不宜重仓')
    else:
        soft_flags.append('板块阶段待确认')

    if role in ('龙头', '中军'):
        pass_flags.append(f'个股角色={role}')
    elif role in ('补涨', '沿用候选'):
        soft_flags.append(f'个股角色={role}')
    else:
        soft_flags.append('角色不清晰')

    if pos60 is not None:
        if pos60 >= 92 and role not in ('龙头', '中军'):
            soft_flags.append('60日位置过高，追价风险大')
        elif pos60 <= 75:
            pass_flags.append('60日位置尚可')

    if market_phase in ('退潮/弱轮动', '轮动市') and stage not in ('主升', '修复'):
        soft_flags.append('当前环境只适合做最强前排')

    if hard_fail:
        tier = 'C'
        tradable = False
        detail_level = 'record'
    else:
        if rr is not None and rr >= 1.5 and stage in ('主升', '修复') and amount is not None and amount >= 5e8 and (lot_ratio is None or lot_ratio <= 0.50):
            tier = 'A'
            tradable = True
            detail_level = 'full'
        else:
            tier = 'B'
            tradable = False
            detail_level = 'brief'

    return {
        'tier': tier,
        'tradable': tradable,
        'detail_level': detail_level,
        'rr_value': rr,
        'close_price': close_price,
        'amount': amount,
        'lot_ratio': lot_ratio,
        'hard_fail': hard_fail,
        'soft_flags': soft_flags,
        'pass_flags': pass_flags,
        'summary': '；'.join((hard_fail or []) + soft_flags[:2] + pass_flags[:2]) or '待进一步核验',
    }


def validate_trade_levels(levels):
    levels = dict(levels or {})
    buy_low = safe_float(levels.get('buy_low'))
    buy_high = safe_float(levels.get('buy_high'))
    breakout = safe_float(levels.get('breakout'))
    stop = safe_float(levels.get('stop'))
    sell1 = safe_float(levels.get('sell1'))
    sell2 = safe_float(levels.get('sell2'))

    if buy_low is not None and buy_high is not None and buy_low > buy_high:
        buy_low, buy_high = buy_high, buy_low
    if stop is not None and buy_low is not None and stop >= buy_low:
        stop = round(buy_low * 0.985, 2)
    if buy_high is not None and breakout is not None and buy_high >= breakout:
        buy_high = round(breakout * 0.998, 2)
        buy_low = min(buy_low or buy_high, buy_high)
    if sell1 is not None and buy_high is not None and sell1 <= buy_high:
        sell1 = round(buy_high * 1.03, 2)
    if sell2 is not None and sell1 is not None and sell2 < sell1:
        sell2 = sell1

    levels.update({
        'buy_low': buy_low,
        'buy_high': buy_high,
        'breakout': breakout,
        'stop': stop,
        'sell1': sell1,
        'sell2': sell2,
    })
    return levels


def round_price(value, ndigits=2):
    value = safe_float(value)
    if value is None:
        return None
    return round(value + 1e-8, ndigits)


def parse_level_list(value):
    if value is None:
        return []
    nums = []
    for token in re.findall(r'([0-9]+(?:\.[0-9]+)?)', str(value)):
        try:
            nums.append(float(token))
        except Exception:
            continue
    return nums


def parse_range_pair(value):
    if value is None:
        return (None, None)
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*[-~～]\s*([0-9]+(?:\.[0-9]+)?)', str(value))
    if not m:
        return (None, None)
    return safe_float(m.group(1)), safe_float(m.group(2))


def parse_analysis_candidate_sections(analysis_path: Path | None):
    if not analysis_path or not analysis_path.exists():
        return {}
    text = analysis_path.read_text(encoding='utf-8')
    pattern = re.compile(r'^##\s+候选：([^（\n]+)（(\d{6})）\n(.*?)(?=^##\s+(?:候选|持仓|组合级结论|后续可扩展)|\Z)', flags=re.M | re.S)
    out = {}
    for name, code, body in pattern.findall(text):
        item = {
            'name': name.strip(),
            'code': normalize_code(code),
            'sector': None,
            'stage': None,
            'supports': [],
            'pressures': [],
            'pred_low': None,
            'pred_high': None,
            'rr': None,
            'close_price': None,
            'strategy': None,
            'source_path': str(analysis_path),
        }
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith('- 所属板块：'):
                payload = line.split('：', 1)[1].strip()
                bits = [x.strip() for x in payload.split('；') if x.strip()]
                if bits:
                    item['sector'] = bits[0]
                for bit in bits[1:]:
                    if '板块阶段' in bit:
                        item['stage'] = bit.split('：', 1)[-1].strip()
            elif line.startswith('- 最新收盘/现价：'):
                first = parse_level_list(line)
                item['close_price'] = first[0] if first else None
            elif line.startswith('- 关键支撑位：'):
                item['supports'] = parse_level_list(line.split('：', 1)[1])
            elif line.startswith('- 关键压力位：'):
                item['pressures'] = parse_level_list(line.split('：', 1)[1])
            elif line.startswith('- 明日走势预判：'):
                item['pred_low'], item['pred_high'] = parse_range_pair(line.split('：', 1)[1])
            elif line.startswith('- 盈亏比参考：') or line.startswith('- 盈亏比：'):
                item['rr'] = parse_rr_value(line.split('：', 1)[1])
            elif line.startswith('- 操作策略：'):
                item['strategy'] = line.split('：', 1)[1].strip()
        out[item['code']] = item
    return out


def choose_recommended_entry(candidate=None, analysis=None, metrics=None):
    candidate = candidate or {}
    analysis = analysis or {}
    metrics = metrics or {}
    close_price = safe_float(metrics.get('close')) or safe_float(analysis.get('close_price')) or safe_float((candidate.get('spot') or {}).get('最新价')) or safe_float(candidate.get('close_price'))
    ma5 = safe_float(metrics.get('ma5'))
    ma10 = safe_float(metrics.get('ma10'))
    ma20 = safe_float(metrics.get('ma20'))
    supports = [safe_float(x) for x in (analysis.get('supports') or []) if safe_float(x) is not None]
    pred_low = safe_float(analysis.get('pred_low'))
    pred_high = safe_float(analysis.get('pred_high'))

    entry_low = None
    entry_high = None
    source = None

    if supports:
        entry_low = supports[0]
        entry_high = supports[0]
        source = 'analysis_support'
        if pred_low is not None:
            entry_low = min(entry_low, pred_low)
            entry_high = max(entry_high, pred_low)
        if pred_high is not None and entry_high > pred_high:
            entry_high = pred_high
    elif pred_low is not None and pred_high is not None:
        entry_low, entry_high = pred_low, pred_high
        source = 'analysis_predicted_range'
    else:
        ma_levels = [x for x in [ma5, ma10, ma20] if x is not None]
        if len(ma_levels) >= 2:
            entry_low, entry_high = min(ma_levels[:2]), max(ma_levels[:2])
            source = 'ma_support_zone'
        elif ma_levels:
            entry_low = ma_levels[0] * 0.995
            entry_high = ma_levels[0] * 1.005
            source = 'single_ma_zone'
        elif close_price is not None:
            entry_low = close_price * 0.985
            entry_high = close_price * 0.995
            source = 'close_discount'

    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    if close_price is not None and entry_high is not None:
        entry_high = min(entry_high, close_price)
    if entry_low is None and entry_high is not None:
        entry_low = entry_high
    if entry_high is None and entry_low is not None:
        entry_high = entry_low

    entry_ref = None
    if entry_low is not None and entry_high is not None:
        entry_ref = (entry_low + entry_high) / 2
    elif entry_low is not None:
        entry_ref = entry_low
    elif close_price is not None:
        entry_ref = close_price
        entry_low = close_price
        entry_high = close_price
        source = source or 'close_fallback'

    return {
        'entry_ref': round_price(entry_ref),
        'entry_low': round_price(entry_low),
        'entry_high': round_price(entry_high),
        'entry_source': source or 'unavailable',
    }


def parse_close_summary_candidates(summary_path: Path | None):
    if not summary_path or not summary_path.exists():
        return {}
    text = summary_path.read_text(encoding='utf-8')
    section_m = re.search(r'## 3\. 个股筛选（.*?）\n(.+?)(?:\n## 4\.|\Z)', text, flags=re.S)
    block = section_m.group(1) if section_m else text
    out = {}
    current_tier = None
    current_stage = None
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith('### A层'):
            current_tier = 'A'
            continue
        if line.startswith('### B层'):
            current_tier = 'B'
            continue
        if line.startswith('### C层'):
            current_tier = 'C'
            continue
        if line.startswith('#### '):
            m = re.match(r'####\s+([^（\n]+)（(\d{6})）', line)
            if not m:
                continue
            code = normalize_code(m.group(2))
            out.setdefault(code, {'name': m.group(1).strip(), 'code': code, 'tier': current_tier})
            continue
        if line.startswith('- 板块阶段：'):
            current_stage = line.split('：', 1)[1].strip()
            continue
        m = re.match(r'-\s*([^（\n]+)（(\d{6})(?:，([^，）]+))?(?:，([^，）]+))?(?:，([^，）]+))?', line)
        if m:
            name = m.group(1).strip()
            code = normalize_code(m.group(2))
            item = out.setdefault(code, {'name': name, 'code': code})
            item['tier'] = item.get('tier') or current_tier
            if m.group(3):
                item['sector'] = m.group(3).strip()
            if m.group(4):
                item['stage'] = m.group(4).strip()
            if m.group(5):
                item['role'] = m.group(5).strip()
            continue
        if line.startswith('- 所属板块：') and out:
            payload = line.split('：', 1)[1].strip()
            latest_key = next(reversed(out))
            item = out[latest_key]
            bits = [x.strip() for x in payload.split('；') if x.strip()]
            if bits:
                item['sector'] = bits[0]
            for bit in bits[1:]:
                if '板块阶段' in bit:
                    item['stage'] = bit.split('：', 1)[-1].strip()
                elif '角色' in bit:
                    item['role'] = bit.split('：', 1)[-1].strip()
    if current_stage:
        for item in out.values():
            item.setdefault('stage', current_stage)
    return out


def summary_path_for_trade_date(trade_date: str):
    day_dir = ROOT / str(trade_date)
    return adu.pick_first_existing(day_dir / 'close-summary.md', day_dir / 'latest-summary.md')


def analysis_path_for_trade_date(trade_date: str):
    day_dir = ROOT / str(trade_date)
    files = sorted(day_dir.glob('持仓股与候选股分析-*.md'))
    return files[-1] if files else None


def _future_price_metrics(code, trade_date, max_horizon=20):
    code = normalize_code(code)
    start = datetime.strptime(trade_date, '%Y-%m-%d').date()
    end = start + timedelta(days=60)
    try:
        df = adu.fetch_pytdx_hist_df(code, start.strftime('%Y%m%d'), end.strftime('%Y%m%d'))
    except Exception:
        df = adu.fetch_hist_df_with_fallback(code, start.strftime('%Y%m%d'), end.strftime('%Y%m%d'))
    if df is None or df.empty:
        return None
    work = df.copy().sort_values('date').reset_index(drop=True)
    work['trade_date'] = work['date'].dt.strftime('%Y-%m-%d')
    future = work[work['trade_date'] > trade_date].copy().reset_index(drop=True)
    if future.empty:
        return None
    future = future.head(max_horizon).copy().reset_index(drop=True)
    days = []
    for horizon in range(1, len(future) + 1):
        sub = future.head(horizon)
        row = sub.iloc[-1]
        days.append({
            'horizon_day': horizon,
            'price_trade_date': row['trade_date'],
            'close_price': safe_float(row.get('close')),
            'high_price': safe_float(sub['high'].max()) if 'high' in sub.columns else safe_float(sub['close'].max()),
            'low_price': safe_float(sub['low'].min()) if 'low' in sub.columns else safe_float(sub['close'].min()),
        })
    return {
        'entry_trade_date': future.iloc[0]['trade_date'],
        'days': days,
    }


def record_candidates(trade_date, candidates, market_ctx=None):
    conn = ensure_db()
    now = datetime.now().astimezone().isoformat()
    cur = conn.cursor()
    for item in candidates:
        metrics = item.get('metrics') or {}
        filt = item.get('filter') or candidate_hard_filter(item, metrics, market_phase=(market_ctx or {}).get('market_phase'))
        entry_plan = choose_recommended_entry(item, metrics=metrics)
        rr = safe_float(filt.get('rr_value'))
        metadata = {
            'filter': filt,
            'market': market_ctx or {},
            'entry_plan': entry_plan,
        }
        cur.execute(
            '''
            INSERT INTO candidate_tracking (
                trade_date, code, name, sector, stage, role, tier, rr, close_price,
                entry_ref, entry_low, entry_high, entry_source,
                metadata_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date, code) DO UPDATE SET
                name=excluded.name,
                sector=excluded.sector,
                stage=excluded.stage,
                role=excluded.role,
                tier=excluded.tier,
                rr=excluded.rr,
                close_price=excluded.close_price,
                entry_ref=excluded.entry_ref,
                entry_low=excluded.entry_low,
                entry_high=excluded.entry_high,
                entry_source=excluded.entry_source,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            ''',
            (
                trade_date,
                normalize_code(item.get('code')),
                item.get('name'),
                item.get('sector'),
                item.get('stage'),
                item.get('role'),
                filt.get('tier'),
                rr,
                safe_float(metrics.get('close')) or safe_float((item.get('spot') or {}).get('最新价')),
                entry_plan.get('entry_ref'),
                entry_plan.get('entry_low'),
                entry_plan.get('entry_high'),
                entry_plan.get('entry_source'),
                json.dumps(metadata, ensure_ascii=False),
                now,
                now,
            )
        )
    conn.commit()
    conn.close()


def update_outcomes(limit=5000, max_horizon=20):
    conn = ensure_db()
    cur = conn.cursor()
    rows = cur.execute(
        '''
        SELECT trade_date, code, entry_ref, entry_trade_date, days_tracked
        FROM candidate_tracking
        ORDER BY trade_date ASC, code ASC
        LIMIT ?
        ''',
        (limit,)
    ).fetchall()
    now = datetime.now().astimezone().isoformat()
    updated = 0
    for trade_date, code, entry_ref, entry_trade_date, days_tracked in rows:
        metrics = _future_price_metrics(code, trade_date, max_horizon=max_horizon)
        if not metrics:
            continue
        entry = safe_float(entry_ref)
        if entry in (None, 0):
            continue
        days = metrics.get('days') or []
        if not days:
            continue
        cur.execute('DELETE FROM candidate_tracking_daily WHERE trade_date = ? AND code = ?', (trade_date, code))
        for item in days:
            close_price = safe_float(item.get('close_price'))
            high_price = safe_float(item.get('high_price'))
            low_price = safe_float(item.get('low_price'))
            close_ret = None if close_price is None else (close_price / entry - 1) * 100
            best_ret = None if high_price is None else (high_price / entry - 1) * 100
            worst_ret = None if low_price is None else (low_price / entry - 1) * 100
            cur.execute(
                '''
                INSERT OR REPLACE INTO candidate_tracking_daily (
                    trade_date, code, horizon_day, price_trade_date, close_price, high_price, low_price,
                    close_ret, best_ret, worst_ret, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ''',
                (
                    trade_date,
                    code,
                    int(item['horizon_day']),
                    item.get('price_trade_date'),
                    close_price,
                    high_price,
                    low_price,
                    close_ret,
                    best_ret,
                    worst_ret,
                    now,
                    now,
                )
            )
        by_h = {int(item['horizon_day']): item for item in days}
        latest_h = max(by_h)
        best_20 = max([(safe_float(x.get('high_price')) / entry - 1) * 100 for x in days if safe_float(x.get('high_price')) is not None], default=None)
        worst_20 = min([(safe_float(x.get('low_price')) / entry - 1) * 100 for x in days if safe_float(x.get('low_price')) is not None], default=None)
        def close_at(h):
            item = by_h.get(h)
            return safe_float(item.get('close_price')) if item else None
        def high_at(h):
            item = by_h.get(h)
            return safe_float(item.get('high_price')) if item else None
        def low_at(h):
            item = by_h.get(h)
            return safe_float(item.get('low_price')) if item else None
        def ret(value):
            if value is None:
                return None
            return (value / entry - 1) * 100
        cur.execute(
            '''
            UPDATE candidate_tracking
            SET entry_trade_date=?,
                next1_close=?, next1_high=?, next1_low=?,
                next3_close=?, next3_high=?, next3_low=?,
                next5_close=?, next5_high=?, next5_low=?,
                next1_ret=?, next3_ret=?, next5_ret=?,
                next3_best_ret=?, next3_worst_ret=?,
                days_tracked=?, current_ret=?, best_ret_20=?, worst_ret_20=?, status=?, updated_at=?
            WHERE trade_date=? AND code=?
            ''',
            (
                metrics.get('entry_trade_date'),
                close_at(1), high_at(1), low_at(1),
                close_at(3), high_at(3), low_at(3),
                close_at(5), high_at(5), low_at(5),
                ret(close_at(1)), ret(close_at(3)), ret(close_at(5)),
                ret(high_at(3)), ret(low_at(3)),
                latest_h,
                ret(close_at(latest_h)),
                best_20,
                worst_20,
                'completed' if latest_h >= max_horizon else 'tracking',
                now,
                trade_date,
                code,
            )
        )
        updated += 1
    conn.commit()
    conn.close()
    return {'updated': updated, 'max_horizon': max_horizon}


def recent_scoreboard(window=20, refresh=True):
    if refresh:
        update_outcomes(limit=5000, max_horizon=20)
    conn = ensure_db()
    cur = conn.cursor()
    trade_dates = [r[0] for r in cur.execute('SELECT DISTINCT trade_date FROM candidate_tracking ORDER BY trade_date DESC LIMIT ?', (window,)).fetchall()]
    if not trade_dates:
        conn.close()
        return {'available_days': 0, 'window': window, 'rows': [], 'detail_rows': []}
    placeholders = ','.join('?' for _ in trade_dates)
    base_df = pd.read_sql_query(
        f'SELECT * FROM candidate_tracking WHERE trade_date IN ({placeholders})',
        conn,
        params=trade_dates,
    )
    daily_df = pd.read_sql_query(
        f'SELECT d.*, b.tier, b.name, b.sector, b.stage, b.role FROM candidate_tracking_daily d '
        f'JOIN candidate_tracking b ON b.trade_date = d.trade_date AND b.code = d.code '
        f'WHERE d.trade_date IN ({placeholders})',
        conn,
        params=trade_dates,
    )
    conn.close()
    if base_df.empty:
        return {'available_days': 0, 'window': window, 'rows': [], 'detail_rows': []}

    rows = []
    for tier in ['A', 'B', 'C']:
        sub = base_df[base_df['tier'] == tier].copy()
        if sub.empty:
            continue
        tier_daily = daily_df[daily_df['tier'] == tier].copy()
        stats = {}
        for horizon in [1, 3, 5, 10, 20]:
            hsub = tier_daily[tier_daily['horizon_day'] == horizon]
            series = pd.to_numeric(hsub['close_ret'], errors='coerce')
            stats[horizon] = {
                'hit_rate': float((series > 0).mean() * 100) if not series.empty and series.notna().any() else None,
                'avg_ret': float(series.mean()) if not series.empty and series.notna().any() else None,
            }
        best20 = pd.to_numeric(sub['best_ret_20'], errors='coerce')
        worst20 = pd.to_numeric(sub['worst_ret_20'], errors='coerce')
        rows.append({
            'tier': tier,
            'count': int(len(sub)),
            'stats': stats,
            'avg_best_ret_20': float(best20.mean()) if best20.notna().any() else None,
            'avg_worst_ret_20': float(worst20.mean()) if worst20.notna().any() else None,
        })

    detail_rows = []
    for _, row in base_df.sort_values(['trade_date', 'tier', 'code'], ascending=[False, True, True]).iterrows():
        detail_rows.append({
            'trade_date': None if pd.isna(row['trade_date']) else row['trade_date'],
            'code': None if pd.isna(row['code']) else row['code'],
            'name': None if pd.isna(row['name']) else row['name'],
            'tier': None if pd.isna(row['tier']) else row['tier'],
            'sector': None if pd.isna(row['sector']) else row['sector'],
            'stage': None if pd.isna(row['stage']) else row['stage'],
            'role': None if pd.isna(row['role']) else row['role'],
            'entry_ref': safe_float(row.get('entry_ref')),
            'entry_trade_date': None if pd.isna(row.get('entry_trade_date')) else row.get('entry_trade_date'),
            'days_tracked': int(safe_float(row.get('days_tracked')) or 0),
            'current_ret': safe_float(row.get('current_ret')),
            'best_ret_20': safe_float(row.get('best_ret_20')),
            'worst_ret_20': safe_float(row.get('worst_ret_20')),
            'status': None if pd.isna(row.get('status')) else (row.get('status') or 'tracking'),
        })

    overall_20 = pd.to_numeric(daily_df[daily_df['horizon_day'] == 20]['close_ret'], errors='coerce')
    return {
        'available_days': len(trade_dates),
        'window': window,
        'rows': rows,
        'detail_rows': detail_rows,
        'overall_avg_ret_20d': float(overall_20.mean()) if overall_20.notna().any() else None,
        'tracked_dates': sorted(trade_dates),
    }


def backfill_from_close_summary_notes(limit_days=120):
    today_iso = datetime.now().astimezone().date().isoformat()
    day_dirs = adu.list_review_day_dirs(ROOT, before_date=today_iso)
    if not day_dirs:
        return {'backfilled_days': 0, 'records': 0}
    target_dirs = day_dirs[-limit_days:]
    conn = ensure_db()
    cur = conn.cursor()
    inserted = 0
    touched_days = 0
    now = datetime.now().astimezone().isoformat()
    for day_dir in target_dirs:
        trade_date = day_dir.name
        summary_path = adu.pick_first_existing(day_dir / 'close-summary.md', day_dir / 'latest-summary.md')
        analysis_path = analysis_path_for_trade_date(trade_date)
        if not summary_path and not analysis_path:
            continue
        summary_candidates = parse_close_summary_candidates(summary_path)
        analysis_candidates = parse_analysis_candidate_sections(analysis_path)
        merged_codes = sorted(set(summary_candidates) | set(analysis_candidates))
        if not merged_codes:
            continue
        touched_days += 1
        for code in merged_codes:
            item = {**analysis_candidates.get(code, {}), **summary_candidates.get(code, {})}
            item['code'] = normalize_code(code)
            item['name'] = item.get('name') or analysis_candidates.get(code, {}).get('name') or summary_candidates.get(code, {}).get('name')
            if not item['code'] or not item['name']:
                continue
            entry_plan = choose_recommended_entry(item, analysis=analysis_candidates.get(code, {}), metrics={})
            filt = candidate_hard_filter(item, {'close': analysis_candidates.get(code, {}).get('close_price')}, market_phase=item.get('stage'))
            cur.execute(
                '''
                INSERT INTO candidate_tracking (
                    trade_date, code, name, sector, stage, role, tier, rr, close_price,
                    entry_ref, entry_low, entry_high, entry_source,
                    metadata_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    name=excluded.name,
                    sector=COALESCE(excluded.sector, candidate_tracking.sector),
                    stage=COALESCE(excluded.stage, candidate_tracking.stage),
                    role=COALESCE(excluded.role, candidate_tracking.role),
                    tier=COALESCE(excluded.tier, candidate_tracking.tier),
                    rr=COALESCE(excluded.rr, candidate_tracking.rr),
                    close_price=COALESCE(excluded.close_price, candidate_tracking.close_price),
                    entry_ref=COALESCE(excluded.entry_ref, candidate_tracking.entry_ref),
                    entry_low=COALESCE(excluded.entry_low, candidate_tracking.entry_low),
                    entry_high=COALESCE(excluded.entry_high, candidate_tracking.entry_high),
                    entry_source=COALESCE(excluded.entry_source, candidate_tracking.entry_source),
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                ''',
                (
                    trade_date,
                    item['code'],
                    item['name'],
                    item.get('sector'),
                    item.get('stage'),
                    item.get('role'),
                    item.get('tier') or filt.get('tier'),
                    safe_float(item.get('rr')) or filt.get('rr_value'),
                    safe_float(item.get('close_price')),
                    entry_plan.get('entry_ref'),
                    entry_plan.get('entry_low'),
                    entry_plan.get('entry_high'),
                    entry_plan.get('entry_source'),
                    json.dumps({
                        'summary_path': str(summary_path) if summary_path else None,
                        'analysis_path': str(analysis_path) if analysis_path else None,
                        'filter': filt,
                        'entry_plan': entry_plan,
                    }, ensure_ascii=False),
                    now,
                    now,
                )
            )
            inserted += 1
        conn.commit()
    conn.close()
    update_outcomes(limit=5000, max_horizon=20)
    return {'backfilled_days': touched_days, 'records': inserted}


def run_tracking_maintenance(backfill_days=120, update_limit=5000, max_horizon=20):
    backfill = backfill_from_close_summary_notes(limit_days=backfill_days)
    update = update_outcomes(limit=update_limit, max_horizon=max_horizon)
    return {
        'backfill': backfill,
        'update': update,
    }
