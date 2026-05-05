---
name: trendradar-local-finance-deploy
description: 在本地 Linux 服务器上以非 Docker 方式部署 TrendRadar，聚焦 A 股盘前盘中财经新闻，输出仅保存到本地文件。
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [TrendRadar, A股, 本地部署, 财经新闻, Linux, Cron]
    related_skills: [finance-news-cn, hermes-cron-architecture]
---

# TrendRadar 本地财经版部署

适用场景：
- 服务器没有 Docker，但有 Python 3.12 和 uv
- 需要盘前/盘中财经新闻聚合
- 暂时不推送飞书，只落本地文件
- 重点看 A 股、港股、美股、大宗商品与宏观政策

## 推荐目录
- Repo: `/home/admin/.hermes/apps/trendradar-finance`
- Output: `/home/admin/Notes/market/trendradar-output`
- Log: `/home/admin/.hermes/logs/trendradar_finance.log`
- Wrapper: `/home/admin/.hermes/scripts/run_trendradar_finance.sh`

## 部署步骤
1. 克隆仓库到固定目录。
2. 将 repo 的 `output/` 替换为指向 `/home/admin/Notes/market/trendradar-output` 的软链接。
3. 在 repo 下运行：
   - `uv sync --locked --no-dev`
4. 新建定制配置：
   - `config/ashare-finance.yaml`
   - `config/custom/keyword/ashare-finance.txt`
5. 配置策略：
   - `filter.method=keyword`
   - `ai_analysis.enabled=false`
   - `ai_translation.enabled=false`
   - 仅保留平台：`wallstreetcn-hot`、`cls-hot`、`thepaper`
   - RSS 保留：Yahoo Finance、MarketWatch、Investing.com
   - `storage.backend=local`
   - `storage.formats.sqlite=true`
   - `storage.formats.html=true`
   - 通知渠道全部留空，但 `notification.enabled=true`，这样仍会生成本地 HTML 报告
6. 创建 wrapper：
   - 导出 `CONFIG_PATH=config/ashare-finance.yaml`
   - 导出 `FREQUENCY_WORDS_PATH=config/custom/keyword/ashare-finance.txt`
   - 导出 `GITHUB_ACTIONS=true`，避免本地环境自动尝试打开浏览器
   - 用 `flock` 防重入
   - stdout/stderr 追加到日志文件
7. 用 cron 运行 wrapper，建议交易日频率：
   - `30-59/10 8 * * 1-5`
   - `*/10 9-11 * * 1-5`
   - `0,10,20,30,40,50 13-14 * * 1-5`
   - `0 15 * * 1-5`

## 验证
手动执行：
```bash
/home/admin/.hermes/scripts/run_trendradar_finance.sh
```
验证结果：
- HTML 历史归档：`/home/admin/Notes/market/trendradar-output/html/YYYY-MM-DD/*.html`
- 最新报告：`/home/admin/Notes/market/trendradar-output/html/latest/current.html`
- 日志：`/home/admin/.hermes/logs/trendradar_finance.log`

## 注意事项
- TrendRadar 的 `platforms.sources[].enabled` 在当前实现中并不作为实际过滤条件，若要真正减少抓取源，应直接删掉不用的 source，而不是仅写 `enabled: false`。
- 本地运行默认会尝试 `webbrowser.open()` 打开 HTML；通过环境变量 `GITHUB_ACTIONS=true` 可抑制。
- 这是新闻/情绪层工具，不适合作为个股公告、研报、持仓异常监控的唯一来源。
