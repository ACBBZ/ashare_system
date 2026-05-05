#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from helpers import output_payload, now_text


def _extract_json_blob(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    candidates = []
    for start_char in ('{', '['):
        idx = text.find(start_char)
        if idx != -1:
            candidates.append(text[idx:])
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            pass
    match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])\s*$', text)
    if match:
        return json.loads(match.group(1))
    raise ValueError('No JSON payload found in command output')


def run_json(script_name: str, code: str) -> tuple[dict[str, Any], str | None]:
    cmd = ['python', str(Path(_SCRIPT_DIR) / script_name), '--code', code, '--json']
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout = proc.stdout or ''
    stderr = proc.stderr or ''
    raw = stdout if proc.returncode == 0 else (stdout + ('\n' + stderr if stderr else ''))
    if proc.returncode != 0:
        return {'module': script_name.replace('.py', ''), 'error': raw.strip()[:2000]}, raw.strip()[:2000] or f'command failed: {proc.returncode}'
    try:
        payload = _extract_json_blob(stdout)
        if isinstance(payload, dict):
            return payload, None
        return {'module': script_name.replace('.py', ''), 'payload': payload}, None
    except Exception as e:
        debug_text = (stdout + ('\nSTDERR:\n' + stderr if stderr else ''))[:2000]
        return {'module': script_name.replace('.py', ''), 'error': f'{e}', 'raw_output': debug_text}, str(e)


def summarize(fundamental: dict[str, Any], valuation: dict[str, Any], strategy: dict[str, Any], event: dict[str, Any], news: dict[str, Any]) -> list[str]:
    lines = []
    fs = fundamental.get('summary', {})
    if fs.get('roe_pct') is not None:
        lines.append(f"ROE 约为 {fs.get('roe_pct')}%，可作为盈利质量参考。")
    if fs.get('debt_ratio_pct') is not None:
        lines.append(f"资产负债率约 {round(float(fs.get('debt_ratio_pct')), 2)}%，需结合行业特性解读。")
    vs = valuation.get('valuation_view', {})
    if vs.get('pe_view') not in (None, '未知'):
        lines.append(f"PE 视角判断为“{vs.get('pe_view')}”，PB 视角判断为“{vs.get('pb_view')}”。")
    ss = strategy.get('strategy_snapshot', {})
    if ss.get('regime'):
        lines.append(f"技术结构当前偏“{ss.get('regime')}”。")
    event_snapshot = event.get('event_snapshot', {})
    if event_snapshot.get('news_count'):
        lines.append(f"最近抓到 {event_snapshot.get('news_count')} 条相关新闻，可用于验证催化逻辑。")
    if news.get('headline_candidates'):
        lines.append('新闻与研报侧已有可跟踪线索，建议结合公告和资金流交叉确认。')
    if not lines:
        lines.append('当前可用数据有限，建议结合更多原始接口继续补充。')
    return lines


def make_markdown(code: str, report: dict[str, Any]) -> str:
    fundamental = report['fundamental']
    valuation = report['valuation']
    strategy = report['strategy']
    event = report['event']
    news = report['news']
    summary_lines = report['executive_summary']

    def bullet(items):
        if not items:
            return '- 暂无\n'
        return '\n'.join(f'- {x}' for x in items) + '\n'

    md = []
    md.append(f"# {code} 综合研报")
    md.append(f"> 生成时间：{report['generated_at']} | 数据源：AKShare + akshare-open")
    md.append('')
    md.append('## 一、核心结论')
    md.extend([f'- {x}' for x in summary_lines])
    md.append('')
    md.append('## 二、基本面分析')
    md.append('### 摘要指标')
    md.append('```json')
    md.append(json.dumps(fundamental.get('summary', {}), ensure_ascii=False, indent=2, default=str))
    md.append('```')
    md.append('### 优势')
    md.append(bullet(fundamental.get('strengths', [])))
    md.append('### 风险')
    md.append(bullet(fundamental.get('risks', [])))
    md.append('## 三、估值分析')
    md.append('```json')
    md.append(json.dumps(valuation.get('valuation_view', {}), ensure_ascii=False, indent=2, default=str))
    md.append('```')
    md.append('### 估值备注')
    md.append(bullet(valuation.get('notes', [])))
    md.append('## 四、策略分析')
    md.append('```json')
    md.append(json.dumps(strategy.get('strategy_snapshot', {}), ensure_ascii=False, indent=2, default=str))
    md.append('```')
    md.append('### 策略观察')
    md.append(bullet(strategy.get('tactical_observations', [])))
    md.append('## 五、事件驱动分析')
    md.append('```json')
    md.append(json.dumps(event.get('event_snapshot', {}), ensure_ascii=False, indent=2, default=str))
    md.append('```')
    md.append('### 催化线索')
    md.append(bullet(event.get('catalyst_summary', [])))
    md.append('## 六、财经新闻总结')
    md.append('### 新闻摘要')
    md.append(bullet(news.get('summary', [])))
    md.append('### 重点标题线索')
    md.append(bullet(news.get('headline_candidates', [])))
    md.append('## 七、模块错误与缺失')
    md.append('```json')
    md.append(json.dumps(report.get('module_errors', {}), ensure_ascii=False, indent=2, default=str))
    md.append('```')
    return '\n'.join(md).strip() + '\n'


def main():
    parser = argparse.ArgumentParser(description='One-click comprehensive equity research report')
    parser.add_argument('--code', required=True, help='stock code such as 000001 or 600519')
    parser.add_argument('--json', action='store_true', help='print json payload')
    parser.add_argument('--save', help='optional path to save markdown report')
    args = parser.parse_args()

    code = args.code.strip()
    fundamental, ferr = run_json('fundamental_analysis.py', code)
    valuation, verr = run_json('valuation_analysis.py', code)
    strategy, serr = run_json('strategy_analysis.py', code)
    event, eerr = run_json('event_driven_analysis.py', code)
    news, nerr = run_json('news_summary.py', code)

    report = {
        'module': 'comprehensive_report',
        'code': code,
        'generated_at': now_text(),
        'executive_summary': summarize(fundamental, valuation, strategy, event, news),
        'fundamental': fundamental,
        'valuation': valuation,
        'strategy': strategy,
        'event': event,
        'news': news,
        'module_errors': {
            'fundamental': ferr,
            'valuation': verr,
            'strategy': serr,
            'event': eerr,
            'news': nerr,
        },
    }
    report['markdown'] = make_markdown(code, report)

    if args.save:
        save_path = Path(args.save).expanduser()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(report['markdown'], encoding='utf-8')
        report['saved_to'] = str(save_path)

    if args.json:
        output_payload(report, True)
    else:
        print(report['markdown'])
        if report.get('saved_to'):
            print(f"\n[Saved] {report['saved_to']}")


if __name__ == '__main__':
    main()
