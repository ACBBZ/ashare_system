#!/usr/bin/env python3
import contextlib
import hashlib
import io
import json
import math
import os
import pickle
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime
from pathlib import Path

import easyquotation
import pandas as pd
import requests
import tushare as ts
from pytdx.config.hosts import hq_hosts
from pytdx.hq import TdxHq_API

DEFAULT_TIMEOUT = 20
DEFAULT_ATTEMPTS = 3
DEFAULT_SLEEP_SECONDS = 2
DEFAULT_QUOTE_CACHE_TTL = 20
DEFAULT_SPOT_CACHE_TTL = 20
DEFAULT_HIST_CACHE_TTL = 300
DEFAULT_INDEX_CACHE_TTL = 20
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Hermes/1.0'
EASTMONEY_UT = 'bd1d9ddb04089700cf9c27f6f7426281'
CACHE_ROOT = Path(os.getenv('ASHARE_CACHE_ROOT', str(Path.home() / '.hermes' / 'cache' / 'ashare-data-utils')))
XUEQIU_SYMBOL_URL = 'https://stock.xueqiu.com/v5/stock/realtime/quotec.json'

_CACHE_LOCK = threading.Lock()
_XUEQIU_SESSION = None
_TDX_HOST = None


def list_review_day_dirs(root, before_date=None):
    root = Path(root)
    before_date = str(before_date) if before_date is not None else None
    dirs = [p for p in root.glob('20*-*-*') if p.is_dir()]
    if before_date is not None:
        dirs = [p for p in dirs if p.name < before_date]
    return sorted(dirs, key=lambda p: p.name)


def preferred_review_day_dir(root, today_iso):
    root = Path(root)
    today_iso = str(today_iso)
    dirs = list_review_day_dirs(root, before_date=today_iso)
    if not dirs:
        return None
    try:
        yesterday = (pd.Timestamp(today_iso) - pd.Timedelta(days=1)).date().isoformat()
    except Exception:
        yesterday = None
    if yesterday:
        exact = root / yesterday
        if exact.exists() and exact.is_dir():
            return exact
    return dirs[-1]


def pick_first_existing(*paths):
    for path in paths:
        if path is None:
            continue
        p = Path(path)
        if p.exists():
            return p
    return None


def latest_review_file(root, pattern, today_iso, preferred_names=None):
    root = Path(root)
    preferred_names = list(preferred_names or [])
    day_dir = preferred_review_day_dir(root, today_iso)
    if day_dir:
        if preferred_names:
            picked = pick_first_existing(*[day_dir / name for name in preferred_names])
            if picked:
                return picked
        files = sorted(day_dir.glob(pattern))
        if files:
            return files[-1]
    files = sorted(root.glob(f'20*-*-*/{pattern}'))
    valid = [p for p in files if p.parent.name < str(today_iso)]
    return valid[-1] if valid else (files[-1] if files else None)


