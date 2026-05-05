# A 股时间穿越校验 MVP 经验

## Context

用于 A 股自动化脚本的第一批最小修复任务：先在 `/home/admin/.hermes/scripts/ashare_data_utils.py` 建立统一 `target_date / asof` 校验工具，但不要立即接入盘前、开盘、收盘、持仓分析业务链路。

## Scope Discipline

当用户要求“只做任务 1 / MVP / 不要同时修改任务 2-5”时：

- 不改 Cron 配置。
- 不改数据库结构。
- 不做大规模重构。
- 不改变现有报告输出格式。
- 不把新工具顺手接入所有业务脚本。
- 若目录没有 git，先创建带时间戳的 `.bak_YYYYMMDD_HHMMSS` 备份文件。

## Recommended MVP API

在 `ashare_data_utils.py` 中增加纯工具函数：

- `parse_datetime_safe(value)`：安全解析 `date`、`datetime`、`YYYY-MM-DD`、`YYYY-MM-DD HH:MM:SS`、`None`；失败返回 `None`，不 raise。
- `normalize_trade_date(value)`：归一化为 `YYYY-MM-DD`，失败返回 `None`。
- `validate_data_asof(target_date, data_date=None, captured_at=None, generated_at=None, source=None, strict_today=True, allow_previous_close_only=False, context='')`：返回结构化校验结果，不 raise。

返回结构应至少包含：

```python
{
    'ok': True/False,
    'level': 'ok'/'warning'/'error',
    'reason': '...',
    'target_date': 'YYYY-MM-DD' or None,
    'data_date': 'YYYY-MM-DD' or None,
    'captured_at': 'ISO datetime' or None,
    'generated_at': 'ISO datetime' or None,
    'source': source,
    'context': context,
}
```

## Minimum Rules

- `strict_today=True` 时，`data_date` 必须等于 `target_date`。
- `allow_previous_close_only=True` 时，允许 `data_date < target_date`，但必须返回 `warning` 或明确标记。
- `data_date > target_date` 必须返回 `error`。
- `captured_at` 的日期大于 `target_date` 必须返回 `error`。
- 同时缺少 `data_date` 和 `captured_at` 不应直接通过，应返回 `warning`。
- 所有错误优先返回结构化结果，避免破坏现有 cron/report 链路。

## TDD Pattern

1. 新增测试文件，例如 `/home/admin/.hermes/scripts/tests/test_data_asof_validation.py`。
2. 先运行并确认失败：`python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q`。
3. 只实现最小工具函数。
4. 再运行：
   - `python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q`
   - `python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_table.py -q`
   - `python -m py_compile /home/admin/.hermes/scripts/ashare_data_utils.py /home/admin/.hermes/scripts/tests/test_data_asof_validation.py`

## Verification Output to User

最终说明必须包含：

- 修改了哪些文件。
- 新增了哪些函数和作用。
- 测试结果。
- 是否影响现有 Cron。
- 是否影响现有报告格式。
- 如何回滚，包含备份文件路径。
- 下一步建议接入哪个模块。

## Task 2: 盘后持仓/候选分析 strict-today MVP

当用户要求“盘后持仓/候选分析禁止回退到前一天价格”且限定只做任务 2 时，优先修改 `/home/admin/.hermes/scripts/ashare_position_watch_analysis.py`，仅在必要时调用 `ashare_data_utils.validate_data_asof()`，不要顺手做 DB latest、Cron、schema 或其他任务。

### Recommended Implementation Pattern

- 在业务脚本内新增缺失行情结构化结果构造器，例如 `build_missing_market_analysis(item, reason=..., data_date=..., quote_validation=...)`。
- 当股票/ETF/LOF 的今日 quote 缺失，且历史行情最新 `date` 不是目标交易日时，**不要 raise 导致整份报告中断，也不要使用最后一根前日 bar 继续分析**；返回：
  - `market_data_missing=True`
  - `technical_missing=True`
  - `trend='今日行情缺失，未生成技术判断'`
  - `close=None`
  - `tomorrow_range=None`
  - `asof_validation` 结构化校验结果
- 若当天 quote 可得但历史 K 线/净值未覆盖当天，可以用当天 quote 拼接到历史序列尾部后再计算技术指标；但必须校验 quote 的 `data_date == target_date`。
- 报告渲染层遇到 `market_data_missing` 或 `technical_missing` 时，输出固定缺失说明，而不是格式化 `None` 价格或生成趋势判断：
  - `今日行情缺失，未生成技术判断`
  - `不使用前一交易日价格替代`
  - 明日区间显示 `暂不输出`
- 基金（ETF/LOF）和主板股票都执行同一 strict-today 最新价格规则；基金不能用前一日净值冒充今日场内价格。

### Minimal Tests

新增测试文件建议：`/home/admin/.hermes/scripts/tests/test_position_watch_analysis_strict_today.py`。

覆盖至少：

1. 股票 quote 缺失且历史行情只有前一交易日：返回 `market_data_missing=True`，报告含“今日行情缺失，未生成技术判断”。
2. 基金 quote 缺失或历史净值未覆盖当天：返回 `market_data_missing=True`，不输出趋势/支撑/压力/明日区间。
3. 关联回归：`test_data_asof_validation.py` 与 `test_opening_action_table.py` 继续通过。

推荐验证命令：

```bash
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_analysis_strict_today.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_action_table.py -q
python -m py_compile /home/admin/.hermes/scripts/ashare_position_watch_analysis.py /home/admin/.hermes/scripts/tests/test_position_watch_analysis_strict_today.py
```

### User-Facing Checklist

任务 2 完成后，用户通常需要一份“变更检查清单”，简明列出：是否修改 Cron、是否修改数据库结构、是否修改报告格式、是否影响现有自动任务、修改文件列表、测试命令与结果、备份路径、回滚命令、下一步建议。

## Pitfalls

- 不要为了“复用”而改动现有 `_coerce_iso_date()` 行为，除非用户明确授权。时间穿越校验 MVP 应新增旁路工具，保持现有生产链路行为稳定。
- 不要把“今日行情缺失”处理成整份报告失败。更稳的行为是单标的降级并在报告中显式说明，避免一个数据源缺失拖垮全部持仓/候选分析。
- 不要在缺失今日 quote 时用历史 DataFrame 的最后一行继续计算通道、支撑压力、盈亏比或明日区间；这正是时间穿越风险。
