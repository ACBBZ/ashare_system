# 持仓/候选盘后分析时间穿越审计修复记录（2026-05）

## 背景
用户要求对 A 股自动化链路做“时间穿越”审计。针对持仓/候选盘后分析，核心约束是：盘后分析必须使用当日行情；当天主数据源失败时可切换其他当天数据源，但不能回退前一交易日价格并伪装为今日行情。

## 已验证的最小修复模式
在 `/home/admin/.hermes/scripts/ashare_position_watch_analysis.py` 中采用最小改动：

1. strict_today 保持在分析层：
   - `analyze_stock()` / `analyze_fund()` 中使用 `validate_data_asof(..., strict_today=True, allow_previous_close_only=False)`。
   - 今日 quote 缺失且历史 K 线只到前一交易日时，返回结构化缺失结果，而不是继续输出完整技术判断。
2. 缺失结果统一字段：
   - `market_data_missing=True`
   - `technical_missing=True`
   - `trend='今日行情缺失，未生成技术判断'`
   - `data_note` 明确“不使用前一交易日价格替代”。
3. 报告顶部新增统一数据时间元信息：
   - 独立函数：`build_position_watch_report_metadata(now=None, target_date=None, analyses=None)`
   - 报告标题后、正文主要分析内容前插入。
   - 根据 `analyses` 中任一 `market_data_missing` 或 `technical_missing` 判定 `数据完整性：存在今日行情缺失`，否则 `正常`。
   - 元信息明确写：`行情日期要求：必须为当日行情`、`是否允许回退前一交易日价格：否`。

## 推荐测试覆盖
新增或维护：`tests/test_position_watch_report_metadata.py`

至少覆盖：
- 全部标的行情正常时：`数据完整性：正常`
- 存在 `market_data_missing=True` 时：`存在今日行情缺失`
- 存在 `technical_missing=True` 时：`存在今日行情缺失`
- 元信息明确显示：`是否允许回退前一交易日价格：否`
- 元信息位于标题之后、`## 方法说明` 等主要内容之前
- 回归 strict_today：`tests/test_position_watch_analysis_strict_today.py`

## 回归命令
```bash
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_report_metadata.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_position_watch_analysis_strict_today.py -q
python -m pytest /home/admin/.hermes/scripts/tests/test_data_asof_validation.py -q
python -m py_compile /home/admin/.hermes/scripts/ashare_position_watch_analysis.py /home/admin/.hermes/scripts/tests/test_position_watch_report_metadata.py
```

## 操作边界
这类修复不要顺手做以下事情：
- 不修改 cron 配置
- 不修改数据库结构
- 不重构项目结构
- 不改变报告整体章节结构
- 不修改盘前简报、收盘摘要或 09:26 开盘操作表
- 不做 DB latest 查询的 trade_date 过滤（那是单独任务）

## 无 git 环境回滚习惯
若 `/home/admin/.hermes/scripts` 不是 git 仓库，改动前先创建时间戳备份：

```bash
cp ashare_position_watch_analysis.py ashare_position_watch_analysis.py.bak_task5B_$(date +%Y%m%d_%H%M%S)
```

回滚主脚本：
```bash
cp <backup-path> /home/admin/.hermes/scripts/ashare_position_watch_analysis.py
```