def ensure_cache_root():
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def parse_datetime_safe(value):
    """Parse common date/datetime inputs without raising.

    Returns a ``datetime`` for date, datetime, ISO-ish strings
    (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS), and ``None`` for missing or
    unparseable values. This helper is intentionally side-effect free so it can
    be used in cron/report validation paths without breaking existing chains.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    text = str(value).strip()
    if not text:
        return None
    text = text.replace('/', '-').replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:19] if '%H' in fmt else text[:10], fmt)
        except Exception:
            continue
    return None


def normalize_trade_date(value):
    """Normalize date/datetime/string input to YYYY-MM-DD, or None."""
    parsed = parse_datetime_safe(value)
    if parsed is None:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def _format_asof_datetime(value):
    parsed = parse_datetime_safe(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def validate_data_asof(
    target_date,
    data_date=None,
    captured_at=None,
    generated_at=None,
    source=None,
    strict_today=True,
    allow_previous_close_only=False,
    context='',
):
    """Validate that a data snapshot does not time-travel past target_date.

    The function returns a structured result instead of raising so existing
    盘前/开盘/收盘/持仓分析链路 can adopt it incrementally without being
    broken by validation failures.
    """
    target_iso = normalize_trade_date(target_date)
    data_iso = normalize_trade_date(data_date)
    captured_dt = parse_datetime_safe(captured_at)
    captured_iso = captured_dt.isoformat() if captured_dt else None
    captured_date_iso = captured_dt.date().isoformat() if captured_dt else None
    generated_iso = _format_asof_datetime(generated_at)

    result = {
        'ok': True,
        'level': 'ok',
        'reason': 'ok',
        'target_date': target_iso,
        'data_date': data_iso,
        'captured_at': captured_iso,
        'generated_at': generated_iso,
        'source': source,
        'context': context,
    }

    def mark(level, reason, ok=None):
        result['level'] = level
        result['reason'] = reason
        if ok is not None:
            result['ok'] = ok
        elif level == 'error':
            result['ok'] = False
        return result

    if target_iso is None:
        return mark('error', 'invalid_or_missing_target_date', False)

    if data_iso is None and captured_dt is None:
        return mark('warning', 'missing_data_date_and_captured_at', True)

    if data_iso is not None and data_iso > target_iso:
        return mark('error', 'future_data_date_gt_target_date', False)

    if captured_date_iso is not None and captured_date_iso > target_iso:
        return mark('error', 'captured_at_date_gt_target_date', False)

    if strict_today and data_iso is not None and data_iso != target_iso:
        if allow_previous_close_only and data_iso < target_iso:
            return mark('warning', 'previous_close_allowed_data_date_lt_target_date', True)
        return mark('error', 'strict_today_data_date_ne_target_date', False)

    if allow_previous_close_only and data_iso is not None and data_iso < target_iso:
        return mark('warning', 'previous_close_allowed_data_date_lt_target_date', True)

    return result


def _coerce_iso_date(value=None):
    if value is None:
        return datetime.now().astimezone().date().isoformat()
    if isinstance(value, datetime):
        return value.astimezone().date().isoformat() if value.tzinfo else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return datetime.now().astimezone().date().isoformat()
    return text[:10]


def _trade_calendar_cache_path():
    return CACHE_ROOT / 'cn_a_share_trade_calendar.json'


def _load_trade_calendar_cache(max_age_seconds=7 * 24 * 3600):
    path = _trade_calendar_cache_path()
    try:
        if not path.exists() or time.time() - path.stat().st_mtime > max_age_seconds:
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
        days = payload.get('trade_dates') if isinstance(payload, dict) else None
        if isinstance(days, list) and days:
            return {str(item)[:10] for item in days}
    except Exception:
        return None
    return None


def _save_trade_calendar_cache(trade_dates):
    try:
        ensure_cache_root()
        payload = {
            'generated_at': datetime.now().astimezone().isoformat(),
            'source': 'akshare.tool_trade_date_hist_sina',
            'trade_dates': sorted(str(item)[:10] for item in trade_dates if str(item).strip()),
        }
        _trade_calendar_cache_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def get_cn_a_share_trade_dates(use_cache=True):
    """Return known China A-share trading dates as ISO strings.

    Uses AkShare's Sina trading calendar and caches it locally so holiday
    checks stay fast inside cron pre-run scripts. If AkShare is temporarily
    unavailable, falls back to the stale cache (if present) before raising.
    """
    if use_cache:
        cached = _load_trade_calendar_cache()
        if cached:
            return cached

    import akshare as ak

    last_exc = None
    for attempts in ((1, 8), (1, 15)):
        try:
            df = ak_call(ak.tool_trade_date_hist_sina, attempts=attempts[0], timeout=attempts[1], suppress_output=True)
            if df is None or df.empty:
                raise RuntimeError('empty trade calendar')
            col = 'trade_date' if 'trade_date' in df.columns else df.columns[0]
            days = set()
            for item in df[col].dropna().tolist():
                if isinstance(item, (datetime, date, pd.Timestamp)):
                    days.add(pd.Timestamp(item).date().isoformat())
                else:
                    text = str(item).strip().replace('/', '-')
                    days.add(pd.Timestamp(text).date().isoformat())
            if not days:
                raise RuntimeError('no trade dates parsed')
            _save_trade_calendar_cache(days)
            return days
        except Exception as exc:
            last_exc = exc

    stale = _load_trade_calendar_cache(max_age_seconds=366 * 24 * 3600)
    if stale:
        return stale
    raise RuntimeError(f'failed to load A-share trade calendar: {last_exc}')


def is_cn_a_share_trading_day(day=None, use_cache=True):
    day_iso = _coerce_iso_date(day)
    return day_iso in get_cn_a_share_trade_dates(use_cache=use_cache)


def cn_a_share_cron_gate(day=None, *, task='ashare-cron', use_cache=True):
    """Build a Hermes cron wake-gate payload for A-share jobs.

    The scheduler skips the LLM/agent phase entirely when the last stdout line
    is JSON containing ``{"wakeAgent": false}``. On calendar lookup failure we
    wake the agent by default to avoid silently missing a real trading day.
    """
    day_iso = _coerce_iso_date(day)
    try:
        is_trade_day = is_cn_a_share_trading_day(day_iso, use_cache=use_cache)
        if is_trade_day:
            return {'wakeAgent': True, 'trade_date': day_iso, 'task': task, 'market': 'cn_a_share'}
        return {
            'wakeAgent': False,
            'trade_date': day_iso,
            'task': task,
            'market': 'cn_a_share',
            'reason': 'not_a_share_trading_day',
            'message': f'{day_iso} 不是 A 股交易日，跳过 cron agent 执行与推送',
        }
    except Exception as exc:
        return {
            'wakeAgent': True,
            'trade_date': day_iso,
            'task': task,
            'market': 'cn_a_share',
            'calendar_error': str(exc),
            'message': 'A 股交易日历检查失败；为避免漏跑，继续执行 cron',
        }


def skip_cron_if_not_a_share_trading_day(day=None, *, task='ashare-cron', use_cache=True):
    """Print a wake-gate JSON line and return True when the job should stop."""
    gate = cn_a_share_cron_gate(day, task=task, use_cache=use_cache)
    if gate.get('wakeAgent') is False:
        print(json.dumps(gate, ensure_ascii=False))
        return True
    return False


def safe_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace(',', '').replace('%', '').replace('—', '').strip()
            if value in {'', '-', '--', 'None', 'null'}:
                return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def normalize_code(code):
    s = str(code or '').strip().lower()
    for prefix in ('sh', 'sz', 'bj'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    digits = ''.join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else None


def market_prefix(code):
    code = normalize_code(code)
    if not code:
        return None
    if code.startswith(('5', '6', '9')):
        return 'sh'
    if code.startswith(('4', '8')):
        return 'bj'
    return 'sz'


def secid(code):
    code = normalize_code(code)
    if not code:
        return None
    if code.startswith(('5', '6', '9')):
        return f'1.{code}'
    if code.startswith(('4', '8')):
        return f'0.{code}'
    return f'0.{code}'


def to_ts_code(code):
    code = normalize_code(code)
    prefix = market_prefix(code)
    if not code or not prefix:
        return None
    return f"{code}.{prefix.upper()}"


def pytdx_market(code):
    code = normalize_code(code)
    if not code:
        return None
    if code.startswith(('5', '6', '9')):
        return 1
    if code.startswith(('4', '8')):
        return 0
    return 0


def cache_key(*parts):
    raw = '||'.join(str(p) for p in parts)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


class _AlarmTimeout(Exception):
    pass


def _run_with_timeout(fn, timeout):
    if timeout and threading.current_thread() is threading.main_thread():
        def _handler(signum, frame):
            raise _AlarmTimeout(f'call timed out after {timeout}s')

        old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return fn()
        except _AlarmTimeout as exc:
            raise TimeoutError(str(exc)) from exc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError(f'call timed out after {timeout}s') from exc


def _call(fn, suppress_output=False):
    if not suppress_output:
        return fn()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn()


def retry_call(fn, attempts=DEFAULT_ATTEMPTS, timeout=DEFAULT_TIMEOUT, sleep_seconds=DEFAULT_SLEEP_SECONDS, suppress_output=False, label=None):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_with_timeout(lambda: _call(fn, suppress_output=suppress_output), timeout)
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(sleep_seconds * attempt)
    if label:
        raise RuntimeError(f'{label} failed after {attempts} attempts: {last}') from last
    raise last


def ak_call(func, *args, attempts=DEFAULT_ATTEMPTS, timeout=DEFAULT_TIMEOUT, sleep_seconds=DEFAULT_SLEEP_SECONDS, suppress_output=True, **kwargs):
    label = getattr(func, '__name__', 'akshare_call')
    return retry_call(
        lambda: func(*args, **kwargs),
        attempts=attempts,
        timeout=timeout,
        sleep_seconds=sleep_seconds,
        suppress_output=suppress_output,
        label=label,
    )


def requests_get(url, *, params=None, headers=None, timeout=DEFAULT_TIMEOUT, attempts=3, sleep_seconds=1.5, session=None):
    merged_headers = {'User-Agent': USER_AGENT}
    if headers:
        merged_headers.update(headers)
    req_session = session or requests
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return req_session.get(url, params=params, headers=merged_headers, timeout=timeout)
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(sleep_seconds * attempt)
    raise last


def _cache_file(namespace, key, suffix):
    ensure_cache_root()
    ns_dir = CACHE_ROOT / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    return ns_dir / f'{key}.{suffix}'


def _cache_get(namespace, key, ttl_seconds, loader, suffix):
    path = _cache_file(namespace, key, suffix)
    if not path.exists():
        return None
    if ttl_seconds is not None and time.time() - path.stat().st_mtime > ttl_seconds:
        return None
    try:
        return loader(path)
    except Exception:
        return None


def _cache_set(namespace, key, value, dumper, suffix):
    path = _cache_file(namespace, key, suffix)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with _CACHE_LOCK:
        dumper(tmp, value)
        tmp.replace(path)
    return value


def cache_get_json(namespace, key, ttl_seconds):
    return _cache_get(namespace, key, ttl_seconds, lambda p: json.loads(p.read_text(encoding='utf-8')), 'json')


def cache_set_json(namespace, key, value):
    return _cache_set(namespace, key, value, lambda p, v: p.write_text(json.dumps(v, ensure_ascii=False), encoding='utf-8'), 'json')


def cache_get_pickle(namespace, key, ttl_seconds):
    return _cache_get(namespace, key, ttl_seconds, lambda p: pickle.loads(p.read_bytes()), 'pkl')


def cache_set_pickle(namespace, key, value):
    return _cache_set(namespace, key, value, lambda p, v: p.write_bytes(pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)), 'pkl')


def _quote_result(source, code, name, latest, **extra):
    return {
        'code': normalize_code(code),
        'name': name or normalize_code(code),
        'latest': safe_float(latest),
        'source': source,
        **{k: v for k, v in extra.items()},
    }


def _valid_quote(quote):
    return bool(quote and safe_float(quote.get('latest')) is not None)


def fetch_tencent_quote(code):
    code = normalize_code(code)
    prefix = market_prefix(code)
    if not code or not prefix:
        return None
    symbol = f'{prefix}{code}'
    resp = requests_get(
        f'https://qt.gtimg.cn/q={symbol}',
        headers={'Referer': 'https://gu.qq.com'},
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.text.strip()
    if '"' not in raw:
        return None
    fields = raw.split('"')[1].split('~')
    if len(fields) < 35:
        return None
    latest = safe_float(fields[3])
    prev_close = safe_float(fields[4])
    open_price = safe_float(fields[5])
    pct = safe_float(fields[32] if len(fields) > 32 else None)
    if pct is None and latest is not None and prev_close not in (None, 0):
        pct = (latest / prev_close - 1) * 100
    return _quote_result(
        'tencent_quote',
        code,
        fields[1],
        latest,
        prev_close=prev_close,
        open=open_price,
        high=safe_float(fields[30] if len(fields) > 30 else None),
        low=safe_float(fields[31] if len(fields) > 31 else None),
        volume=safe_float(fields[6] if len(fields) > 6 else None),
        amount=safe_float(fields[37] if len(fields) > 37 else None),
        pct=pct,
        change_pct=pct,
        change_amount=safe_float(fields[33] if len(fields) > 33 else None),
        buy1=safe_float(fields[9] if len(fields) > 9 else None),
        sell1=safe_float(fields[19] if len(fields) > 19 else None),
        buy1_vol=safe_float(fields[10] if len(fields) > 10 else None),
        sell1_vol=safe_float(fields[20] if len(fields) > 20 else None),
    )


def fetch_sina_quote(code):
    code = normalize_code(code)
    prefix = market_prefix(code)
    if not code or not prefix:
        return None
    resp = requests_get(
        f'https://hq.sinajs.cn/list={prefix}{code}',
        headers={'Referer': 'https://finance.sina.com.cn'},
        timeout=10,
    )
    resp.raise_for_status()
    resp.encoding = 'gbk'
    raw = resp.text.strip()
    if '"' not in raw:
        return None
    fields = raw.split('"')[1].split(',')
    if len(fields) < 10:
        return None
    latest = safe_float(fields[3])
    prev_close = safe_float(fields[2])
    pct = None
    if latest is not None and prev_close not in (None, 0):
        pct = (latest / prev_close - 1) * 100
    return _quote_result(
        'sina_quote',
        code,
        fields[0],
        latest,
        open=safe_float(fields[1]),
        prev_close=prev_close,
        high=safe_float(fields[4]),
        low=safe_float(fields[5]),
        volume=safe_float(fields[8]),
        amount=safe_float(fields[9]),
        pct=pct,
        change_pct=pct,
    )


def fetch_eastmoney_quote(code):
    code = normalize_code(code)
    sid = secid(code)
    if not code or not sid:
        return None
    resp = requests_get(
        'https://push2.eastmoney.com/api/qt/stock/get',
        params={'secid': sid, 'fields': 'f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170'},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = (payload or {}).get('data') or {}
    if not data:
        return None

    def _em_cents(field):
        value = safe_float(data.get(field))
        if value is None:
            return None
        return value / 100

    latest = _em_cents('f43')
    pct = _em_cents('f170')
    change_amount = _em_cents('f169')
    return _quote_result(
        'eastmoney_quote',
        code,
        data.get('f58') or code,
        latest,
        prev_close=_em_cents('f60') or _em_cents('f44'),
        open=_em_cents('f46'),
        high=_em_cents('f44'),
        low=_em_cents('f45'),
        volume=safe_float(data.get('f47')),
        amount=safe_float(data.get('f48')),
        pct=pct,
        change_pct=pct,
        change_amount=change_amount,
    )


def _get_xueqiu_session():
    global _XUEQIU_SESSION
    if _XUEQIU_SESSION is not None:
        return _XUEQIU_SESSION
    sess = requests.Session()
    headers = {'Referer': 'https://xueqiu.com/'}
    resp = requests_get('https://xueqiu.com', headers=headers, timeout=15, session=sess)
    resp.raise_for_status()
    _XUEQIU_SESSION = sess
    return sess


def fetch_xueqiu_quote(code):
    code = normalize_code(code)
    prefix = market_prefix(code)
    if not code or prefix not in {'sh', 'sz'}:
        return None
    symbol = f'{prefix.upper()}{code}'
    sess = _get_xueqiu_session()
    headers = {'Referer': f'https://xueqiu.com/S/{symbol}'}
    resp = requests_get(XUEQIU_SYMBOL_URL, params={'symbol': symbol}, headers=headers, timeout=15, session=sess)
    resp.raise_for_status()
    payload = resp.json()
    rows = (payload or {}).get('data') or []
    if not rows:
        return None
    row = rows[0]
    return _quote_result(
        'xueqiu_quote',
        code,
        row.get('symbol') or code,
        row.get('current'),
        prev_close=row.get('last_close'),
        open=row.get('open'),
        high=row.get('high'),
        low=row.get('low'),
        volume=row.get('volume'),
        amount=row.get('amount'),
        pct=row.get('percent'),
        change_pct=row.get('percent'),
        change_amount=row.get('chg'),
        turnover_rate=row.get('turnover_rate'),
    )


def _connect_pytdx():
    global _TDX_HOST
    candidates = []
    if _TDX_HOST:
        candidates.append(_TDX_HOST)
    candidates.extend(hq_hosts)
    last = None
    seen = set()
    for item in candidates:
        name, host, port = item
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        api = TdxHq_API(heartbeat=True, auto_retry=True, multithread=False)
        try:
            ok = api.connect(host, port, time_out=5)
            if ok:
                _TDX_HOST = (name, host, port)
                return api, _TDX_HOST
        except Exception as exc:
            last = exc
        try:
            api.disconnect()
        except Exception:
            pass
    raise RuntimeError(f'pytdx connect failed: {last}')


def fetch_pytdx_quote(code):
    code = normalize_code(code)
    market = pytdx_market(code)
    if code is None or market is None:
        return None
    api, host_info = _connect_pytdx()
    try:
        rows = api.get_security_quotes([(market, code)])
        if not rows:
            return None
        row = rows[0]
        last_close = safe_float(row.get('last_close'))
        latest = safe_float(row.get('price'))
        pct = None
        if latest is not None and last_close not in (None, 0):
            pct = (latest / last_close - 1) * 100
        return _quote_result(
            'pytdx_quote',
            code,
            code,
            latest,
            prev_close=last_close,
            open=safe_float(row.get('open')),
            high=safe_float(row.get('high')),
            low=safe_float(row.get('low')),
            volume=safe_float(row.get('vol')),
            amount=safe_float(row.get('amount')),
            pct=pct,
            change_pct=pct,
            buy1=safe_float(row.get('bid1')),
            sell1=safe_float(row.get('ask1')),
            buy1_vol=safe_float(row.get('bid_vol1')),
            sell1_vol=safe_float(row.get('ask_vol1')),
            servertime=row.get('servertime'),
            host=f'{host_info[0]}:{host_info[1]}:{host_info[2]}',
        )
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def fetch_tushare_quote(code):
    token = os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN')
    if not token:
        raise RuntimeError('missing TUSHARE_TOKEN')
    ts_code = to_ts_code(code)
    ts.set_token(token)
    df = retry_call(lambda: ts.realtime_quote(ts_code=ts_code, src='sina'), attempts=2, timeout=20, suppress_output=True, label='tushare.realtime_quote')
    if df is None or df.empty:
        return None
    row = df.iloc[0].to_dict()
    latest = safe_float(row.get('PRICE'))
    prev_close = safe_float(row.get('PRE_CLOSE'))
    pct = None
    if latest is not None and prev_close not in (None, 0):
        pct = (latest / prev_close - 1) * 100
    return _quote_result(
        'tushare_quote',
        code,
        row.get('NAME') or ts_code,
        latest,
        prev_close=prev_close,
        open=safe_float(row.get('OPEN')),
        high=safe_float(row.get('HIGH')),
        low=safe_float(row.get('LOW')),
        volume=safe_float(row.get('VOLUME')),
        amount=safe_float(row.get('AMOUNT')),
        pct=pct,
        change_pct=pct,
        buy1=safe_float(row.get('B1_P')),
        sell1=safe_float(row.get('A1_P')),
        buy1_vol=safe_float(row.get('B1_V')),
        sell1_vol=safe_float(row.get('A1_V')),
    )


def fetch_quote_with_fallback(code, *, primary=None, ttl_seconds=DEFAULT_QUOTE_CACHE_TTL, refresh=False):
    code = normalize_code(code)
    key = cache_key('quote', code)
    if not refresh:
        cached = cache_get_json('quotes', key, ttl_seconds)
        if _valid_quote(cached):
            return cached

    errors = []
    providers = []
    if primary:
        providers.append(primary)
    providers.extend([
        fetch_xueqiu_quote,
        fetch_pytdx_quote,
        fetch_tencent_quote,
        fetch_sina_quote,
        fetch_eastmoney_quote,
        fetch_tushare_quote,
    ])
    seen = set()
    for provider in providers:
        if provider in seen or provider is None:
            continue
        seen.add(provider)
        try:
            quote = provider(code)
            if _valid_quote(quote):
                return cache_set_json('quotes', key, quote)
            errors.append(f'{provider.__name__} returned empty')
        except Exception as exc:
            errors.append(f'{provider.__name__}: {exc}')
    raise RuntimeError(' ; '.join(errors))


def fetch_eastmoney_spot_df(ttl_seconds=DEFAULT_SPOT_CACHE_TTL, refresh=False):
    key = cache_key('eastmoney_spot_df')
    if not refresh:
        cached = cache_get_pickle('spot_df', key, ttl_seconds)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            cached.attrs['source'] = cached.attrs.get('source', 'eastmoney_direct_clist_cache')
            return cached
    params = {
        'pn': '1',
        'pz': '6000',
        'po': '1',
        'np': '1',
        'ut': EASTMONEY_UT,
        'fltt': '2',
        'invt': '2',
        'fid': 'f3',
        'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
        'fields': 'f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f23,f20,f21',
    }
    resp = requests_get('https://82.push2.eastmoney.com/api/qt/clist/get', params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    diff = (((payload or {}).get('data') or {}).get('diff')) or []
    rows = []
    for item in diff:
        rows.append({
            '代码': normalize_code(item.get('f12')),
            '名称': item.get('f14'),
            '最新价': safe_float(item.get('f2')),
            '涨跌幅': safe_float(item.get('f3')),
            '涨跌额': safe_float(item.get('f4')),
            '成交量': safe_float(item.get('f5')),
            '成交额': safe_float(item.get('f6')),
            '振幅': safe_float(item.get('f7')),
            '换手率': safe_float(item.get('f8')),
            '市盈率-动态': safe_float(item.get('f9')),
            '市净率': safe_float(item.get('f23')),
            '总市值': safe_float(item.get('f20')),
            '流通市值': safe_float(item.get('f21')),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[df['代码'].notna()].copy()
    df.attrs['source'] = 'eastmoney_direct_clist'
    return cache_set_pickle('spot_df', key, df)


def fetch_index_quotes(index_map, ttl_seconds=DEFAULT_INDEX_CACHE_TTL, refresh=False):
    key = cache_key('index_quotes', json.dumps(index_map, ensure_ascii=False, sort_keys=True))
    if not refresh:
        cached = cache_get_json('index_quotes', key, ttl_seconds)
        if isinstance(cached, list) and cached:
            return cached, []
    items = []
    errors = []
    for symbol, label in index_map.items():
        code = normalize_code(symbol)
        try:
            quote = fetch_quote_with_fallback(code, ttl_seconds=ttl_seconds, refresh=refresh)
            items.append({
                'index_code': symbol,
                'index_name': label,
                'latest_value': safe_float(quote.get('latest')),
                'pct_change': safe_float(quote.get('pct') or quote.get('change_pct')),
                'amount': safe_float(quote.get('amount')),
                'high': safe_float(quote.get('high')),
                'low': safe_float(quote.get('low')),
                'raw': quote,
            })
        except Exception as exc:
            errors.append(f'{symbol} fallback failed: {exc}')
    if items:
        cache_set_json('index_quotes', key, items)
    return items, errors


def fetch_em_hist_df(code, start_date, end_date, *, adjust='qfq', ttl_seconds=DEFAULT_HIST_CACHE_TTL, refresh=False):
    code = normalize_code(code)
    key = cache_key('em_hist', code, start_date, end_date, adjust)
    if not refresh:
        cached = cache_get_pickle('hist_df', key, ttl_seconds)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            cached.attrs['source'] = cached.attrs.get('source', 'eastmoney_hist_cache')
            return cached
    sid = secid(code)
    if not code or not sid:
        return pd.DataFrame()
    adjust_map = {'': '0', None: '0', 'qfq': '1', 'hfq': '2'}
    params = {
        'secid': sid,
        'ut': EASTMONEY_UT,
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101',
        'fqt': adjust_map.get(adjust, '1'),
        'beg': str(start_date),
        'end': str(end_date),
        'smplmt': '1000',
        'lmt': '1000',
    }
    resp = requests_get('https://push2his.eastmoney.com/api/qt/stock/kline/get', params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    klines = (((payload or {}).get('data') or {}).get('klines')) or []
    rows = []
    for line in klines:
        parts = str(line).split(',')
        if len(parts) < 11:
            continue
        rows.append({
            'date': pd.to_datetime(parts[0], errors='coerce'),
            'open': safe_float(parts[1]),
            'close': safe_float(parts[2]),
            'high': safe_float(parts[3]),
            'low': safe_float(parts[4]),
            'volume': safe_float(parts[5]),
            'amount': safe_float(parts[6]),
            'amplitude': safe_float(parts[7]),
            'pct_change': safe_float(parts[8]),
            'change_amount': safe_float(parts[9]),
            'turnover': safe_float(parts[10]),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=['date', 'close']).sort_values('date').reset_index(drop=True)
        df.attrs['source'] = 'eastmoney_hist'
    return cache_set_pickle('hist_df', key, df)


def fetch_pytdx_hist_df(code, start_date, end_date, *, ttl_seconds=DEFAULT_HIST_CACHE_TTL, refresh=False):
    code = normalize_code(code)
    market = pytdx_market(code)
    key = cache_key('pytdx_hist', code, start_date, end_date)
    if not refresh:
        cached = cache_get_pickle('hist_df', key, ttl_seconds)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            cached.attrs['source'] = cached.attrs.get('source', 'pytdx_hist_cache')
            return cached
    if code is None or market is None:
        return pd.DataFrame()
    api, _host = _connect_pytdx()
    try:
        all_rows = []
        for start in range(0, 2400, 800):
            rows = api.get_security_bars(9, market, code, start, 800)
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < 800:
                break
        df = pd.DataFrame(all_rows)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if df.empty:
        return df
    df = df.rename(columns={'vol': 'volume'})
    df['date'] = pd.to_datetime(df['datetime'].astype(str).str.slice(0, 10), errors='coerce')
    for col in ['open', 'close', 'high', 'low', 'volume', 'amount']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    start_ts = pd.to_datetime(str(start_date), errors='coerce')
    end_ts = pd.to_datetime(str(end_date), errors='coerce')
    df = df.dropna(subset=['date', 'close']).sort_values('date').drop_duplicates(subset=['date'], keep='last')
    if pd.notna(start_ts):
        df = df[df['date'] >= start_ts]
    if pd.notna(end_ts):
        df = df[df['date'] <= end_ts]
    df = df.reset_index(drop=True)
    if len(df) >= 2:
        df['pct_change'] = df['close'].pct_change() * 100
        df['change_amount'] = df['close'].diff()
    df.attrs['source'] = 'pytdx_hist_unadjusted'
    return cache_set_pickle('hist_df', key, df)


def fetch_tushare_hist_df(code, start_date, end_date, *, adjust='qfq', ttl_seconds=DEFAULT_HIST_CACHE_TTL, refresh=False):
    token = os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN')
    if not token:
        raise RuntimeError('missing TUSHARE_TOKEN')
    code = normalize_code(code)
    key = cache_key('tushare_hist', code, start_date, end_date, adjust)
    if not refresh:
        cached = cache_get_pickle('hist_df', key, ttl_seconds)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            cached.attrs['source'] = cached.attrs.get('source', 'tushare_hist_cache')
            return cached
    ts.set_token(token)
    ts_code = to_ts_code(code)
    df = retry_call(lambda: ts.pro_bar(ts_code=ts_code, start_date=str(start_date), end_date=str(end_date), adj=adjust), attempts=2, timeout=25, suppress_output=True, label='tushare.pro_bar')
    if df is None or df.empty:
        return pd.DataFrame()
    rename_map = {
        'trade_date': 'date',
        'vol': 'volume',
        'pct_chg': 'pct_change',
        'change': 'change_amount',
        'amount': 'amount',
    }
    df = df.rename(columns=rename_map).copy()
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d', errors='coerce')
    for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'pct_change', 'change_amount']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['date', 'close']).sort_values('date').reset_index(drop=True)
    df.attrs['source'] = 'tushare_hist'
    return cache_set_pickle('hist_df', key, df)


def fetch_hist_df_with_fallback(code, start_date, end_date, *, adjust='qfq', ttl_seconds=DEFAULT_HIST_CACHE_TTL, refresh=False):
    key = cache_key('hist_fallback', normalize_code(code), start_date, end_date, adjust)
    if not refresh:
        cached = cache_get_pickle('hist_df', key, ttl_seconds)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            cached.attrs['source'] = cached.attrs.get('source', 'hist_fallback_cache')
            return cached
    errors = []
    providers = [
        lambda: fetch_em_hist_df(code, start_date, end_date, adjust=adjust, ttl_seconds=ttl_seconds, refresh=refresh),
        lambda: fetch_pytdx_hist_df(code, start_date, end_date, ttl_seconds=ttl_seconds, refresh=refresh),
        lambda: fetch_tushare_hist_df(code, start_date, end_date, adjust=adjust, ttl_seconds=ttl_seconds, refresh=refresh),
    ]
    for provider in providers:
        try:
            df = provider()
            if isinstance(df, pd.DataFrame) and not df.empty:
                return cache_set_pickle('hist_df', key, df)
            errors.append(f'{provider.__name__} returned empty')
        except Exception as exc:
            errors.append(f'{provider.__name__}: {exc}')
    raise RuntimeError(' ; '.join(errors))


def validate_quote_payload(quote):
    latest = safe_float((quote or {}).get('latest'))
    prev_close = safe_float((quote or {}).get('prev_close'))
    high = safe_float((quote or {}).get('high'))
    low = safe_float((quote or {}).get('low'))
    issues = []
    if latest is None or latest <= 0:
        issues.append('latest<=0')
    if prev_close is not None and prev_close <= 0:
        issues.append('prev_close<=0')
    if high is not None and low is not None and high < low:
        issues.append('high<low')
    if latest is not None and high is not None and latest > high * 1.2:
        issues.append('latest_too_high_vs_high')
    if latest is not None and low is not None and latest < low * 0.8:
        issues.append('latest_too_low_vs_low')
    return {'valid': not issues, 'issues': issues}
