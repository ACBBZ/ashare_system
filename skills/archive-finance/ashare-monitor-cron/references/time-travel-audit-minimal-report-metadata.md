# 时间穿越审计后的最小报告元信息补丁

适用场景：用户要求只给 A 股自动化报告增加统一数据时间元信息，避免读者误以为报告使用了当天数据，但又明确禁止重构、改 cron、改 DB、改候选逻辑或改 SQL latest 查询。

## 已验证模式

对具体报告脚本（例如 `ashare_close_summary.py`、`ashare_opening_brief.py`、`ashare_opening_action_table.py`、`ashare_position_watch_analysis.py`）采用最小补丁：

1. **先确认范围**
   - 只改当前指定报告脚本。
   - 不改其他报告链路。
   - 不改 cron 配置。
   - 不改数据库结构。
   - 不改候选股生成 / A-B-C 分层 / scoreboard 写入。
   - 不在元信息任务中顺手修 DB latest 查询。

2. **无 git 时先备份**
   - 在脚本目录执行 `git rev-parse --is-inside-work-tree 2>/dev/null || true`。
   - 若不是 git 仓库，创建时间戳备份，例如：
     - `ashare_close_summary.py.bak_task5C_YYYYMMDD_HHMMSS`

3. **新增独立、可测试的小函数**
   - 收盘摘要：`build_close_summary_report_metadata(now=None, target_date=None, data_warnings=None, capture_info=None)`
   - 盘前简报：`build_opening_brief_report_metadata(now=None, target_date=None, data_warnings=None, previous_summary_date=None)`
   - 函数只返回 Markdown 文本，不做外部 IO。
   - 支持传入 `now` 和 `target_date`，避免测试依赖真实时间。
   - `data_warnings` 非空时标记 `数据完整性：存在缺失/存在降级`，否则 `正常`。
   - 若已有 `capture_info`，可以把 `run_id`、`captured_at`、`trade_date` 等加入“快照数据说明”；不要为了这些字段大改读取逻辑。
   - 若是盘前简报且已有 `previous_summary_date`，可显示“前一日复盘日期”；没有就不要为了该字段大改复盘/新闻读取逻辑。

4. **插入位置保持章节结构不变**
   - 标题之后、原第一个正文章节之前。
   - 例如：
     ```markdown
     # A股收盘摘要 - YYYY-MM-DD

     > 数据日期：YYYY-MM-DD
     > 生成时间：YYYY-MM-DD HH:MM:SS
     > 报告类型：收盘摘要
     > 数据阶段：收盘后数据
     > 行情日期要求：必须为当日收盘/盘中快照数据
     > 是否允许回退前一交易日行情：否
     > 快照数据说明：使用目标交易日可获得的盘中/收盘快照
     > 数据完整性：正常
     > 缺失说明：无
     > 备注：本报告用于盘后复盘与候选股研究，不构成买卖建议

     ## 1. 市场总览
     ```

5. **测试先行**
   - 新增独立测试文件，例如 `tests/test_close_summary_report_metadata.py`。
   - 覆盖：正常完整性、warnings 降级、禁止回退前一交易日行情、报告类型/阶段、插入位置。
   - 对 `build_markdown` 测试时 monkeypatch 重型依赖（如 `ase.classify_market_hard`），避免触发真实行情或 DB。

6. **必须回归邻近报告元信息测试**
   - 至少运行当前新增测试、已有持仓/候选元信息测试、开盘元信息/窗口测试、asof 校验测试和 py_compile。

## 关键坑

- 元信息任务不是数据修复任务：不要顺手改 DB latest 查询，除非用户明确要求。
- 不要改变报告整体章节结构；只允许在标题与第一个章节之间插入 blockquote 元信息。
- `build_markdown` 增加参数时保持可选默认值，避免破坏 17:00/17:30 自动任务的既有调用。
- 若项目无 git，最终回复必须列出备份路径和回滚命令。
