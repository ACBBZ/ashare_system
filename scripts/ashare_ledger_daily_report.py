#!/usr/bin/env python3
import json

import ashare_data_utils as adu
import ashare_ledger_lib as ledger


def main():
    if adu.skip_cron_if_not_a_share_trading_day(task='ashare-ledger-daily-pnl-feishu'):
        return
    ledger.init_db()
    summary = ledger.build_daily_report_markdown()
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
