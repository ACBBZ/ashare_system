#!/usr/bin/env python3
import sys
import time
import akshare as ak
import pandas as pd

symbol = sys.argv[1] if len(sys.argv) > 1 else '600519'
start = sys.argv[2] if len(sys.argv) > 2 else '20240101'
end = sys.argv[3] if len(sys.argv) > 3 else '20261231'
include_financial = '--financial' in sys.argv[4:]
cost = None
for arg in sys.argv[4:]:
    if arg.startswith('--cost='):
        try:
            cost = float(arg.split('=', 1)[1])
        except ValueError:
            pass


def with_retry(fn, attempts=3, delay=2):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last


def classify_bar(row):
    op = float(row['开盘'])
    close = float(row['收盘'])
    high = float(row['最高'])
    low = float(row['最低'])
    body = abs(close - op)
    full = max(high - low, 1e-9)
    upper = high - max(op, close)
    lower = min(op, close) - low
    if body / full < 0.15:
        return '十字/犹豫'
    if close > op and upper / full > 0.35:
        return '长上影阳线'
    if close < op and lower / full > 0.35:
        return '长下影阴线'
    if close > op and body / full > 0.6:
        return '实体阳线'
    if close < op and body / full > 0.6:
        return '实体阴线'
    return '普通K线'


def nearest_levels(current, values, pct_band=0.12):
    values = sorted(set(round(float(v), 2) for v in values if pd.notna(v) and v > 0))
    lower = [v for v in values if v <= current and v >= current * (1 - pct_band)]
    upper = [v for v in values if v >= current and v <= current * (1 + pct_band)]
    return lower[-3:], upper[:3]


def load_hist(symbol: str, start: str, end: str):
    try:
        return with_retry(lambda: ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date=start, end_date=end, adjust='qfq'))
    except Exception:
        market_symbol = symbol
        if not symbol.startswith(('sh', 'sz', 'bj')) and len(symbol) == 6:
            if symbol.startswith(('6', '9')):
                market_symbol = f'sh{symbol}'
            elif symbol.startswith(('0', '2', '3')):
                market_symbol = f'sz{symbol}'
        tx = with_retry(lambda: ak.stock_zh_a_hist_tx(symbol=market_symbol, start_date=start, end_date=end, adjust='qfq'))
        tx = tx.rename(columns={'date': '日期', 'open': '开盘', 'close': '收盘', 'high': '最高', 'low': '最低', 'amount': '成交量'})
        return tx

df = load_hist(symbol, start, end)
close_col = '收盘'
vol_col = '成交量'
for n in [5, 10, 20, 30, 60, 120]:
    df[f'MA{n}'] = df[close_col].rolling(n).mean()
    df[f'VOL_MA{n}'] = df[vol_col].rolling(n).mean()

df['K线标签'] = df.apply(classify_bar, axis=1)
last = df.iloc[-1]
recent60 = df.tail(60).copy()
recent120 = df.tail(120).copy()
current = float(last[close_col])

candidates_3m = list(recent60['最低'].tail(20)) + list(recent60['最高'].tail(20)) + [last['MA5'], last['MA10'], last['MA20'], last['MA30']]
candidates_6m = list(recent120['最低'].tail(40)) + list(recent120['最高'].tail(40)) + [last['MA20'], last['MA30'], last['MA60'], last['MA120']]
support_3m, resistance_3m = nearest_levels(current, candidates_3m)
support_6m, resistance_6m = nearest_levels(current, candidates_6m, pct_band=0.2)

print('=== LATEST DAILY SNAPSHOT ===')
cols = ['日期', '开盘', '收盘', '最高', '最低', '成交量', 'MA5', 'MA10', 'MA20', 'MA30', 'VOL_MA5', 'VOL_MA10', 'K线标签']
print(df.tail(20)[cols].to_string(index=False))

print('\n=== TREND SUMMARY ===')
print({
    'current_price': round(current, 2),
    'ma5': round(float(last['MA5']), 2) if pd.notna(last['MA5']) else None,
    'ma10': round(float(last['MA10']), 2) if pd.notna(last['MA10']) else None,
    'ma20': round(float(last['MA20']), 2) if pd.notna(last['MA20']) else None,
    'ma30': round(float(last['MA30']), 2) if pd.notna(last['MA30']) else None,
    'ma_alignment': '多头' if last['MA5'] >= last['MA10'] >= last['MA20'] >= last['MA30'] else ('空头' if last['MA5'] <= last['MA10'] <= last['MA20'] <= last['MA30'] else '胶着'),
    'volume_vs_ma5': round(float(last[vol_col] / last['VOL_MA5']), 2) if pd.notna(last['VOL_MA5']) and last['VOL_MA5'] else None,
    'latest_bar': last['K线标签'],
})

print('\n=== 3M SUPPORT / RESISTANCE ===')
print({'support_3m': support_3m, 'resistance_3m': resistance_3m})

print('\n=== 6M SUPPORT / RESISTANCE ===')
print({'support_6m': support_6m, 'resistance_6m': resistance_6m})

if cost is not None:
    pnl = (current - cost) / cost * 100 if cost else None
    print('\n=== COST PERSPECTIVE ===')
    print({
        'cost': cost,
        'current_price': round(current, 2),
        'pnl_pct': round(pnl, 2) if pnl is not None else None,
        'cost_vs_price': '盈利' if current > cost else ('亏损' if current < cost else '持平'),
        'cost_position_hint': '成本处于上方压力区域' if cost > current else '成本处于下方支撑/回撤参考区域'
    })

if include_financial:
    try:
        fin = with_retry(lambda: ak.stock_financial_abstract_ths(symbol=symbol, indicator='按报告期'))
        print('\n=== FINANCIAL ABSTRACT ===')
        print(fin.head(20).to_string(index=False))
    except Exception as e:
        print(f'\n[WARN] financial abstract unavailable: {e}')
else:
    print('\n[INFO] skip financial abstract; append --financial to fetch it')
