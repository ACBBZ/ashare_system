#!/usr/bin/env python3
import argparse
import sys, os
import akshare as ak
import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from helpers import eastmoney_code, latest_row, recent_rows, first_existing, as_float, pct_change, output_payload, now_text


def main():
    parser = argparse.ArgumentParser(description='Fundamental analysis based on AKShare')
    parser.add_argument('--code', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    code = args.code.strip()
    payload = {'module': 'fundamental_analysis', 'code': code, 'generated_at': now_text()}
    notes = []
    errors = {}

    try:
        abstract_df = ak.stock_financial_abstract_ths(symbol=code, indicator='按报告期')
    except Exception as e:
        abstract_df = pd.DataFrame()
        errors['financial_abstract'] = str(e)

    try:
        indicator_df = ak.stock_financial_analysis_indicator_em(symbol=eastmoney_code(code), indicator='按报告期')
    except Exception as e:
        indicator_df = pd.DataFrame()
        errors['financial_indicator'] = str(e)

    try:
        hist_df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date='20240101', end_date='20300101', adjust='qfq')
    except Exception as e:
        hist_df = pd.DataFrame()
        errors['price_history'] = str(e)

    latest_abs = latest_row(abstract_df)
    latest_ind = latest_row(indicator_df)
    hist_tail = hist_df.tail(60).copy() if isinstance(hist_df, pd.DataFrame) else pd.DataFrame()
    close_col = '收盘'
    if close_col in hist_tail.columns and not hist_tail.empty:
        hist_tail['MA20'] = hist_tail[close_col].rolling(20).mean()
        hist_tail['MA60'] = hist_tail[close_col].rolling(60).mean()
        latest_close = float(hist_tail.iloc[-1][close_col])
        close_20d = float(hist_tail.iloc[-20][close_col]) if len(hist_tail) >= 20 else None
        trend_20d = pct_change(latest_close, close_20d)
    else:
        latest_close = None
        trend_20d = None

    revenue = first_existing(latest_abs, ['营业总收入', '营业收入'])
    net_profit = first_existing(latest_abs, ['归属于母公司股东的净利润', '净利润'])
    gross_margin = first_existing(latest_ind, ['销售毛利率(%)', '毛利率(%)', 'XSMLL'])
    roe = first_existing(latest_ind, ['净资产收益率(%)', '净资产收益率-摊薄(%)', 'ROEJQ'])
    debt_ratio = first_existing(latest_ind, ['资产负债率(%)', 'ZCFZL'])
    ocf_per_share = first_existing(latest_ind, ['每股经营性现金流(元)', 'MGJYXJJE'])
    eps = first_existing(latest_ind, ['基本每股收益(元)', '每股收益(元)', 'EPSJB'])

    strengths = []
    risks = []
    roe_f = as_float(roe)
    gm_f = as_float(gross_margin)
    debt_f = as_float(debt_ratio)
    if roe_f is not None:
        if roe_f >= 15:
            strengths.append(f'ROE 较高（{roe_f:.2f}%）')
        elif roe_f < 8:
            risks.append(f'ROE 偏弱（{roe_f:.2f}%）')
    if gm_f is not None:
        if gm_f >= 30:
            strengths.append(f'毛利率较高（{gm_f:.2f}%）')
        elif gm_f < 15:
            risks.append(f'毛利率偏低（{gm_f:.2f}%）')
    if debt_f is not None:
        if debt_f <= 55:
            strengths.append(f'资产负债率可控（{debt_f:.2f}%）')
        elif debt_f >= 70:
            risks.append(f'资产负债率偏高（{debt_f:.2f}%）')
    if trend_20d is not None:
        if trend_20d > 10:
            strengths.append(f'近20日价格趋势较强（{trend_20d:.2f}%）')
        elif trend_20d < -10:
            risks.append(f'近20日价格走弱（{trend_20d:.2f}%）')

    if not strengths:
        notes.append('未从当前可得指标中提炼出显著基本面强项，需结合行业和历史口径进一步判断')
    if not risks:
        notes.append('当前可得指标未显示明显基本面风险，但仍需结合公告与行业周期判断')

    payload['summary'] = {
        'revenue': revenue,
        'net_profit': net_profit,
        'gross_margin_pct': gross_margin,
        'roe_pct': roe,
        'debt_ratio_pct': debt_ratio,
        'operating_cashflow_per_share': ocf_per_share,
        'eps': eps,
        'latest_close': latest_close,
        'trend_20d_pct': trend_20d,
    }
    payload['strengths'] = strengths
    payload['risks'] = risks
    payload['notes'] = notes
    payload['latest_indicator_row'] = latest_ind
    payload['latest_abstract_row'] = latest_abs
    payload['recent_indicator_rows'] = recent_rows(indicator_df, 5)
    payload['errors'] = errors
    output_payload(payload, args.json)


if __name__ == '__main__':
    main()
