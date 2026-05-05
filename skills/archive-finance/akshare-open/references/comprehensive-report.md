# 一键综合研报脚本

脚本：`scripts/analysis/comprehensive_report.py`

## 作用

一次性聚合以下 5 个模块输出：
- 基本面分析
- 估值分析
- 策略分析
- 事件驱动分析
- 财经新闻总结

并自动生成：
- `executive_summary`（执行摘要）
- `markdown`（可直接保存/分发的综合研报正文）
- `module_errors`（各模块失败或数据缺失信息）

## 用法

```bash
python scripts/analysis/comprehensive_report.py --code 600519 --json
python scripts/analysis/comprehensive_report.py --code 600519 --save ~/Desktop/600519_综合研报.md
```

## 输出结构

- `executive_summary`：综合结论
- `fundamental`：基本面模块原始结果
- `valuation`：估值模块原始结果
- `strategy`：策略模块原始结果
- `event`：事件模块原始结果
- `news`：新闻模块原始结果
- `module_errors`：失败原因汇总
- `markdown`：完整研报正文

## 适用场景

- 快速给单只股票生成研究初稿
- 盘后复盘
- 给飞书、邮件、文档系统提供 Markdown 版研究输出
- 做多模块结果的统一汇总与质检

## 注意事项

- 该脚本依赖下游 5 个模块，因此会继承它们的数据缺口与上游接口波动
- 某些字段缺失时，脚本会继续输出，并在 `module_errors` 中保留错误信息
- 更适合作为“研究初稿生成器”，而不是替代人工最终判断
