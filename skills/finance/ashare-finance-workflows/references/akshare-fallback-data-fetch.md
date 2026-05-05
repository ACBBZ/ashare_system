---
name: akshare-fallback-data-fetch
description: >
  当 AkShare 接口（特别是 EastMoney 后端的历史行情 API）被限速或超时时，
  如何快速降级到备用数据源完成 A股 / 基金 / LOF 的实时行情获取。
  适用场景：盘中分析、持仓检查、紧急查价。
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [AkShare, 数据获取, 降级策略, 腾讯行情, A股, LOF]
    related_skills:
      - stock-analysis-cn
      - akshare-open
      - akshare-stock-data
---

# AkShare 降级数据获取

当 AkShare 的 EastMoney 后端接口被限速时（症状：`Connection aborted`、`RemoteDisconnected`），使用本技能中记录的备用方案完成数据获取。

## 问题症状

```
requests.exceptions.ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))
```

或：

```
b'Unknown JavaScript error during parse'
```

常见受影响接口：
- `ak.stock_zh_a_hist()` — **最常被限速**
- `ak.stock_zh_index_spot_sina()` — 偶发

不受影响接口：
- `ak.fund_etf_hist_sino()` — 基本稳定
- `ak.stock_info_a_code_name()` — 基本稳定

## 降级方案（按优先级）

新增实践：共享容错层建议统一做成 **“provider fallback + 短时缓存 + 硬超时”** 结构。对于盘中 cron，实时行情缓存建议 10~30 秒，历史 K 线缓存建议 3~10 分钟；否则同一轮任务中反复请求相同标的会放大限流概率。

### 方案 1：腾讯实时行情 API（推荐，稳定）

**适用**：获取当前交易日快照（价格/涨跌幅/成交量/最高/最低）

```python
import requests

def get_realtime_quote_tengxun(code: str) -> dict:
    """
    code: 6位股票代码，如 '002119'
    返回示例字段：
      [3]  当前价
      [4]  昨收
      [5]  今开
      [6]  成交量（股）
      [30] 最高
      [31] 最低
      [32] 时间（YYYYMMDDHHMMSS）
      [33] 涨跌额
      [34] 涨跌幅(%)
    """
    if code.startswith(('6', '5', '9')):
        prefix = 'sh'  # 上交所
    else:
        prefix = 'sz'  # 深交所
    
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://gu.qq.com"
    }
    resp = requests.get(url, headers=headers, timeout=10)
    raw = resp.text.strip()
    # 格式: v_sz002119="51~名称~代码~当前价~昨收~今开~成交量~..."
    field = raw.split('"')[1].split('~')
    return {
        'code': code,
        'name': field[1],
        'current': float(field[3]),
        'prev_close': float(field[4]),
        'open': float(field[5]),
        'volume': int(field[6]),
        'high': float(field[30]),
        'low': float(field[31]),
        'time': field[32],
        'change_amt': float(field[33]),
        'change_pct': float(field[34]),
    }
```

**限制**：仅当日快照，无法计算 MA/MACD/RSI 等历史指标。

---

### 方案 2：新浪实时行情（需正确 Referer）

```python
import requests

def get_realtime_quote_sina(code: str) -> dict:
    prefix = 'sh' if code.startswith(('6', '5', '9')) else 'sz'
    url = f"https://hq.sinajs.cn/list={prefix}{code}"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.encoding = "gbk"
    field = resp.text.strip().split('"')[1].split(',')
    return {
        'name': field[0],
        'open': float(field[1]),
        'prev_close': float(field[2]),
        'current': float(field[3]),
        'high': float(field[4]),
        'low': float(field[5]),
        'volume': int(field[8]),
        'amount': float(field[9]),
    }
```

**注意**：新浪行情 API 在无正确 Referer 时返回 403，需携带 `Referer: https://finance.sina.com.cn`。

---

### 方案 3：雪球实时（新增，可用）

适合单票实时快照。通常先访问 `https://xueqiu.com` 建 session/cookie，再请求：

- `https://stock.xueqiu.com/v5/stock/realtime/quotec.json?symbol=SH600000`

