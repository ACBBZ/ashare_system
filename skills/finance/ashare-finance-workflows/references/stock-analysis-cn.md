---
name: stock-analysis-cn
description: 基于 AkShare 的 A股股票分析技能，覆盖行情、技术面、财务面、估值面与风险提示。
---

# A股股票分析

当用户问：
- 某只股票值不值得关注
- 帮我分析 600519 / 000001 / 某板块龙头
- 从技术面和基本面看怎么样

使用本技能。

## 分析框架

默认按以下顺序：
1. 基本信息：代码、名称、行业/概念（能取则取）
2. 行情概览：近 1/3/6/12 个月涨跌、波动、成交额/成交量变化
3. 技术面（日线为主）：
   - 均线结构：5 日、10 日、20 日、30 日均线的位置、斜率、金叉/死叉、股价与均线乖离
   - K线形态：最近 5~20 个交易日是否出现长阳/长阴、十字星、上影/下影、吞没、突破/假突破、缩量回踩、放量滞涨等
   - 趋势判断：短线、中短线是否处于上升趋势、震荡整理、破位下行，结合阶段高低点判断强弱
   - 量价关系：放量上涨、放量下跌、缩量回调、缩量横盘、量价背离
4. 关键价位：
   - 基于近 3 个月与 6 个月日线/K线/量能结构，给出主要支撑位与压力位
   - 支撑压力应尽量结合阶段低点/高点、均线密集区、跳空缺口、放量突破平台、前高前低等依据说明
5. 财务面：营收、利润、ROE、毛利率、资产负债率等可得指标
6. 风险点：高波动、回撤、业绩承压、估值压力、政策风险、技术破位风险
7. 总结：观察结论，不给确定性荐股承诺

## 额外要求

- 默认按**短线主板交易框架**分析：
  - 个股默认只保留主板股票：`600/601/603/605/000/001/002/003`
  - 默认排除：`300/301`（创业板）、`688/689`（科创板）、`8/4` 开头（北交所等）
  - 若用户明确要求分析 ETF/LOF，则允许进入“基金/场内产品分支”处理
- 如果用户提供持仓成本，必须补充“持仓视角分析”：
  - 当前价相对成本价的浮盈/浮亏幅度
  - 成本价在当前结构中属于压力位、支撑位还是中性位置
  - 基于趋势、量能、K线与关键价位，给出偏交易层面的操作建议（如继续观察、逢反弹减仓、跌破支撑止损观察、回踩支撑可跟踪、突破压力后再确认等）
- 若分析对象是**候选股**，必须额外补充：
  - 所属板块
  - 板块阶段：主升 / 修复 / 分歧 / 退潮 / 轮动
  - 在板块中的角色：龙头 / 中军 / 补涨 / 跟风
  - 是否符合“低位趋势成形 + 催化 + 承接”的短线框架
- 操作建议必须写清触发条件，不使用绝对化表述，不承诺收益。
- 用户属于**小资金短线风格**时，输出优先强调：
  - 盈亏比是否值得出手
  - 计划买点 / 确认条件 / 放弃条件
  - 是否值得作为 1~2 个核心观察标的，而不是给出大资金分散配置建议

## ETF / LOF 分支

当用户明确分析 ETF/LOF 等场内基金产品时，不要直接沿用个股接口与结论模板：
- 现价 / 当日涨跌优先使用基金现货接口
- 趋势、通道、支撑压力可基于基金净值走势做近似分析
- 文中需明确注明：`LOF/ETF 优先使用场内最新价做盈亏测算；趋势、通道、支撑压力可基于近一年净值走势近似分析。`
- 输出重点应偏交易执行，不做企业基本面式解读

## 行情分析示例
```bash
python3 - <<'PY'
import time
import akshare as ak
import pandas as pd

symbol = '600519'
start = '20240101'
end = '20261231'

def with_retry(fn, attempts=3, delay=2):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last

def load_hist(symbol: str, start: str, end: str):
    try:
        return with_retry(lambda: ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date=start, end_date=end, adjust='qfq'))
    except Exception:
        market_symbol = symbol
        if not symbol.startswith(('sh', 'sz', 'bj')) and len(symbol) == 6:
            if symbol.startswith(('6', '9')):
                market_symbol = f'sh{symbol}'
            elif symbol.startswith(('0', '2', '3')):
                market_symbol = f'sz{symbol}'
        tx = with_retry(lambda: ak.stock_zh_a_hist_tx(symbol=market_symbol, start_date=start, end_date=end, adjust='qfq'))
        return tx.rename(columns={'date': '日期', 'open': '开盘', 'close': '收盘', 'high': '最高', 'low': '最低', 'amount': '成交量'})

df = load_hist(symbol, start, end)
close_col = '收盘'
vol_col = '成交量'
for n in [5, 10, 20, 30, 60, 120]:
    df[f'MA{n}'] = df[close_col].rolling(n).mean()
    df[f'VOL_MA{n}'] = df[vol_col].rolling(n).mean()

recent_3m = df.tail(60)
recent_6m = df.tail(120)
print('=== RECENT DAILY BARS ===')
print(df.tail(30).to_string(index=False))
print('\n=== 3M SUPPORT / RESISTANCE CANDIDATES ===')
print({'3m_low': recent_3m['最低'].min(), '3m_high': recent_3m['最高'].max()})
print('\n=== 6M SUPPORT / RESISTANCE CANDIDATES ===')
print({'6m_low': recent_6m['最低'].min(), '6m_high': recent_6m['最高'].max()})
PY
```

## 财务摘要示例
```bash
python3 - <<'PY'
import akshare as ak
symbol = '600519'
df = ak.stock_financial_abstract_ths(symbol=symbol, indicator='按报告期')
print(df.head(20).to_string(index=False))
PY
```

## 输出模板

建议输出：
- 一句话结论
- 若为主板候选股：所属板块、板块阶段、个股角色
- 日线趋势与 K 线要点
- 5/10/20/30 日均线结构
- 量价关系判断
- 3 个月支撑/压力位
- 6 个月支撑/压力位
- 财务面要点（若为 ETF/LOF 可省略为产品属性与驱动因素）
- 催化/风险
- 盈亏比与执行条件
- 适合继续跟踪的观察位
- 若提供持仓成本：补充“成本视角下的操作建议”
- 若用于次日计划：补充“计划买点 / 确认条件 / 放弃条件”

## 注意事项

- 如果 AkShare 某财务接口失效，优先换同花顺/东财相近接口
- 当前环境下 `stock_zh_a_hist` 历史行情接口偶尔会出现上游断连；先自动重试，仍失败时优先回退到 `stock_zh_a_hist_tx`
- 使用 `stock_zh_a_hist_tx` 时，股票代码需要带市场前缀：如 `600519 -> sh600519`，`000001/002119/300750 -> sz000001/sz002119/sz300750`
- 如果用户只给公司名，先确认代码再分析
- 支撑位/压力位不要只报一个数字，尽量给出区间，并解释依据（前高前低、均线密集区、平台区、缺口、放量长阴/长阳位置）
- 若用户给出成本价，可以给出基于当前结构的操作分析和建议，但需明确是信息参考，不构成确定性投资承诺
- 不把分析写成收益保证或绝对化个性化投资建议
