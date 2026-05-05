---
name: ashare-ledger-db-workflow
description: 使用 SQLite 建立 A 股持仓账本，记录每日操作，生成 15:05 持仓盈亏表，并接入收盘复盘与持仓分析。
version: 1.0.0
author: Hermes Agent
license: MIT
---

# A 股 DB 账本工作流

适用场景：
- 用户通过飞书逐日发送交易操作
- 需要本地 DB 持久化记录持仓、成本、现价、累计盈亏
- 每个交易日 15:05 自动发送持仓盈亏表
- 当日操作与持仓要进入收盘复盘、盘后持仓分析链路

## 路径约定
- DB：`/home/admin/Notes/market/ashare-monitor/ledger/ashare_ledger.db`
- 库文件：`/home/admin/.hermes/scripts/ashare_ledger_lib.py`
- CLI：`/home/admin/.hermes/scripts/ashare_ledger_cli.py`
- 15:05 报表脚本：`/home/admin/.hermes/scripts/ashare_ledger_daily_report.py`
- 模板：`/home/admin/Notes/market/ashare-monitor/ledger/trade-message-template.md`

## 表结构
至少包含：
- `trades`：逐笔交易
- `daily_position_snapshots`：每日持仓快照
- `daily_reports`：日报文件索引

## 核心实现
1. 用 `ashare_ledger_cli.py init` 初始化 DB。
2. 用 `add-trade` 写入结构化买卖：
   - `--trade-date`
   - `--symbol`
   - `--name`
   - `--side buy|sell`
   - `--quantity`
   - `--price`
   - 可选 `--fees --note --trade-time`
3. 同时提供自然语言入账：
   - 在 `ashare_ledger_lib.py` 中实现 `parse_trade_message()` + `record_trade_message()`
   - 在 `ashare_ledger_cli.py` 中增加 `parse-text`
   - 可直接处理如：`21.28 卖出300股康强电子，清仓止损，2.736卖出2800股白银LOF，清仓止损`
   - 经验：若自然语言里没写代码，要优先按“当前持仓名称 -> 代码”反查，再回退到 AkShare 名称表
4. 账本持仓按**移动平均成本法**计算：
   - 买入：把手续费计入持仓成本
   - 卖出：已实现盈亏 = `(卖价 - 持仓均价) * 数量 - 手续费`
5. 15:05 报表脚本生成简表，表头固定为：
   - `股票 | 成本 | 现价 | 持仓 | 累计盈亏`
   - 若当天已清仓，也不要把累计盈亏清零；空仓行应继续显示当天累计盈亏
6. `daily_position_snapshots` 每次生成日报前要先 `DELETE` 当日旧快照，再写入新快照；否则清仓后旧持仓会残留，导致复盘/分析误判仍有仓位。
7. 将 15:05 账本摘要追加到 `ashare_close_summary.py` 的输出中，形成：
   - `## 4.1 今日持仓与操作账本`
   - 这里的累计已实现盈亏/累计总盈亏应优先从 `daily_reports.summary_json` 读取，而不是仅从当日持仓快照汇总；否则清仓日会错误显示为 0。
8. 将账本接入 `ashare_position_watch_analysis.py`，并且**优先使用账本作为持仓来源**：
   - 优先读取 `latest_report_summary(NOW_DATE)` + `load_snapshot_rows(NOW_DATE)`
   - 若账本明确给出 `当前持仓记录：无。`，绝不能回退到旧 `close-summary` 中的历史持仓
   - 仅在账本完全缺失时，才回退到 `close-summary` 文本解析
9. 候选股提取必须只读 `## 3. 个股筛选（最重要）` 区块，不要全篇正则扫描；否则账本摘要里的股票代码会被误识别成候选股。

## 行情取数优化
避免全市场 `stock_zh_a_spot_em()`，否则容易超时。
推荐：
- 股票：逐票 `ak.stock_bid_ask_em(symbol=代码)`
- LOF / ETF：`ak.fund_lof_spot_em()` / `ak.fund_etf_spot_em()`
- 用 `contextlib.redirect_stdout/redirect_stderr` 压掉 AkShare 进度条输出

## Cron 建议
创建 15:05 任务：
- 名称：`ashare-ledger-daily-pnl-feishu`
- schedule：`5 15 * * 1-5`
- **deliver：优先使用显式飞书 chat_id**，例如：`feishu:oc_xxx`；不要依赖 `origin`，因为账本 cron 在部分场景下会出现“脚本成功、输出成功，但 origin 自动投递静默不显现”的情况
- script：`ashare_ledger_daily_report.py`
- prompt：只读取 `holding-pnl-1505.md` 并原样发送

### 15:05 飞书发送格式实战修正
- 不要把 15:05 持仓盈亏表发成 Markdown 表格
- 优先改成 **纯文本代码块**，例如：

```text
股票       成本     现价     持仓   累计盈亏
豪威集团    99.50   104.63   100    513.00
```

原因：
- Feishu 对“很短、几乎只有表格、尤其是空仓场景只有一行”的 cron 内容，走 markdown/post 渲染链路时更容易出现静默显示异常
- 改成纯文本代码块后更稳定，也更适合 15:05 简表场景

## 初始建账
若用户已有“当前持仓记录”但没有历史逐笔交易：
- 可以把最近可信的持仓行作为账本基线导入
- 这些基线仓位的累计已实现盈亏默认从 0 开始
- 需要在用户侧明确：账本累计盈亏从建账基线开始，不追溯更早已平仓历史

## 用户输入模板
推荐用户每天通过飞书按一行一笔发送：
```text
2026-04-23 买入 康强电子 002119 100股 21.30 手续费 5 备注 回封试单
2026-04-23 卖出 白银LOF 161226 500股 3.22 手续费 3 备注 做T减仓
```

也可以接受自然语言口语输入，例如：
```text
21.28 卖出300股康强电子，清仓止损，2.736卖出2800股白银LOF，清仓止损
```

实现上可在 `ashare_ledger_lib.py` 增加：
- `parse_trade_message(text, default_trade_date=...)`
- `record_trade_message(...)`

并在 CLI 中增加：
- `ashare_ledger_cli.py parse-text --trade-date YYYY-MM-DD --text "..."`

## 验证
- `python3 /home/admin/.hermes/scripts/ashare_ledger_cli.py show-positions --trade-date YYYY-MM-DD`
- `python3 /home/admin/.hermes/scripts/ashare_ledger_daily_report.py`
- 手动 `cronjob.run(job_id=...)` 看会话中是否发送 Markdown 表格正文
