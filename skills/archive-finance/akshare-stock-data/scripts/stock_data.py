#!/usr/bin/env python3
import sys
import akshare as ak

mode = sys.argv[1] if len(sys.argv) > 1 else 'spot'

if mode == 'spot':
    df = ak.stock_zh_a_spot_em()
    cols = [c for c in ['代码','名称','最新价','涨跌幅','成交额','换手率','量比'] if c in df.columns]
    print(df[cols].head(30).to_string(index=False))
elif mode == 'hist':
    symbol = sys.argv[2] if len(sys.argv) > 2 else '000001'
    start = sys.argv[3] if len(sys.argv) > 3 else '20250101'
    end = sys.argv[4] if len(sys.argv) > 4 else '20251231'
    df = ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date=start, end_date=end, adjust='qfq')
    print(df.tail(30).to_string(index=False))
elif mode == 'financial':
    symbol = sys.argv[2] if len(sys.argv) > 2 else '000001'
    df = ak.stock_financial_abstract_ths(symbol=symbol, indicator='按报告期')
    print(df.head(30).to_string(index=False))
else:
    print('Usage: stock_data.py [spot|hist SYMBOL START END|financial SYMBOL]')
    sys.exit(1)