返回字段常见可用：
- `current`
- `percent`
- `chg`
- `open`
- `last_close`
- `high`
- `low`
- `volume`
- `amount`

注意：雪球接口通常需要先拿到 cookie/session，直接裸调容易失败。

---

### 方案 4：pytdx 实时（新增，可用）

适合作为本地直连行情备用源。通过 `pytdx.hq.TdxHq_API` 连接可用服务器后调用：

- `get_security_quotes([(market, code)])`

常见返回字段：
- `price`
- `last_close`
- `open`
- `high`
- `low`
- `vol`
- `amount`
- `bid1` / `ask1`

适合在 AkShare / HTTP 数据源异常时继续完成实时行情拉取。

---

### 方案 5：TuShare（新增，但通常需要 token）

TuShare 的 `realtime_quote` / `pro_bar` 需要先设置有效 token。若环境变量缺失，应在共享层里显式跳过该 provider，不要让它拖慢整条 fallback 链路。

---

### 方案 6：东方财富实时（易超时，备选）

```python
import requests

def get_realtime_quote_eastmoney(code: str) -> dict:
    # code: 6位数字股票代码
    # secid: 1.上海（6开头）, 0.深圳（0/3开头）
    if code.startswith('6'):
        secid = f"1.{code}"
    else:
        secid = f"0.{code}"
    
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f170"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    data = resp.json()['data']
    return {
        'current': data.get('f43'),
        'prev_close': data.get('f44'),
        'open': data.get('f45'),
        'high': data.get('f46'),
        'low': data.get('f47'),
        'volume': data.get('f48'),
    }
```

**注意**：东方财富实时 API 在网络受限环境下极易超时，建议优先用腾讯方案。

---

## 历史数据重试逻辑（推荐写法）

当需要同时获取历史数据时，使用重试 + 降级双重逻辑：

```python
import time

def get_stock_hist_with_fallback(code: str, days: int = 120):
    """尝试 AkShare 历史数据，失败后降级到实时快照 + 盘中数据补充。"""
    import akshare as ak
    
    # Step 1: 尝试 AkShare
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period='daily',
                                     start_date='20250101', adjust='qfq')
            return df  # 成功直接返回
        except Exception as e:
            print(f"AkShare attempt {attempt+1} failed: {e}")
            time.sleep(2)
    
    # Step 2: AkShare 失败，降级到腾讯实时
    quote = get_realtime_quote_tengxun(code)
    
    # 返回当日快照（历史指标不可用，需在结论中注明）
    return {
        'note': '历史数据不可用，仅当日快照',
        'current': quote['current'],
        'prev_close': quote['prev_close'],
        'change_pct': quote['change_pct'],
        'high': quote['high'],
        'low': quote['low'],
        'volume': quote['volume'],
    }
```

---

## 基金 / LOF 数据

基金/LOF 数据通常不受 EastMoney 限速影响：

```python
# 白银 LOF（sz161226）
fdf = ak.fund_etf_hist_sina(symbol='sz161226')
fdf['date'] = pd.to_datetime(fdf['date'])
fdf = fdf.sort_values('date').tail(90)
# 后续计算 MA/RSI/通道 与股票相同
```

---

## 关键原则

1. **先 AkShare，失败再降级**，不要跳过 AkShare 直接用备用源
2. 降级后需在分析结论中**明确标注"历史指标基于盘中快照"**，避免误用
3. 历史数据（均线、MACD、RSI 等）必须等 AkShare 恢复后补充，不得用单日快照模拟
4. 腾讯实时行情 API (`qt.gtimg.cn`) 在本次会话中经验证**稳定可用**，可作为首选备用源
5. 对 cron / 后台脚本，**仅做 Python 线程超时不够**：底层请求可能继续卡住。Linux 上优先用 `signal.setitimer()` 做硬超时，再叠加重试与备用源。
6. 对全市场行情可增加 **EastMoney 直连 clist / kline 接口** 作为 AkShare 之外的备用源，避免 AkShare 包装层故障时整条任务失败。
