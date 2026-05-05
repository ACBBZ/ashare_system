#!/usr/bin/env python3
import json
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

def normalize_code(code: str) -> str:
    return str(code or '').strip().upper()

def full_code(code: str) -> str:
    code = normalize_code(code)
    if '.' in code:
        return code
    if code.startswith(('6', '9')):
        return f'SH{code}'
    if code.startswith(('0', '3', '8', '4')):
        return f'SZ{code}'
    return code

def eastmoney_code(code: str) -> str:
    code = normalize_code(code)
    raw = code.replace('SH', '').replace('SZ', '')
    suffix = 'SZ' if raw.startswith(('0', '3', '8', '4')) else 'SH'
    return f'{raw}.{suffix}'

def to_records(df: Any) -> list[dict[str, Any]]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    return df.to_dict(orient='records')

def first_existing(row: dict[str, Any], keys: Iterable[str], default=None):
    for key in keys:
        if key in row and row[key] not in (None, ''):
            return row[key]
    return default

def as_float(value: Any, default=None):
    if value in (None, '', '-', '--'):
        return default
    try:
        text = str(value).replace(',', '').replace('%', '').strip()
        return float(text)
    except Exception:
        return default

def find_date_key(df: pd.DataFrame):
    for key in ['日期', 'date', '报告期', '公告日期', '发布时间', '时间']:
        if key in df.columns:
            return key
    return None

def latest_row(df: pd.DataFrame):
    if df is None or df.empty:
        return {}
    date_key = find_date_key(df)
    if date_key:
        try:
            temp = df.copy()
            temp[date_key] = pd.to_datetime(temp[date_key], errors='coerce')
            temp = temp.sort_values(by=date_key, ascending=False)
            return temp.iloc[0].to_dict()
        except Exception:
            pass
    return df.iloc[0].to_dict()

def recent_rows(df: pd.DataFrame, n=10):
    if df is None or df.empty:
        return []
    date_key = find_date_key(df)
    try:
        temp = df.copy()
        if date_key:
            temp[date_key] = pd.to_datetime(temp[date_key], errors='coerce')
            temp = temp.sort_values(by=date_key, ascending=False)
        return temp.head(n).to_dict(orient='records')
    except Exception:
        return df.head(n).to_dict(orient='records')

def pct_change(last, prev):
    if last in (None, 0) or prev in (None, 0):
        return None
    try:
        return (float(last) - float(prev)) / float(prev) * 100
    except Exception:
        return None

def output_payload(payload: dict[str, Any], as_json: bool):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    for k, v in payload.items():
        if isinstance(v, (dict, list)):
            print(f'{k}:')
            print(json.dumps(v, ensure_ascii=False, indent=2, default=str))
        else:
            print(f'{k}: {v}')

def now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
