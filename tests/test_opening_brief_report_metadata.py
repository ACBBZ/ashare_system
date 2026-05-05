import importlib.util
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_opening_brief.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_opening_brief', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_opening_brief_metadata_normal_without_warnings(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-30')
    metadata = mod.build_opening_brief_report_metadata(
        now=datetime(2026, 4, 30, 9, 0, 5),
        target_date='2026-04-30',
        data_warnings=None,
    )

    assert '> 数据日期：2026-04-30' in metadata
    assert '> 生成时间：2026-04-30 09:00:05' in metadata
    assert '> 报告类型：盘前简报' in metadata
    assert '> 数据阶段：盘前数据' in metadata
    assert '> 数据完整性：正常' in metadata
    assert '> 缺失说明：无' in metadata


def test_opening_brief_metadata_flags_warnings_as_missing_or_degraded(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-30')
    metadata = mod.build_opening_brief_report_metadata(
        now=datetime(2026, 4, 30, 9, 0, 5),
        target_date='2026-04-30',
        data_warnings=['TrendRadar 本地输出缺失', '公告接口降级'],
    )

    assert '> 数据完整性：存在缺失/存在降级' in metadata
    assert '> 缺失说明：TrendRadar 本地输出缺失；公告接口降级' in metadata


def test_opening_brief_metadata_explicitly_disallows_after_open_market_data(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-30')
    metadata = mod.build_opening_brief_report_metadata(
        now=datetime(2026, 4, 30, 9, 0, 5),
        target_date='2026-04-30',
        data_warnings=[],
    )

    assert '> 行情日期要求：主要使用前一交易日收盘数据 + 当日盘前新闻/公告' in metadata
    assert '> 是否允许使用当日开盘后行情：否' in metadata
    assert '> 新闻/公告时间要求：不晚于报告生成时间' in metadata
    assert '不构成买卖建议' in metadata


def test_opening_brief_metadata_can_include_previous_summary_date(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-30')
    metadata = mod.build_opening_brief_report_metadata(
        now=datetime(2026, 4, 30, 9, 0, 5),
        target_date='2026-04-30',
        previous_summary_date='2026-04-29',
    )

    assert '> 前一日复盘日期：2026-04-29' in metadata


def test_opening_brief_metadata_inserted_after_title_before_first_section(monkeypatch):
    monkeypatch.setattr(mod, 'TODAY', '2026-04-30')
    monkeypatch.setattr(mod, 'NOW', datetime(2026, 4, 30, 9, 0, 5, tzinfo=mod.CST))

    news_pack = {
        'top3': [],
        'ashare_related': [],
        'sentiment': '中性',
        'trendradar_count': 0,
    }
    anomalies = []
    selection = {'priority_sectors': [], 'candidates': []}
    focus_list = {'focus': ['先观察'], 'alt': ['无'], 'avoid': ['不追高']}
    review_ctx = {
        'close_path': 'close-summary.md',
        'hold_path': 'holding.md',
        'analysis_path': None,
        'plan_sectors': [],
    }
    action_plan = {
        'holdings': [],
        'candidates': [],
        'attention': ['控制仓位'],
        'market_env': {
            'market_phase': '轮动市',
            'stage_counts': {'主升': 0, '修复': 0, '轮动': 0, '分歧': 0, '退潮': 0},
            'hard_rules': ['测试规则'],
            'rule_score': 0,
            'action_stance': '轻仓观察',
            'max_position': '10%-20%',
            'should_buy': '只观察',
            'should_sell': '弱则卖',
            'portfolio_advice': '先看持仓',
            'position_ladder': ['轮动：10%-20%'],
            'from_empty_to_light': '确认后再试',
            'from_light_to_half': '暂不加仓',
            'forbidden_trade': '不追高',
            'full_attack_guard': '不满仓',
        },
    }

    markdown = mod.render_markdown(
        news_pack,
        anomalies,
        selection,
        focus_list,
        review_ctx,
        action_plan,
        now=datetime(2026, 4, 30, 9, 0, 5),
        target_date='2026-04-30',
        data_warnings=[],
    )

    title_idx = markdown.index('# A股开盘前简报 - 2026-04-30')
    metadata_idx = markdown.index('> 数据日期：2026-04-30')
    first_section_idx = markdown.index('## 1. 昨夜今晨发生了什么（含 TrendRadar 多源）')

    assert title_idx < metadata_idx < first_section_idx
    assert '> 报告类型：盘前简报' in markdown
    assert '> 数据阶段：盘前数据' in markdown
    assert '> 是否允许使用当日开盘后行情：否' in markdown
