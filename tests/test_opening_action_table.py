import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
MODULE_PATH = SCRIPT_DIR / 'ashare_opening_action_table.py'

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location('ashare_opening_action_table', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_parse_close_summary_supports_latest_summary_hard_filter_format(tmp_path):
    close_path = tmp_path / 'latest-summary.md'
    analysis_path = tmp_path / 'analysis.md'

    close_path.write_text(
        """# A股盘面监控 - 2026-04-28 11:30

## 2. 板块分析
### 基础化工
- 板块涨幅：10.03%
- 板块阶段：主升
### 锂
- 板块涨幅：10.00%
- 板块阶段：主升

## 3. 个股筛选（硬过滤后）
### A层：可执行池
- 今日无满足硬过滤的 A 层候选，次日计划默认以观察为主。
### B层：观察池
- 天齐锂业（002466，锂，主升，龙头）：盈亏比缺失；成交额>=5亿
- 恩捷股份（002812，电池，主升，龙头）：一手成本超过半仓承受度
- 多氟多（002407，基础化工，主升，龙头）：一手成本适合小资金试错
- 莲花控股（600186，食品饮料，主升，龙头）：一手成本适合小资金试错
### C层：仅记录
- 无。

## 4. 次日计划
- 明天看好的 3 个板块：基础化工、电子、食品饮料
""",
        encoding='utf-8',
    )
    analysis_path.write_text('# empty\n', encoding='utf-8')

    result = mod.parse_close_summary(close_path, analysis_path)
    candidates = {item['code']: item for item in result['candidates']}

    assert set(candidates) >= {'002466', '002812', '002407', '600186'}
    assert candidates['002466']['sector'] == '锂'
    assert candidates['002466']['stage'] == '主升'
    assert candidates['002466']['role'] == '龙头'
    assert candidates['002407']['sector'] == '基础化工'
    assert result['boards']['基础化工']['stage'] == '主升'


def test_latest_close_summary_before_today_prefers_structured_close_summary(tmp_path, monkeypatch):
    root = tmp_path / 'notes'
    day_older = root / '2026-04-27'
    day_prev = root / '2026-04-28'
    day_older.mkdir(parents=True)
    day_prev.mkdir(parents=True)
    (day_prev / 'latest-summary.md').write_text('# intraday monitor\n\n## 市场概览\n- 数据缺失\n', encoding='utf-8')
    (day_older / 'close-summary.md').write_text('## 3. 个股筛选（硬过滤后）\n### B层：观察池\n- 天齐锂业（002466，锂，主升，龙头）\n\n## 4. 次日计划\n', encoding='utf-8')

    monkeypatch.setattr(mod, 'ROOT', root)
    monkeypatch.setattr(mod, 'TODAY', '2026-04-29')

    picked = mod.latest_close_summary_before_today()

    assert picked == day_older / 'close-summary.md'


def test_fetch_index_snapshot_from_db_reads_target_date_with_open_and_prev_close(tmp_path, monkeypatch):
    db_path = tmp_path / 'ashare_monitor.db'
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE index_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            captured_at TEXT,
            trade_date TEXT,
            index_code TEXT,
            index_name TEXT,
            latest_value REAL,
            pct_change REAL,
            amount REAL,
            high REAL,
            low REAL,
            raw_json TEXT,
            created_at TEXT
        )
        """
    )
    rows = [
        ('2026-04-29T09:26:00+08:00', '2026-04-29', 'sh000001', '上证指数', 4107.51, 0.708, 1126615000000.0, 4110.0, 4061.8,
         {'代码': 'sh000001', '名称': '上证指数', '最新价': 4107.51, '今开': 4061.82, '昨收': 4078.64, '涨跌幅': 0.708}),
        ('2026-04-29T09:26:00+08:00', '2026-04-29', 'sz399001', '深证成指', 15120.92, 1.959, 1463521000000.0, 15139.7, 14751.4,
         {'代码': 'sz399001', '名称': '深证成指', '最新价': 15120.92, '今开': 14751.42, '昨收': 14830.45, '涨跌幅': 1.959}),
        ('2026-04-29T09:26:00+08:00', '2026-04-29', 'sz399006', '创业板指', 3687.17, 2.515, 679396800000.0, 3692.2, 3578.4,
         {'代码': 'sz399006', '名称': '创业板指', '最新价': 3687.17, '今开': 3578.42, '昨收': 3596.71, '涨跌幅': 2.515}),
    ]
    conn.executemany(
        "INSERT INTO index_snapshots (captured_at, trade_date, index_code, index_name, latest_value, pct_change, amount, high, low, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(captured_at, trade_date, code, name, latest, pct, amount, high, low, json.dumps(raw, ensure_ascii=False), captured_at)
         for captured_at, trade_date, code, name, latest, pct, amount, high, low, raw in rows],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(mod, 'DB_PATH', db_path)
    monkeypatch.setattr(mod.adu, 'ak_call', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('live source down')))

    result = mod.fetch_index_snapshot_from_db(
        target_date='2026-04-29',
        asof_time='2026-04-29T09:26:00+08:00',
    )
    by_name = {item['name']: item for item in result}

    assert by_name['上证指数']['open'] == pytest.approx(4061.82)
    assert by_name['上证指数']['prev_close'] == pytest.approx(4078.64)
    assert by_name['深证成指']['open'] == pytest.approx(14751.42)
    assert by_name['创业板指']['prev_close'] == pytest.approx(3596.71)


def test_fetch_strong_boards_from_db_dedupes_same_sector_family_for_target_date(tmp_path, monkeypatch):
    db_path = tmp_path / 'ashare_monitor.db'
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sector_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            captured_at TEXT,
            trade_date TEXT,
            sector_name TEXT,
            pct_change REAL,
            up_count INTEGER,
            down_count INTEGER,
            leader_name TEXT,
            leader_code TEXT,
            net_inflow REAL,
            raw_json TEXT,
            created_at TEXT
        )
        """
    )
    rows = [
        ('2026-04-29T09:26:00+08:00', '2026-04-29', '证券Ⅱ', 1.40, 46, 1, '广发证券', {'领涨股票-涨跌幅': 6.08, '换手率': 0.75}),
        ('2026-04-29T09:26:00+08:00', '2026-04-29', '证券Ⅲ', 1.40, 46, 1, '广发证券', {'领涨股票-涨跌幅': 6.08, '换手率': 0.75}),
        ('2026-04-29T09:26:00+08:00', '2026-04-29', '氮肥', 4.03, 6, 0, '赤天化', {'领涨股票-涨跌幅': 10.05, '换手率': 5.09}),
    ]
    conn.executemany(
        "INSERT INTO sector_snapshots (captured_at, trade_date, sector_name, pct_change, up_count, down_count, leader_name, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(captured_at, trade_date, name, pct, up_count, down_count, leader_name, json.dumps(raw, ensure_ascii=False), captured_at)
         for captured_at, trade_date, name, pct, up_count, down_count, leader_name, raw in rows],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(mod, 'DB_PATH', db_path)

    boards = mod.fetch_strong_boards_from_db(
        limit=6,
        target_date='2026-04-29',
        asof_time='2026-04-29T09:26:00+08:00',
    )
    names = [item['name'] for item in boards]

    assert '氮肥' in names
    assert sum(name.startswith('证券') for name in names) == 1
