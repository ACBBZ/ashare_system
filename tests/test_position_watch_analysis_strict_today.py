import importlib.util
import sys
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_position_watch_analysis.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_position_watch_analysis', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _previous_day_hist():
    return pd.DataFrame([
        {'date': '2026-04-27', 'open': 10.0, 'high': 10.5, 'low': 9.8, 'close': 10.2, 'volume': 1000},
        {'date': '2026-04-28', 'open': 10.2, 'high': 10.6, 'low': 10.0, 'close': 10.4, 'volume': 1200},
    ])


def test_analyze_stock_returns_missing_result_when_today_quote_missing_and_hist_is_previous_day(monkeypatch):
    monkeypatch.setattr(mod, 'NOW_DATE', '2026-04-29')
    monkeypatch.setattr(mod, 'TODAY_TS', pd.Timestamp('2026-04-29'))
    monkeypatch.setattr(mod, 'fetch_same_day_stock_quote', lambda code: (_ for _ in ()).throw(RuntimeError('quote down')))
    monkeypatch.setattr(mod.adu, 'ak_call', lambda *args, **kwargs: _previous_day_hist())
    monkeypatch.setattr(mod.adu, 'fetch_hist_df_with_fallback', lambda *args, **kwargs: _previous_day_hist())

    result = mod.analyze_stock({'name': '测试股票', 'code': '600000', 'group': '候选股', 'asset_type': 'stock'})

    assert result['market_data_missing'] is True
    assert result['technical_missing'] is True
    assert result['close'] is None
    assert result['trend'] == '今日行情缺失，未生成技术判断'
    assert '今日行情缺失，未生成技术判断' in result['data_note']
    assert result['asof_validation']['level'] in {'warning', 'error'}


def test_append_missing_market_data_lines_makes_report_status_explicit():
    result = mod.build_missing_market_analysis(
        {'name': '测试股票', 'code': '600000', 'group': '候选股', 'asset_type': 'stock'},
        reason='unit test missing quote',
        source='unit-test',
        context='unit-test',
    )
    lines = []

    mod.append_missing_market_data_lines(lines, result)

    text = '\n'.join(lines)
    assert '今日行情缺失，未生成技术判断' in text
    assert '不使用前一交易日价格替代' in text
