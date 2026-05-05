#!/usr/bin/env python3
import sys
import akshare as ak

threshold_pct = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
turnover_threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
limit_n = int(sys.argv[3]) if len(sys.argv) > 3 else 50

df = ak.stock_zh_a_spot_em()
cond = None
if '涨跌幅' in df.columns:
    cond = df['涨跌幅'].abs() >= threshold_pct
if '换手率' in df.columns:
    cond = cond & (df['换手率'] >= turnover_threshold) if cond is not None else (df['换手率'] >= turnover_threshold)
res = df[cond].copy() if cond is not None else df.copy()
cols = [c for c in ['代码','名称','最新价','涨跌幅','涨跌额','成交额','振幅','换手率','量比'] if c in res.columns]
if '涨跌幅' in res.columns:
    res = res.sort_values(by='涨跌幅', ascending=False)
print(res[cols].head(limit_n).to_string(index=False))
