#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

import ashare_ledger_lib as ledger


def cmd_init(_args):
    ledger.init_db()
    print(json.dumps({'ok': True, 'db_path': str(ledger.DB_PATH)}, ensure_ascii=False))


def cmd_add_trade(args):
    trade_id = ledger.add_trade(
        trade_date=args.trade_date,
        trade_time=args.trade_time,
        symbol=args.symbol,
        name=args.name,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        fees=args.fees,
        note=args.note,
        source=args.source,
        asset_type=args.asset_type,
    )
    print(json.dumps({'ok': True, 'trade_id': trade_id}, ensure_ascii=False))


def cmd_show_positions(args):
    positions = ledger.compute_positions(as_of_date=args.trade_date)
    rows = [p for p in positions.values() if p['quantity'] > 0]
    print(json.dumps({'ok': True, 'trade_date': args.trade_date, 'positions': rows}, ensure_ascii=False, indent=2))


def cmd_report(args):
    summary = ledger.build_daily_report_markdown(trade_date=args.trade_date)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_parse_text(args):
    inserted = ledger.record_trade_message(
        args.text,
        default_trade_date=args.trade_date,
        default_trade_time=args.trade_time,
        source='natural_language_cli',
    )
    print(json.dumps({'ok': True, 'inserted': inserted}, ensure_ascii=False, indent=2))


def cmd_bootstrap(args):
    text = Path(args.file).read_text(encoding='utf-8') if args.file else (args.text or '')
    holdings = ledger.parse_holdings_line(text)
    if not holdings:
        raise SystemExit('No holdings parsed from input')
    inserted = []
    for h in holdings:
        trade_id = ledger.add_trade(
            trade_date=args.trade_date,
            trade_time='09:30:00',
            symbol=args.symbol_map.get(h['name'], h.get('code', '')) if hasattr(args, 'symbol_map') else h.get('code', ''),
            name=h['name'],
            side='buy',
            quantity=h['shares'],
            price=h['cost'],
            fees=0,
            note='bootstrap_current_holding',
            source='bootstrap_from_text',
            asset_type=None,
        )
        inserted.append({'trade_id': trade_id, 'name': h['name'], 'shares': h['shares'], 'cost': h['cost']})
    print(json.dumps({'ok': True, 'inserted': inserted}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='A-share ledger CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('init')
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('add-trade')
    p.add_argument('--trade-date', required=True)
    p.add_argument('--trade-time', default=None)
    p.add_argument('--symbol', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--side', choices=['buy', 'sell'], required=True)
    p.add_argument('--quantity', type=int, required=True)
    p.add_argument('--price', type=float, required=True)
    p.add_argument('--fees', type=float, default=0)
    p.add_argument('--note', default='')
    p.add_argument('--source', default='manual')
    p.add_argument('--asset-type', default=None)
    p.set_defaults(func=cmd_add_trade)

    p = sub.add_parser('show-positions')
    p.add_argument('--trade-date', default=ledger.today_str())
    p.set_defaults(func=cmd_show_positions)

    p = sub.add_parser('report')
    p.add_argument('--trade-date', default=ledger.today_str())
    p.set_defaults(func=cmd_report)

    p = sub.add_parser('parse-text')
    p.add_argument('--text', required=True)
    p.add_argument('--trade-date', default=ledger.today_str())
    p.add_argument('--trade-time', default=None)
    p.set_defaults(func=cmd_parse_text)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
