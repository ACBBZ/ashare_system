#!/usr/bin/env python3
import argparse
import sys, os
import akshare as ak
import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from helpers import eastmoney_code, latest_row, first_existing, as_float, output_payload, now_text

def label_value(v, low, high, low_label='偏低', high_label='偏高'):
    if v is None:
        return '未知'
    if v <= low:
        return low_label
    if v >= high:
        return high_label
    return '中性'

def main():
    parser = argparse.ArgumentParser(description='Valuation analysis based on AKShare')
    parser.add_argument('--code', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    code = args.code.strip()

    payload = {'module': 'valuation_analysis', 'code': code, 'generated_at': now_text()}
    notes = []

    try:
        valuation_df = ak.stock_zh_valuation_comparison_em(symbol=code)
        valuation_rows = valuation_df.to_dict(orient='records') if isinstance(valuation_df, pd.DataFrame) and not valuation_df.empty else []
    except Exception as e:
        valuation_rows = []
        notes.append(f'估值对比接口失败: {e}')

    try:
        indicator_df = ak.stock_financial_analysis_indicator_em(symbol=eastmoney_code(code), indicator='按报告期')
        latest_ind = latest_row(indicator_df)
    except Exception as e:
        latest_ind = {}
        notes.append(f'财务指标接口失败: {e}')

    latest_val = valuation_rows[0] if valuation_rows else {}
    pe = as_float(first_existing(latest_val, ['市盈率', 'PE', 'pe', '滚动市盈率']))
    pb = as_float(first_existing(latest_val, ['市净率', 'PB', 'pb']))
    ps = as_float(first_existing(latest_val, ['市销率', 'PS', 'ps']))
    roe = as_float(first_existing(latest_ind, ['净资产收益率(%)', '净资产收益率-摊薄(%)']))
    gross_margin = as_float(first_existing(latest_ind, ['销售毛利率(%)', '毛利率(%)']))

    valuation_view = {
        'pe': pe,
        'pb': pb,
        'ps': ps,
        'roe_pct': roe,
        'gross_margin_pct': gross_margin,
        'pe_view': label_value(pe, 15, 35),
        'pb_view': label_value(pb, 1.5, 5),
        'ps_view': label_value(ps, 2, 8),
    }
    if roe is not None and pe is not None:
        if roe >= 15 and pe <= 25:
            notes.append('ROE 与 PE 组合相对均衡，估值压力不算特别大。')
        elif roe < 8 and pe >= 30:
            notes.append('低盈利质量叠加高估值，需防范估值压缩。')
    if gross_margin is not None and gross_margin < 15:
        notes.append('毛利率偏低时，估值容忍度通常也会下降。')
    if not notes:
        notes.append('估值模块更适合做相对比较，不应单独作为买卖依据。')

    payload['valuation_view'] = valuation_view
    payload['valuation_records'] = valuation_rows[:10]
    payload['latest_indicator_row'] = latest_ind
    payload['notes'] = notes
    output_payload(payload, args.json)

if __name__ == '__main__':
    main()
