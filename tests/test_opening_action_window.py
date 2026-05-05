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


def test_opening_window_at_0926_has_no_warning():
    warning = mod.build_opening_window_warning(
        now=datetime(2026, 5, 6, 9, 26, tzinfo=mod.CST),
        target_date='2026-05-06',
    )

    assert not warning


def test_opening_window_before_0925_warns_precheck_only():
    warning = mod.build_opening_window_warning(
        now=datetime(2026, 5, 6, 9, 20, tzinfo=mod.CST),
        target_date='2026-05-06',
    )

    assert '早于开盘操作窗口' in warning
    assert '仅供预检查' in warning


def test_opening_window_after_0935_warns_review_only():
    warning = mod.build_opening_window_warning(
        now=datetime(2026, 5, 6, 10, 0, tzinfo=mod.CST),
        target_date='2026-05-06',
    )

    assert '超过开盘操作窗口' in warning
    assert '仅供复核' in warning


def test_opening_window_warning_does_not_break_markdown_rendering():
    markdown, strategy_text, max_position_text = mod.build_markdown(
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
        now=datetime(2026, 5, 6, 10, 0, tzinfo=mod.CST),
        target_date='2026-05-06',
    )

    assert markdown.startswith('# A股 09:26 操作表 - 2026-05-06 10:00')
    assert '⚠️ 本报告生成时间已超过开盘操作窗口' in markdown
    assert '## 0. 一眼执行结论' in markdown
    assert strategy_text
    assert max_position_text
