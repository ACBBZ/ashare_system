import importlib.util
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_opening_action_table.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_opening_action_table', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_opening_report_metadata_normal_window_marks_not_out_of_window():
    now = datetime(2026, 5, 6, 9, 26, 7, tzinfo=mod.CST)
    warning = mod.build_opening_window_warning(now=now, target_date='2026-05-06')
    metadata = mod.build_opening_report_metadata(now=now, target_date='2026-05-06', window_warning=warning)

    assert '> 数据日期：2026-05-06' in metadata
    assert '> 生成时间：2026-05-06 09:26:07' in metadata
    assert '> 报告类型：09:26 开盘操作表' in metadata
    assert '> 运行窗口：09:25 - 09:35' in metadata
    assert '> 是否超出窗口：否' in metadata
    assert '> 行情数据说明：使用当前任务可获得的开盘/竞价阶段数据' in metadata
    assert '> 数据完整性：正常' in metadata


def test_opening_report_metadata_before_window_marks_early_window():
    now = datetime(2026, 5, 6, 9, 20, 0, tzinfo=mod.CST)
    warning = mod.build_opening_window_warning(now=now, target_date='2026-05-06')
    metadata = mod.build_opening_report_metadata(now=now, target_date='2026-05-06', window_warning=warning)

    assert '> 是否超出窗口：是（早于开盘操作窗口）' in metadata
    assert '早于开盘操作窗口' in metadata


def test_opening_report_metadata_after_window_marks_late_window():
    now = datetime(2026, 5, 6, 10, 0, 0, tzinfo=mod.CST)
    warning = mod.build_opening_window_warning(now=now, target_date='2026-05-06')
    metadata = mod.build_opening_report_metadata(now=now, target_date='2026-05-06', window_warning=warning)

    assert '> 是否超出窗口：是（超过开盘操作窗口）' in metadata
    assert '超过开盘操作窗口' in metadata


def test_build_markdown_places_metadata_between_warning_and_priority_summary():
    markdown, _, _ = mod.build_markdown(
        {},
        '',
        {},
        [],
        [],
        [],
        [],
        [],
        None,
        None,
        now=datetime(2026, 5, 6, 10, 0, 0, tzinfo=mod.CST),
        target_date='2026-05-06',
    )

    title_pos = markdown.index('# A股 09:26 操作表 - 2026-05-06 10:00')
    warning_pos = markdown.index('⚠️ 本报告生成时间已超过开盘操作窗口')
    metadata_pos = markdown.index('> 数据日期：2026-05-06')
    summary_pos = markdown.index('## 0. 一眼执行结论')

    assert title_pos < warning_pos < metadata_pos < summary_pos
    assert '> 是否超出窗口：是（超过开盘操作窗口）' in markdown


def test_opening_window_warning_logic_still_returns_empty_inside_window():
    warning = mod.build_opening_window_warning(
        now=datetime(2026, 5, 6, 9, 26, 0, tzinfo=mod.CST),
        target_date='2026-05-06',
    )

    assert warning == ''
