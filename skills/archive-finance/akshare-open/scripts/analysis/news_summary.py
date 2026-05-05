#!/usr/bin/env python3
import argparse
import sys, os
import akshare as ak

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from helpers import recent_rows, first_existing, output_payload, now_text

def row_title(row):
    return first_existing(row, ['新闻标题', '标题', 'name', '股票名称', '关键词', '报告名称'], '')

def main():
    parser = argparse.ArgumentParser(description='Finance/news summary based on AKShare')
    parser.add_argument('--code', help='Optional stock code')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    payload = {'module': 'news_summary', 'code': args.code, 'generated_at': now_text()}
    errors = {}

    market_items = []
    hot_rank = []
    hot_keywords = []
    stock_news = []
    research = []

    try:
        research = recent_rows(ak.stock_research_report_em(), 8)
    except Exception as e:
        errors['research_report'] = str(e)
    try:
        hot_rank = recent_rows(ak.stock_hot_rank_em(), 10)
    except Exception as e:
        errors['hot_rank'] = str(e)
    try:
        hot_keywords = recent_rows(ak.stock_hot_keyword_em(), 10)
    except Exception as e:
        errors['hot_keyword'] = str(e)
    if args.code:
        try:
            stock_news = recent_rows(ak.stock_news_em(symbol=args.code.strip()), 10)
        except Exception as e:
            errors['stock_news'] = str(e)

    if stock_news:
        market_items.extend([row_title(x) for x in stock_news[:5] if row_title(x)])
    market_items.extend([row_title(x) for x in research[:5] if row_title(x)])
    market_items.extend([row_title(x) for x in hot_rank[:5] if row_title(x)])
    market_items.extend([row_title(x) for x in hot_keywords[:5] if row_title(x)])

    seen = []
    for item in market_items:
        if item and item not in seen:
            seen.append(item)

    summary_lines = []
    if args.code and stock_news:
        summary_lines.append(f'个股 {args.code} 最近新闻较活跃，可优先核对公告、研报与资金流是否共振。')
    if hot_rank:
        summary_lines.append('热度榜可用于识别当前市场关注焦点，但需与成交额和基本面交叉验证。')
    if hot_keywords:
        summary_lines.append('热门关键词可用于提炼主题线索，再追踪板块与个股扩散。')
    if research:
        summary_lines.append('最新研报能补充卖方观点，但需要与原始财务数据和公告核验。')
    if not summary_lines:
        summary_lines.append('当前未抓到足够新闻样本，建议稍后重试或结合网页检索。')

    payload['summary'] = summary_lines
    payload['headline_candidates'] = seen[:12]
    payload['stock_news'] = stock_news[:8]
    payload['research_reports'] = research[:8]
    payload['hot_rank'] = hot_rank[:8]
    payload['hot_keywords'] = hot_keywords[:8]
    payload['errors'] = errors
    output_payload(payload, args.json)

if __name__ == '__main__':
    main()
