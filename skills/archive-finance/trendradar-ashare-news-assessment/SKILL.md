---
name: trendradar-ashare-news-assessment
description: 评估 TrendRadar 是否适合作为 A 股盘前/盘中新闻获取层，并在无 Docker 的 Linux 服务器上验证本地部署可行性。
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [TrendRadar, A股, 新闻, 飞书, 部署评估, Linux]
    related_skills: [ashare-opening-brief, finance-news-cn, hermes-cron-architecture]
---

# TrendRadar 作为 A 股新闻层的评估与本地部署验证

当用户想要：
- 判断 `https://github.com/sansan0/TrendRadar` 是否适合接入自己的 A 股盘前/盘中新闻链路
- 确认当前服务器能否部署该项目
- 区分它适合做“新闻入口层”还是“个股事件层”

使用本技能。

## 核心结论模板

通常应先给出三段结论：

1. **是否适合业务场景**
   - 适合：盘前/盘中新闻、情绪、快讯、港股/美股/大宗商品联动
   - 不适合单独承担：A 股个股公告、研报评级、持仓/候选股逐票异常检测

2. **是否适合当前机器**
   - 能否本地 Python 部署
   - 能否 Docker 部署
   - 内存/磁盘是否够

3. **推荐接入方式**
   - TrendRadar 做新闻层
   - 现有 AkShare / Obsidian 脚本继续做公告/研报/异动层

## 必做检查顺序

### 1. 克隆仓库并读关键文件
优先检查：
- `README.md`
- `pyproject.toml`
- `config/config.yaml`
- `trendradar/crawler/fetcher.py`
- `trendradar/crawler/rss/fetcher.py`
- `trendradar/core/scheduler.py`
- `docker/docker-compose.yml`
- `docker/Dockerfile`

重点确认：
- Python 版本要求
- 默认数据源
- 是否支持飞书
- 是否支持 RSS
- 是否支持调度
- Docker 是否为官方推荐路径

### 2. 检查当前服务器环境
必须实际检查：
- OS
- Python 版本
- 是否有 Python 3.12
- 是否有 `uv`
- 是否有 Docker
- 内存 / 磁盘

推荐命令：
```bash
uname -a && python3 --version && node --version && npm --version && docker --version || true && free -h && df -h /
command -v uv || true && uv --version || true && command -v python3.12 || true && python3.12 --version || true
```

### 3. 识别项目真实定位，而不是只看 README 宣传语
TrendRadar 的经验判断：
- 它本质是 **热点聚合 + RSS + AI筛选 + 消息推送**
- 默认热榜数据来自 `newsnow` API
- 默认财经相关热榜源里最值得关注的是：
  - `wallstreetcn-hot`
  - `cls-hot`
- RSS 能补充海外财经源，如 `Yahoo Finance`

因此它更适合：
- 盘前全球财经快讯
- 市场情绪补充
- 新闻入口层

而不应被误判为：
- 专门的 A 股公告系统
- 专门的研报系统
- 专门的个股监控系统

## 本地部署验证流程（无 Docker 场景）

### 4. 若机器没有 Docker，优先验证本地 uv/Python 路线
若仓库要求 `Python >= 3.12`，先确认机器上是否已有 `python3.12`。

若有，执行：
```bash
cd /path/to/TrendRadar
uv sync --locked --no-dev
```

### 5. 先跑 doctor，不要直接上完整运行
推荐：
```bash
cd /path/to/TrendRadar
. .venv/bin/activate
python -m trendradar --doctor
```

#### 经验结论
TrendRadar 的 `--doctor` 可能因为默认启用了：
- AI 分析
- AI 翻译

而在未配置 `AI_API_KEY` 时失败。

这不代表项目不能部署，只代表：
- 默认配置对“无 AI 本地验证”不够友好

### 6. 做一个最小测试配置，再验证完整运行
更稳的做法：
1. 复制 `config/config.yaml` 为测试版，例如：
   - `config/test-config.yaml`
2. 修改为最小可运行方案：
   - `filter.method = keyword`
   - `ai_analysis.enabled = false`
   - `ai_translation.enabled = false`
   - 热榜源先只保留财经相关源，如：
     - `wallstreetcn-hot`
     - `cls-hot`
   - RSS 先只保留 `yahoo-finance`
3. 再运行：
```bash
CONFIG_PATH=config/test-config.yaml python -m trendradar --doctor
CONFIG_PATH=config/test-config.yaml python -m trendradar
```

#### 一个实战坑
不要把测试配置写到仓库外的临时目录后直接用 `CONFIG_PATH=/tmp/...` 跑 doctor。

原因：
- TrendRadar 会按 `CONFIG_PATH` 的相对位置推断 `timeline.yaml`
- 如果配置文件不在仓库 `config/` 目录内，可能出现：
  - `timeline.yaml` 未找到
  - 调度 preset 无法解析

更稳的做法：
- 把测试配置写回仓库内，如：`config/test-config.yaml`

## 成功判定标准

当以下条件满足，可判定“当前服务器可本地部署”：

1. `uv sync --locked --no-dev` 成功
2. `python -m trendradar --doctor` 在最小配置下无 fail 项
3. `python -m trendradar` 能实际：
   - 抓到热榜数据
   - 抓到 RSS 数据
   - 写入本地 output
   - 生成 HTML 报告

## 对 A 股业务的推荐接法

### 推荐定位：新闻层，而不是事件层
对 A 股自动化链路，TrendRadar 最适合承担：
- 盘前新闻摘要
- 盘中快讯补充
- 市场情绪线索
- 海外市场 / 大宗商品 / 港股联动观察

不建议让它独自承担：
- 持仓股公告检查
- 候选股公告检查
- 券商研报 / 评级监控
- 个股 >3% 异动精确筛选

### 推荐组合方式
最稳的架构是：

1. **TrendRadar**
   - 抓华尔街见闻 / 财联社 / RSS
   - 提供盘前盘中的新闻流与情绪补充

2. **AkShare / Obsidian 本地脚本**
   - 查公告
   - 查研报
   - 查个股异动
   - 生成今日关注清单

也就是：
- TrendRadar = **新闻入口层**
- 现有脚本 = **交易决策与持仓事件层**

## 评估输出建议

向用户汇报时，最好分四段：

### 1. 能不能用
- 可以做盘前/盘中新闻获取方式
- 但更适合做“新闻情绪层”而非“全量 A 股事件层”

### 2. 当前机器能不能部署
- 可本地 Python 部署
- 若未装 Docker，则不能直接按官方推荐 Docker 方案部署

### 3. 实测结果
应明确写出：
- 是否安装成功
- 是否抓到 `wallstreetcn-hot`
- 是否抓到 `cls-hot`
- 是否抓到 `Yahoo Finance RSS`
- 是否生成 HTML 报告

### 4. 最终建议
建议用户采用：
- TrendRadar 做新闻层
- 现有 A 股脚本继续做公告/研报/持仓候选分析

## 一条实战结论

评估 TrendRadar 这类项目时，**不要只停留在 README 功能列表**。最关键的是：
1. 看清它真实数据源是不是热榜/API 聚合
2. 实测医生检查和最小运行链路
3. 区分“新闻层”与“事件层”职责

对 A 股交易自动化来说，TrendRadar 适合做 **盘前/盘中新闻与情绪补充层**，但不应替代个股公告、研报和候选股异常监控脚本。