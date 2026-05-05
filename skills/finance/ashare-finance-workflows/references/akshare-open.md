---
name: akshare-open
description: >
  基于 AKShare 的综合金融投研 Skill。覆盖 A股/港股/美股/指数/宏观/财务/资金流/事件数据，
  并在数据层之上补充基本面分析、事件驱动分析、财经新闻总结、估值分析、策略分析模块。
  适用于股票研究、板块跟踪、财报解读、估值检查、新闻催化梳理、事件驱动复盘与多维度投研。
---

# AKShare Open 投研增强版

这是一个以 **AKShare 数据访问能力** 为底座的综合金融研究 skill。

与现有本地 finance skills 的关系：
- `akshare-stock-data`：偏基础数据查询
- `stock-analysis-cn`：偏单票技术面/财务面分析模板
- `finance-news-cn`：偏新闻检索与摘要流程
- `stock-monitor-anomaly`：偏异动扫描
- **`akshare-open`**：把上面几类需求统一到一个更完整的数据/分析工作台里，并提供可复用脚本

## 何时使用

当用户要求以下任一类任务时优先使用本 skill：
- 查询 A股/港股/美股/指数/宏观/板块/资金流/龙虎榜/两融/解禁/研报/公告
- 对单只股票做 **基本面分析**、**估值分析**、**策略分析**
- 做 **事件驱动分析**：公告、研报、机构调研、龙虎榜、回购、股东变化
- 做 **财经新闻总结**：个股新闻、市场热度、热点关键词、研报线索
- 需要结构化 JSON 输出供 agent 后续加工

## 前提条件

```bash
python -c "import akshare, pandas; print(akshare.__version__, pandas.__version__)"
```

如果未安装：
```bash
python -m pip install akshare pandas
```

## 使用优先级

1. **先取原始数据**：优先使用 `scripts/stock/`、`scripts/index/`、`scripts/macro/` 下的脚本获取结构化数据
2. **再做专题分析**：如果用户要结论、框架化判断或可读摘要，再调用 `scripts/analysis/` 下的增强模块
3. **最后给研究结论**：明确区分事实、指标解释和主观判断，不输出确定性投资承诺

## 目录

- `scripts/stock/`：行情、财务、板块、资金流、特殊数据
- `scripts/index/`：指数数据
- `scripts/macro/`：宏观数据
- `scripts/market/`：市场总貌
- `scripts/analysis/`：增强模块
  - `fundamental_analysis.py`：基本面分析
  - `event_driven_analysis.py`：事件驱动分析
  - `news_summary.py`：财经新闻总结
  - `valuation_analysis.py`：估值分析
  - `strategy_analysis.py`：策略分析
  - `comprehensive_report.py`：一键综合研报
- `references/`：API 速查与分析框架说明

## 常用命令

### 数据层
```bash
python scripts/stock/spot.py info --code 600519 --json
python scripts/stock/hist.py a --code 600519 --period daily --start 20240101 --end 20251231 --adjust qfq --json
python scripts/stock/financial.py indicator --code 600519 --json
python scripts/stock/board.py industry-list --json
python scripts/stock/fund_flow.py individual --code 600519 --market sh --json
python scripts/stock/special.py stock-news --code 600519 --json
python scripts/index/data.py daily --code sh000001 --json
python scripts/macro/data.py cn pmi --json
```

### 分析层
```bash
python scripts/analysis/fundamental_analysis.py --code 600519 --json
python scripts/analysis/event_driven_analysis.py --code 600519 --json
python scripts/analysis/news_summary.py --code 600519 --json
python scripts/analysis/valuation_analysis.py --code 600519 --json
python scripts/analysis/strategy_analysis.py --code 600519 --json
python scripts/analysis/comprehensive_report.py --code 600519 --json
python scripts/analysis/comprehensive_report.py --code 600519 --save ~/Desktop/600519_综合研报.md
```

## 推荐工作流

### 单票深度研究
1. `spot.py info`
2. `hist.py a`
3. `financial.py indicator` + `financial.py abstract`
4. `special.py stock-news` + `notice-report` + `inst-research-detail`
5. `analysis/fundamental_analysis.py`
6. `analysis/valuation_analysis.py`
7. `analysis/strategy_analysis.py`
8. 若要一次性汇总全部模块，直接运行 `analysis/comprehensive_report.py`

### 事件驱动复盘
1. `special.py notice-report`
2. `special.py lhb-stock`
3. `special.py shareholder-change`
4. `special.py stock-repurchase`
5. `special.py stock-news`
6. `analysis/event_driven_analysis.py`

### 财经新闻/热点总结
1. `special.py research-report`
2. `special.py hot-rank`
3. `special.py hot-keyword`
4. 如是个股，再补 `special.py stock-news --code`
5. `analysis/news_summary.py`

## 输出要求

- 默认先返回关键数据和结论摘要
- 标明时间范围、代码、是否前复权/后复权
- 对新闻、公告、热度、研报等时效性信息注明“以实时数据为准”
- 对估值/策略判断注明不构成投资建议

## 注意事项

- AKShare 底层数据源来自公开网站抓取，字段和稳定性会随上游变化
- 全市场接口较慢，不适合高频轮询
- 实时行情通常不是 Level 2，且可能有延迟
- 某些财务/新闻/公告接口在不同版本 AKShare 中字段会变化，分析脚本应优先容错读取
- 当数据缺失时，应返回“未获取到足够数据”，不要强行下结论
- 一键综合研报 `comprehensive_report.py` 会继续产出结果，即使某些子模块失败；请检查 `module_errors` 字段，而不是假设所有模块都成功
- 某些 AKShare 命令会把进度条或提示写到 **stderr**；聚合脚本解析 JSON 时应优先解析 **stdout**，避免被 stderr 污染
- 历史行情接口可能偶发 `RemoteDisconnected`；这通常是上游源抖动，不一定是脚本逻辑错误，适合重试或保留为模块级错误继续输出
