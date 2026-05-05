import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path('/home/admin/.hermes/scripts')
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ashare_data_utils as adu


def test_normalize_trade_date_handles_supported_inputs():
    assert adu.normalize_trade_date(date(2026, 5, 1)) == '2026-05-01'
    assert adu.normalize_trade_date(datetime(2026, 5, 1, 9, 30, 0)) == '2026-05-01'
    assert adu.normalize_trade_date('2026-05-01') == '2026-05-01'
    assert adu.normalize_trade_date('2026-05-01 09:30:00') == '2026-05-01'
    assert adu.normalize_trade_date(None) is None


def test_parse_datetime_safe_handles_supported_inputs():
    assert adu.parse_datetime_safe(date(2026, 5, 1)).date().isoformat() == '2026-05-01'
    assert adu.parse_datetime_safe(datetime(2026, 5, 1, 9, 30, 0)).isoformat().startswith('2026-05-01T09:30:00')
    assert adu.parse_datetime_safe('2026-05-01').date().isoformat() == '2026-05-01'
    assert adu.parse_datetime_safe('2026-05-01 09:30:00').isoformat().startswith('2026-05-01T09:30:00')
    assert adu.parse_datetime_safe(None) is None


def test_validate_data_asof_accepts_same_day_data():
    result = adu.validate_data_asof(
        '2026-05-01',
        data_date='2026-05-01',
        captured_at='2026-05-01 15:00:00',
        source='unit-test',
        context='same-day',
    )

    assert result['ok'] is True
    assert result['level'] == 'ok'
    assert result['target_date'] == '2026-05-01'
    assert result['data_date'] == '2026-05-01'
    assert result['captured_at'].startswith('2026-05-01T15:00:00')
    assert result['source'] == 'unit-test'
    assert result['context'] == 'same-day'


def test_validate_data_asof_rejects_previous_day_when_strict_today():
    result = adu.validate_data_asof('2026-05-02', data_date='2026-05-01', strict_today=True)

    assert result['ok'] is False
    assert result['level'] == 'error'
    assert 'strict_today' in result['reason']


def test_validate_data_asof_warns_previous_day_when_previous_close_allowed():
    result = adu.validate_data_asof(
        '2026-05-02',
        data_date='2026-05-01',
        strict_today=True,
        allow_previous_close_only=True,
    )

    assert result['ok'] is True
    assert result['level'] == 'warning'
    assert 'previous_close' in result['reason']


def test_validate_data_asof_rejects_future_data_date():
    result = adu.validate_data_asof('2026-05-01', data_date='2026-05-02')

    assert result['ok'] is False
    assert result['level'] == 'error'
    assert 'future' in result['reason']


def test_validate_data_asof_rejects_future_captured_at_date():
    result = adu.validate_data_asof('2026-05-01', data_date='2026-05-01', captured_at='2026-05-02 09:30:00')

    assert result['ok'] is False
    assert result['level'] == 'error'
    assert 'captured_at' in result['reason']


def test_validate_data_asof_warns_when_data_date_and_captured_at_missing():
    result = adu.validate_data_asof('2026-05-01')

    assert result['ok'] is True
    assert result['level'] == 'warning'
    assert 'missing' in result['reason']
