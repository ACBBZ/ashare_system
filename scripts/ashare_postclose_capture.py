#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path('/home/admin/.hermes/scripts/ashare_background_monitor.py')
spec = importlib.util.spec_from_file_location('ashare_background_monitor', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def main():
    current = mod.now_local()
    trade_date = current.date().isoformat()
    if mod.adu.skip_cron_if_not_a_share_trading_day(trade_date, task='ashare-postclose-capture'):
        return
    paths = mod.ensure_day_paths(trade_date)

    df_raw, fetch_method, prior_errors = mod.fetch_spot_df()
    df = mod.standardize_df(df_raw)
    if df.empty:
        raise RuntimeError('fetched dataframe is empty after standardization')

    index_items, index_errors = mod.fetch_index_snapshots()
    sector_items, sector_errors = mod.fetch_sector_snapshots(limit=8, df=df)
    watchlist_targets = mod.parse_watchlist_targets()
    watchlist_items = mod.build_watchlist_snapshots(df, watchlist_targets)
    summary = mod.build_summary(df, fetch_method, current, index_items, sector_items, watchlist_items)
    mod.append_snapshot(summary, paths['snapshots_path'])
    mod.write_latest_summary(summary, paths['latest_summary_path'])
    run_id = mod.insert_db(summary, df, index_items, sector_items, watchlist_items)

    result = {
        'status': 'captured_postclose',
        'captured_at': summary['captured_at'],
        'trade_date': summary['trade_date'],
        'fetch_method': fetch_method,
        'run_id': run_id,
        'total_stocks': summary['total_stocks'],
        'main_board_count': summary['main_board_count'],
        'up_count': summary['up_count'],
        'down_count': summary['down_count'],
        'flat_count': summary['flat_count'],
        'strong_up_count': summary['strong_up_count'],
        'strong_down_count': summary['strong_down_count'],
        'sector_snapshot_count': len(sector_items),
        'watchlist_snapshot_count': len(watchlist_items),
        'snapshots_path': str(paths['snapshots_path']),
        'latest_summary_path': str(paths['latest_summary_path']),
        'db_path': str(mod.DB_PATH),
        'prior_fetch_errors': prior_errors + index_errors + sector_errors + (summary.get('anomaly_mapping_errors') or []),
    }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
