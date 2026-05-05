# 盘前简报顶部数据时间元信息：最小修改经验

适用脚本：`/home/admin/.hermes/scripts/ashare_opening_brief.py`

## 目标

只在盘前简报标题后、正文第一节前增加统一数据时间元信息，让用户能快速判断报告是否只使用盘前可获得信息。

## 已验证的最小实现形态

新增独立函数：

```python
def build_opening_brief_report_metadata(now=None, target_date=None, data_warnings=None, previous_summary_date=None):
    now_dt = now or NOW
    target = target_date or TODAY
    warnings = [str(w).strip() for w in (data_warnings or []) if str(w).strip()]
    completeness = '存在缺失/存在降级' if warnings else '正常'
    missing_note = '；'.join(warnings) if warnings else '无'
    ...
```

修改 `render_markdown(...)` 时只增加可选参数，不破坏原调用：

```python
def render_markdown(..., now=None, target_date=None, data_warnings=None, previous_summary_date=None):
    report_now = now or NOW
    report_date = target_date or TODAY
    lines = []
    lines.append(f"# A股开盘前简报 - {report_date}")
    lines.append('')
    lines.extend(build_opening_brief_report_metadata(...).splitlines())
    lines.append('')
    lines.append('## 1. 昨夜今晨发生了什么（含 TrendRadar 多源）')
```

## TDD 覆盖点

新增测试文件建议：`tests/test_opening_brief_report_metadata.py`

覆盖：
- 无 `data_warnings` 时 `数据完整性：正常`、`缺失说明：无`
- 有 `data_warnings` 时 `数据完整性：存在缺失/存在降级`
- 显示 `报告类型：盘前简报`
- 显示 `数据阶段：盘前数据`
- 显示 `是否允许使用当日开盘后行情：否`
- 显示 `新闻/公告时间要求：不晚于报告生成时间`
- 元信息位于标题之后、`## 1. 昨夜今晨发生了什么（含 TrendRadar 多源）` 之前

## 必须避免

- 不修改 cron 配置
- 不修改数据库结构或 DB latest 查询
- 不修改新闻抓取、Google News RSS、TrendRadar、公告读取逻辑
- 不修改候选股生成逻辑
- 不修改持仓账本逻辑
- 不修改飞书推送逻辑
- 不改变后续章节顺序

## 验证命令

```bash
python -m pytest /home/admin/.hermes/scripts/tests/test_opening_brief_report_metadata.py -q
python -m py_compile /home/admin/.hermes/scripts/ashare_opening_brief.py /home/admin/.hermes/scripts/tests/test_opening_brief_report_metadata.py
```
