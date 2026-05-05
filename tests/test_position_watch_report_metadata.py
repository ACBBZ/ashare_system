import importlib.util
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_position_watch_analysis.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_position_watch_analysis', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_position_watch_metadata_normal_when_all_analyses_have_today_market_data(monkeypatch):
    monkeypatch.setattr(mod, 'NOW_DATE', '2026-04-29')
    metadata = mod.build_position_watch_report_metadata(
        now=datetime(2026, 4, 29, 17, 31, 2),
        target_date='2026-04-29',
        analyses=[
            {'item': {'name': '正常A'}, 'market_data_missing': False, 'technical_missing': False},
            {'item': {'name': '正常B'}, 'market_data_missing': False, 'technical_missing': False},
        ],
    )

    assert '> 数据日期：2026-04-29' in metadata
    assert '> 生成时间：2026-04-29 17:31:02' in metadata
    assert '> 报告类型：持仓/候选盘后分析' in metadata
    assert '> 数据完整性：正常' in metadata


def test_position_watch_metadata_flags_market_data_missing(monkeypatch):
    monkeypatch.setattr(mod, 'NOW_DATE', '2026-04-29')
    metadata = mod.build_position_watch_report_metadata(
        now=datetime(2026, 4, 29, 17, 31, 2),
        target_date='2026-04-29',
        analyses=[
            {'item': {'name': '缺行情'}, 'market_data_missing': True, 'technical_missing': True},
        ],
    )

    assert '> 数据完整性：存在今日行情缺失' in metadata
    assert '缺行情' in metadata
    assert '部分标的今日行情缺失，未生成技术判断' in metadata


def test_position_watch_metadata_flags_technical_missing(monkeypatch):
    monkeypatch.setattr(mod, 'NOW_DATE', '2026-04-29')
    metadata = mod.build_position_watch_report_metadata(
        now=datetime(2026, 4, 29, 17, 31, 2),
        target_date='2026-04-29',
        analyses=[
            {'item': {'name': '缺技术'}, 'market_data_missing': False, 'technical_missing': True},
        ],
    )

    assert '> 数据完整性：存在今日行情缺失' in metadata
    assert '缺技术' in metadata


def test_position_watch_metadata_explicitly_disallows_previous_trade_day_fallback(monkeypatch):
    monkeypatch.setattr(mod, 'NOW_DATE', '2026-04-29')
    metadata = mod.build_position_watch_report_metadata(
        now=datetime(2026, 4, 29, 17, 31, 2),
        target_date='2026-04-29',
        analyses=[],
    )

    assert '> 行情日期要求：必须为当日行情' in metadata
    assert '> 是否允许回退前一交易日价格：否' in metadata
    assert '不构成买卖建议' in metadata


def test_position_watch_metadata_inserted_after_title_before_main_content(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, 'NOW_DATE', '2026-04-29')
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    monkeypatch.setattr(mod, 'current_day_dir', lambda: tmp_path / '2026-04-29')
    monkeypatch.setattr(mod, 'find_latest_close_summary', lambda: None)
    monkeypatch.setattr(mod, 'find_latest_holding_summary', lambda: None)
    monkeypatch.setattr(mod, 'load_holdings_from_ledger', lambda: ([], None, None, None))
    monkeypatch.setattr(mod, 'load_recent_review_context', lambda limit=3: [])
    monkeypatch.setattr(mod, 'resolve_codes', lambda items: {})
    monkeypatch.setattr(mod, 'load_sector_constituent_cache', lambda: ({}, {}, {}))
    monkeypatch.setattr(mod, 'parse_candidates', lambda text: [
        {'name': '测试候选', 'code': '600000', 'group': '候选股', 'asset_type': 'stock'}
    ])
    monkeypatch.setattr(mod, 'parse_holdings', lambda text: [])
    monkeypatch.setattr(mod, 'parse_board_stage_map', lambda text: {})
    monkeypatch.setattr(mod, 'parse_candidate_sector_map', lambda text: {})
    monkeypatch.setattr(mod, 'validate_review_source', lambda path, expected_day_dir, label: {
        'label': label,
        'expected_day': '2026-04-29',
        'actual_day': None,
        'is_expected_day': False,
        'used_fallback': False,
        'path': None,
    })
    monkeypatch.setattr(mod, 'analyze_stock', lambda item: {
        'item': item,
        'data_date': '2026-04-29',
        'close': None,
        'change_pct': None,
        'high': None,
        'low': None,
        'trend': '今日行情缺失，未生成技术判断',
        'tomorrow_direction': '今日行情缺失，未生成技术判断',
        'tomorrow_range': None,
        'strategy': '今日行情缺失，未生成技术判断',
        'data_note': '今日行情缺失，未生成技术判断',
        'market_data_missing': True,
        'technical_missing': True,
    })
    monkeypatch.setattr(mod, 'analyze_fund', lambda item: mod.analyze_stock(item))
    monkeypatch.setattr(mod, 'candidate_strength_score', lambda item, analysis: {'score': 0, 'level': 'C', 'reasons': []})
    monkeypatch.setattr(mod, 'candidate_action_tag', lambda item, analysis: '只观察')
    monkeypatch.setattr(mod, 'ledger_lib', None)

    mod.main()

    note_path = tmp_path / '2026-04-29' / '持仓股与候选股分析-2026-04-29.md'
    text = note_path.read_text(encoding='utf-8')
    title_idx = text.index('# 持仓股与候选股分析 - 2026-04-29')
    metadata_idx = text.index('> 数据日期：2026-04-29')
    main_idx = text.index('## 方法说明')

    assert title_idx < metadata_idx < main_idx
    assert '> 数据完整性：存在今日行情缺失' in text
