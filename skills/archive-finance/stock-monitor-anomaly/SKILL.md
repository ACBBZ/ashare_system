---
name: stock-monitor-anomaly
description: 使用 AkShare 对 A股股票进行异动筛选与监控，包括涨跌幅、成交量、换手率和短线异常。
---

# 股票异动监控

当用户要求：
- 监控某几只股票是否异动
- 找出今日涨跌幅异常、放量异常、换手率异常
- 做盘中/日内扫描

使用本技能。

## 目标

对指定股票池或全市场进行快速筛选，输出疑似异动标的及原因。

## 默认异动规则

可按需要调整：
- 涨跌幅绝对值 >= 5%
- 换手率 >= 5%
- 量比或成交额显著放大
- 振幅较高

## 默认市场过滤（非常重要）

若用户没有特别说明，默认只扫描**主板股票**：
- 保留：`600/601/603/605/000/001/002/003`
- 排除：`300/301`（创业板）、`688/689`（科创板）、`8/4` 开头（北交所等）

如果用户明确要求把 ETF/LOF 纳入观察池：
- ETF/LOF 单独成组输出
- 不与主板个股混排打分
- 需要在结果里显式标注“场内基金产品”

## 个股池监控示例
```bash
python3 - <<'PY'
import akshare as ak
import pandas as pd
watch = {'平安银行':'000001','万科A':'000002','贵州茅台':'600519'}
df = ak.stock_zh_a_spot_em()
code_col = '代码'
name_col = '名称'
sub = df[df[code_col].isin(watch.values())].copy()
cols = [c for c in ['代码','名称','最新价','涨跌幅','涨跌额','成交量','成交额','振幅','换手率','量比'] if c in sub.columns]
sub = sub[cols]
print(sub.to_string(index=False))
PY
```

## 全市场异动筛选示例
```bash
python3 - <<'PY'
import akshare as ak

df = ak.stock_zh_a_spot_em()
cond = None
if '涨跌幅' in df.columns:
    cond = df['涨跌幅'].abs() >= 5
if '换手率' in df.columns:
    cond = cond & (df['换手率'] >= 5) if cond is not None else (df['换手率'] >= 5)
res = df[cond].copy() if cond is not None else df.copy()
cols = [c for c in ['代码','名称','最新价','涨跌幅','成交额','振幅','换手率','量比'] if c in res.columns]
print(res[cols].sort_values(by='涨跌幅', ascending=False).head(50).to_string(index=False))
PY
```

## 输出规范

返回时给出：
1. 股票代码/名称
2. 触发规则
3. 关键数值（涨跌幅、换手率、量比、成交额、振幅）
4. 所属板块（若可获取）
5. 是否需要进一步结合新闻核验
6. 是否值得进入“候选池继续跟踪”

## 候选池优先级（适配短线主板体系）

不要把“异动”直接等同于“机会”。默认按以下顺序做二次筛选：
1. 是否属于强板块 / 主线板块
2. 板块当前阶段：主升 > 修复 > 轮动 > 分歧 > 退潮
3. 个股在板块中的角色：龙头 / 中军 / 补涨 / 跟风
4. 是否处于低位趋势成形，而非纯高位情绪冲顶
5. 是否存在可核验催化（公告、新闻、政策、业绩、机构调研等）
6. 是否有承接而不是单纯脉冲

建议把输出结果分成三层：
- **A层：值得继续跟踪** —— 强板块、趋势结构较完整、催化相对明确
- **B层：可观察** —— 有异动但条件不完整，需要再看承接/催化
- **C层：仅情绪异动** —— 更像脉冲或跟风，不建议直接纳入次日计划

## 与 Hermes 配合的建议

- 如需定时监控，可让 Hermes 创建 cron job，定时运行筛选并投递到飞书
- 如需盘后复盘，结合 `finance-news-cn` 对异动股做新闻归因
- 如需进一步判断质量，结合 `stock-analysis-cn` 做趋势与财务分析
- 若当前环境已安装 `shortline-mainboard-workflow`，候选池输出优先参考：
  - `templates/candidate-pool-template.md`
  - `references/candidate-scoring.md`
  - `references/risk-and-failure-signals.md`

> 实战建议：异动扫描本身只负责“找苗子”，最终是否进 A/B/C 层，应尽量参考统一模板和打分规则，而不是临场主观排序。

## 风险提示

异动不等于机会；需结合流动性、消息面、行业、财务与市场环境综合判断。
