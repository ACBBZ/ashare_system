#!/usr/bin/env python3
import json
from datetime import datetime

import ashare_data_utils as adu
import ashare_strategy_engine as ase


def main():
    today = datetime.now().astimezone().date().isoformat()
    if adu.skip_cron_if_not_a_share_trading_day(today, task='ashare-strategy-tracker-local'):
        return
    result = ase.run_tracking_maintenance(backfill_days=240, update_limit=12000, max_horizon=20)
    payload = {
        'ok': True,
        'generated_at': datetime.now().astimezone().isoformat(),
        'db_path': str(ase.DB_PATH),
        'backfill': result.get('backfill') or {},
        'update': result.get('update') or {},
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
