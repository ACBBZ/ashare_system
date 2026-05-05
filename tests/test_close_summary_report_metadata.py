import importlib.util
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_close_summary.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_close_summary', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_close_summary_metadata_normal_without_warnings(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-29')
    metadata = mod.build_close_summary_report_metadata(
        now=datetime(2026, 4, 29, 17, 0, 5),
        target_date='2026-04-29',
        data_warnings=None,
    )

    assert '> 数据日期：2026-04-29' in metadata
    assert '> 生成时间：2026-04-29 17:00:05' in metadata
    assert '> 报告类型：收盘摘要' in metadata
    assert '> 数据阶段：收盘后数据' in metadata
    assert '> 数据完整性：正常' in metadata


def test_close_summary_metadata_flags_warnings_as_missing_or_degraded(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-29')
    metadata = mod.build_close_summary_report_metadata(
        now=datetime(2026, 4, 29, 17, 0, 5),
        target_date='2026-04-29',
        data_warnings=['涨停池接口降级', '部分板块资金缺失'],
    )

    assert '> 数据完整性：存在缺失/存在降级' in metadata
    assert '> 缺失说明：涨停池接口降级；部分板块资金缺失' in metadata


def test_close_summary_metadata_explicitly_disallows_previous_day_market_fallback(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-29')
    metadata = mod.build_close_summary_report_metadata(
        now=datetime(2026, 4, 29, 17, 0, 5),
        target_date='2026-04-29',
        data_warnings=[],
    )

    assert '> 行情日期要求：必须为当日收盘/盘中快照数据' in metadata
    assert '> 是否允许回退前一交易日行情：否' in metadata
    assert '> 快照数据说明：使用目标交易日可获得的盘中/收盘快照' in metadata
    assert '不构成买卖建议' in metadata


def test_close_summary_metadata_can_include_capture_info(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-29')
    metadata = mod.build_close_summary_report_metadata(
        now=datetime(2026, 4, 29, 17, 0, 5),
        target_date='2026-04-29',
        capture_info={'run_id': 123, 'captured_at': '2026-04-29 15:01:02'},
    )

    assert '> 快照数据说明：使用目标交易日可获得的盘中/收盘快照；run_id=123；captured_at=2026-04-29 15:01:02' in metadata


def test_close_summary_metadata_inserted_after_title_before_first_section(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-29')
    monkeypatch.setattr(mod.ase, 'classify_market_hard', lambda limit_stats, amount_stats, latest_capture: {
        'market_phase': '轮动',
        'env': '中性',
        'hard_rules': ['测试规则'],
        'score': 0,
    })

    markdown = mod.build_markdown(
        index_spot={},
        amount_stats={},
        limit_stats={},
        latest_capture={},
        sectors=[],
        candidates=[],
        intraday_watchlist=[],
        sector_tiers={},
        scoreboard={},
        now=datetime(2026, 4, 29, 17, 0, 5),
        target_date='2026-04-29',
        data_warnings=[],
    )

    title_idx = markdown.index('# A股收盘摘要 - 2026-04-29')
    metadata_idx = markdown.index('> 数据日期：2026-04-29')
    first_section_idx = markdown.index('## 1. 市场总览')

    assert title_idx < metadata_idx < first_section_idx
    assert '> 报告类型：收盘摘要' in markdown
    assert '> 数据阶段：收盘后数据' in markdown
