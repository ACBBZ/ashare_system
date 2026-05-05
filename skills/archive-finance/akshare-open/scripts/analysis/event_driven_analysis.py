#!/usr/bin/env python3
import argparse
import sys, os
import akshare as ak

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from helpers import recent_rows, output_payload, now_text


def fetch_variants(variants):
    last_error = None
    for fn, kwargs in variants:
        try:
            df = fn(**kwargs)
            return recent_rows(df, 10), None
        except Exception as e:
            last_error = str(e)
    return [], last_error


def main():
    parser = argparse.ArgumentParser(description='Event-driven analysis based on AKShare')
    parser.add_argument('--code', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    code = args.code.strip()
    payload = {'module': 'event_driven_analysis', 'code': code, 'generated_at': now_text()}
    errors = {}

    news, err = fetch_variants([(ak.stock_news_em, {'symbol': code})])
    if err:
        errors['stock_news'] = err

    notices, err = fetch_variants([
        (ak.stock_notice_report, {'symbol': code, 'indicator': '全部'}),
        (ak.stock_notice_report, {'symbol': code}),
    ])
    if err:
        errors['notice_report'] = err

    lhb, err = fetch_variants([
        (ak.stock_lhb_stock_detail_em, {'symbol': code}),
    ])
    if err:
        errors['lhb_stock'] = err

    inst, err = fetch_variants([
        (ak.stock_jgdy_detail_em, {'symbol': code}),
        (ak.stock_jgdy_detail_em, {'stock': code}),
    ])
    if err:
        errors['inst_research'] = err

    holder_change, err = fetch_variants([
        (ak.stock_shareholder_change_ths, {'symbol': code}),
    ])
    if err:
        errors['shareholder_change'] = err

    repurchase, err = fetch_variants([
        (ak.stock_repurchase_em, {'symbol': code}),
        (ak.stock_repurchase_em, {}),
    ])
    if err:
        errors['repurchase'] = err
    if repurchase:
        repurchase = [x for x in repurchase if code in str(x)]

    catalysts = []
    if notices:
        catalysts.append(f'最近抓到 {len(notices)} 条公告，可优先筛查业绩、并购、回购、股权激励等事件')
    if lhb:
        catalysts.append(f'最近抓到 {len(lhb)} 条龙虎榜相关记录，可观察短线博弈资金是否活跃')
    if inst:
        catalysts.append(f'最近抓到 {len(inst)} 条机构调研记录，可跟踪机构关注度变化')
    if holder_change:
        catalysts.append(f'检测到 {len(holder_change)} 条股东变化数据，需关注减持/增持方向')
    if repurchase:
        catalysts.append(f'检测到 {len(repurchase)} 条回购相关记录，可作为资本运作信号参考')
    if news:
        catalysts.append(f'最近抓到 {len(news)} 条新闻，可与股价/成交量异动交叉验证')

    payload['event_snapshot'] = {
        'news_count': len(news),
        'notice_count': len(notices),
        'lhb_count': len(lhb),
        'institutional_research_count': len(inst),
        'shareholder_change_count': len(holder_change),
        'repurchase_count': len(repurchase),
    }
    payload['catalyst_summary'] = catalysts or ['未抓到足够事件数据，建议改用更长时间窗口或结合外部新闻源']
    payload['recent_news'] = news[:5]
    payload['recent_notices'] = notices[:5]
    payload['recent_lhb'] = lhb[:5]
    payload['recent_institutional_research'] = inst[:5]
    payload['recent_shareholder_change'] = holder_change[:5]
    payload['recent_repurchase'] = repurchase[:5]
    payload['errors'] = errors
    output_payload(payload, args.json)


if __name__ == '__main__':
    main()
