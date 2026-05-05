#!/usr/bin/env python3
import argparse
import sys, os
import akshare as ak
import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from helpers import pct_change, output_payload, now_text

def main():
    parser = argparse.ArgumentParser(description='Strategy analysis based on AKShare')
    parser.add_argument('--code', required=True)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--start', default='20240101')
    parser.add_argument('--end', default='20300101')
    args = parser.parse_args()

    code = args.code.strip()
    payload = {'module': 'strategy_analysis', 'code': code, 'generated_at': now_text()}
    df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=args.start, end_date=args.end, adjust='qfq')
    if df is None or df.empty:
        payload['error'] = '未获取到历史行情'
        output_payload(payload, args.json)
        return

    df = df.copy()
    close_col = '收盘'
    vol_col = '成交量'
    df['MA5'] = df[close_col].rolling(5).mean()
    df['MA20'] = df[close_col].rolling(20).mean()
    df['MA60'] = df[close_col].rolling(60).mean()
    df['VOL5'] = df[vol_col].rolling(5).mean() if vol_col in df.columns else None

    last = df.iloc[-1]
    prev_20 = df.iloc[-20][close_col] if len(df) >= 20 else None
    prev_60 = df.iloc[-60][close_col] if len(df) >= 60 else None
    latest_close = float(last[close_col])
    ma5 = float(last['MA5']) if pd.notna(last['MA5']) else None
    ma20 = float(last['MA20']) if pd.notna(last['MA20']) else None
    ma60 = float(last['MA60']) if pd.notna(last['MA60']) else None
    trend_20 = pct_change(latest_close, prev_20)
    trend_60 = pct_change(latest_close, prev_60)
    latest_vol = float(last[vol_col]) if vol_col in df.columns and pd.notna(last[vol_col]) else None
    vol5 = float(last['VOL5']) if 'VOL5' in df.columns and pd.notna(last['VOL5']) else None
    vol_ratio = (latest_vol / vol5) if latest_vol is not None and vol5 not in (None, 0) else None

    regime = '震荡'
    if ma5 and ma20 and ma60:
        if latest_close > ma5 > ma20 > ma60:
            regime = '多头趋势'
        elif latest_close < ma5 < ma20 < ma60:
            regime = '空头趋势'

    tactical = []
    if trend_20 is not None and trend_20 > 8:
        tactical.append(f'近20日动量偏强（{trend_20:.2f}%）')
    if trend_60 is not None and trend_60 < -10:
        tactical.append(f'近60日仍偏弱（{trend_60:.2f}%），反弹持续性需验证')
    if vol_ratio is not None:
        if vol_ratio >= 1.5:
            tactical.append(f'量能放大（量比近似 {vol_ratio:.2f}）')
        elif vol_ratio < 0.8:
            tactical.append(f'量能偏弱（量比近似 {vol_ratio:.2f}）')
    if not tactical:
        tactical.append('当前技术与量能信号偏中性，建议结合事件与基本面进一步确认。')

    payload['strategy_snapshot'] = {
        'latest_close': latest_close,
        'ma5': ma5,
        'ma20': ma20,
        'ma60': ma60,
        'trend_20d_pct': trend_20,
        'trend_60d_pct': trend_60,
        'volume_ratio_approx': vol_ratio,
        'regime': regime,
    }
    payload['tactical_observations'] = tactical
    payload['recent_bars'] = df.tail(20).to_dict(orient='records')
    output_payload(payload, args.json)

if __name__ == '__main__':
    main()
