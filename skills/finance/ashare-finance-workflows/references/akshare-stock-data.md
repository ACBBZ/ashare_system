---
name: akshare-stock-data
description: 使用 AkShare 获取 A股/指数/板块/财务/历史行情数据的本地技能。
---

# AkShare 股票数据获取

当用户需要获取 A股、指数、ETF、板块、财务指标、历史 K 线、实时行情时，使用本技能。

## 环境

本机已安装：
- python3
- akshare
- pandas

验证：
```bash
python3 -c "import akshare as ak; print(ak.__version__)"
```

## 常用方式

### 1. 查询全市场实时行情
```bash
python3 - <<'PY'
import akshare as ak
print(ak.stock_zh_a_spot_em().head(10).to_string())
PY
```

### 2. 查询单只股票历史行情
```bash
python3 - <<'PY'
import akshare as ak
symbol = '000001'
df = ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date='20250101', end_date='20251231', adjust='qfq')
print(df.tail(20).to_string())
PY
```

### 3. 查询指数历史行情
```bash
python3 - <<'PY'
import akshare as ak
# 上证指数示例
df = ak.index_zh_a_hist(symbol='000001', period='daily', start_date='20250101', end_date='20251231')
print(df.tail(20).to_string())
PY
```

### 4. 查询财务摘要
```bash
python3 - <<'PY'
import akshare as ak
symbol = '000001'
df = ak.stock_financial_abstract_ths(symbol=symbol, indicator='按报告期')
print(df.head(20).to_string())
PY
```

## 工作流程

1. 先确认用户要的是：股票/指数/ETF/板块/财务/新闻
2. 如果是个股，确认代码（如 000001）
3. 优先抓取原始表格数据
4. 再做摘要、排序、同比/环比或技术指标分析
5. 返回时标明数据来源接口和时间范围

## 注意事项

- AkShare 接口偶尔会因上游网站变化失效；报错时先换相近接口
- 当前这台机器上已验证 `stock_zh_a_spot_em()` 可用，但部分历史行情接口可能出现 `RemoteDisconnected`，通常属于上游源不稳定，建议重试或切换相近接口
- A股代码通常用 6 位数字；指数接口与个股接口不同
- 历史行情要明确 period、start_date、end_date、adjust
- 不要把投资分析表述成确定性投资建议

## 推荐配合

- 配合 `stock-analysis-cn` 做技术面/财务面分析
- 配合 `stock-monitor-anomaly` 做异动筛选
- 配合 `finance-news-cn` 做新闻核验
