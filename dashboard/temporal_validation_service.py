"""Fail-closed, read-only delivery of chronological retrospective validations."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings

from .morning_service import MSK, TICKER, _count, _finite, _report, _text, _time, digest


MAX_ARTIFACT_BYTES = 5_000_000
TESTS = {'test_2024': ('2024-01-01', '2025-01-01'),
         'test_2025': ('2025-01-01', '2026-01-01'),
         'test_2026': ('2026-01-01', '2026-10-23')}
NOTICE = ('Ретроспектива, не заранее записанные прогнозы. Все три модели используют '
          'один подход 17:45; меняются только периоды обучения и калибровки. '
          'Модели не выбираются по лучшему результату и не подключаются к торговле или Telegram.')


def model_spec():
    from . import temporal_validation
    return temporal_validation


def _dates(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid date range')
    result = {}
    for key in ('start', 'end_exclusive'):
        raw = value[key]
        if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError('Invalid date')
        result[key] = date.fromisoformat(raw).isoformat()
    if result['start'] >= result['end_exclusive']:
        raise ValueError('Invalid date range')
    return result


def _periods(value, name):
    if not isinstance(value, dict) or set(value) != {'train', 'calibration', 'test'}:
        raise ValueError('Missing chronological periods')
    result = {key: _dates(value[key]) for key in ('train', 'calibration', 'test')}
    if (result['train']['end_exclusive'] > result['calibration']['start']
            or result['calibration']['end_exclusive'] > result['test']['start']
            or tuple(result['test'][key] for key in ('start', 'end_exclusive')) != TESTS[name]):
        raise ValueError('Overlapping or changed test periods')
    expected = {fold['id']: fold['periods'] for fold in model_spec().FOLDS}
    if result != expected[name]:
        raise ValueError('Changed fixed training or calibration periods')
    return result


def _metrics(value, trained, calibrated):
    # Reuse the frozen projection without modifying its strict two-period contract.
    result = _report({'trained_rows': trained, 'calibration_rows': calibrated,
                      'periods': {'test': value, 'later': value}})['periods']['test']
    for key in ('rmse_bps', 'baseline_rmse_bps', 'mean_baseline_rmse_bps', 'mape_pct',
                'baseline_mape_pct', 'mean_baseline_mape_pct',
                'mean_signed_error_bps', 'direction_baseline_pct'):
        metric = value[key]
        if metric is not None and (not _finite(metric)
                                  or key != 'mean_signed_error_bps' and metric < 0
                                  or key == 'direction_baseline_pct' and metric > 100):
            raise ValueError('Invalid extra metric')
        if result['samples'] == 0 and metric is not None:
            raise ValueError('Absent observations cannot prove an error metric')
        result[key] = metric
    for key in ('actual_up', 'actual_down', 'actual_zero'):
        if not _count(value[key]):
            raise ValueError('Invalid movement count')
        result[key] = value[key]
    if sum(result[key] for key in ('actual_up', 'actual_down', 'actual_zero')) != result['samples']:
        raise ValueError('Movement counts do not match observations')
    label = value['direction_baseline_label']
    if label not in ('Всегда рост', 'Всегда падение', 'Нет направления'):
        raise ValueError('Invalid fixed baseline label')
    result['direction_baseline_label'] = label
    return result


def _stocks(value, samples):
    if not isinstance(value, dict) or samples > 0 and not value or len(value) > 100:
        raise ValueError('Invalid stocks')
    result = {}
    for ticker, stock in value.items():
        if not isinstance(ticker, str) or not TICKER.fullmatch(ticker) or not isinstance(stock, dict):
            raise ValueError('Invalid stock')
        if not _count(stock['samples']):
            raise ValueError('Invalid stock count')
        item = {'samples': stock['samples']}
        for key in ('mae_rub', 'baseline_mae_rub', 'rmse_rub', 'mape_pct',
                    'baseline_rmse_rub', 'mean_baseline_rmse_rub',
                    'baseline_mape_pct', 'mean_baseline_mape_pct'):
            metric = stock[key]
            if metric is not None and (not _finite(metric) or metric < 0):
                raise ValueError('Invalid stock metric')
            if stock['samples'] == 0 and metric is not None:
                raise ValueError('Absent stock observations cannot prove errors')
            item[key] = metric
        if 'mean_baseline_mae_rub' in stock:
            metric = stock['mean_baseline_mae_rub']
            if (metric is not None and (not _finite(metric) or metric < 0)
                    or stock['samples'] == 0 and metric is not None):
                raise ValueError('Invalid stock mean baseline')
            item['mean_baseline_mae_rub'] = metric
        result[ticker] = item
    if sum(row['samples'] for row in result.values()) != samples:
        raise ValueError('Stock samples do not match report')
    return result


def _examples(value, period, created):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('Invalid examples')
    result = []
    seen = set()
    for row in value:
        if (not isinstance(row, dict) or not isinstance(row['ticker'], str)
                or not TICKER.fullmatch(row['ticker'])
                or row['kind'] not in ('latest', 'largest_error')):
            raise ValueError('Invalid example identity')
        as_of, target = _time(row['as_of']), _time(row['target_at'])
        local = as_of.astimezone(MSK)
        expected = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        end = datetime.fromisoformat(period['end_exclusive']).replace(tzinfo=MSK)
        if (local.weekday() > 3 or (local.hour, local.minute, local.second, local.microsecond) != (17, 45, 0, 0)
                or target != expected.astimezone(timezone.utc)
                or target + timedelta(minutes=15) > created
                or target + timedelta(minutes=15) >= end
                or not period['start'] <= local.date().isoformat() < period['end_exclusive']):
            raise ValueError('Invalid example time')
        identity = (row['ticker'], as_of, row['kind'])
        if identity in seen:
            raise ValueError('Duplicate example')
        seen.add(identity)
        item = {key: row[key] for key in ('ticker', 'as_of', 'target_at', 'kind')}
        for key in ('anchor_price', 'predicted_price', 'actual_price'):
            if not _finite(row[key]) or key != 'predicted_price' and row[key] <= 0:
                raise ValueError('Invalid example price')
            item[key] = row[key]
        expected_rub = abs(row['actual_price'] - row['predicted_price'])
        for key, expected_value in (('absolute_error_rub', expected_rub),
                                    ('absolute_error_pct', expected_rub / row['actual_price'] * 100)):
            if (not _finite(row[key]) or row[key] < 0
                    or abs(row[key] - expected_value) > max(0.000001, abs(expected_value) * 0.000001)):
                raise ValueError('Inconsistent example error')
            item[key] = row[key]
        result.append(item)
    return result


def _coverage(value, created):
    if not isinstance(value, dict) or not value or len(value) > 100:
        raise ValueError('Invalid coverage')
    result = {}
    for ticker, row in value.items():
        if not isinstance(ticker, str) or not TICKER.fullmatch(ticker) or not isinstance(row, dict):
            raise ValueError('Invalid coverage stock')
        if not _count(row['bars']) or row['bars'] > 2**53 - 1:
            raise ValueError('Invalid coverage count')
        begin, end = _time(row['requested_from']), _time(row['to'])
        if begin >= end:
            raise ValueError('Invalid requested range')
        if row['bars'] == 0:
            if row['first_actual'] is not None or row['last_actual'] is not None:
                raise ValueError('Absent observations cannot have actual dates')
        else:
            first, last = _time(row['first_actual']), _time(row['last_actual'])
            if (not begin <= first <= last < end or last > created
                    or row['bars'] == 1 and first != last
                    or row['bars'] > 1 and first == last):
                raise ValueError('Invalid actual coverage')
        result[ticker] = {key: row[key] for key in
                          ('bars', 'first_actual', 'last_actual', 'requested_from', 'to')}
    return result


def _folds(value, created):
    if not isinstance(value, list) or len(value) != len(TESTS):
        raise ValueError('Missing folds')
    result = {}
    for fold in value:
        if not isinstance(fold, dict) or fold['id'] not in TESTS or fold['id'] in result:
            raise ValueError('Invalid fold')
        if not _text(fold['label'], 250):
            raise ValueError('Invalid fold label')
        if not _count(fold['trained_rows']) or not _count(fold['calibration_rows']):
            raise ValueError('Invalid fit counts')
        periods = _periods(fold['periods'], fold['id'])
        item = {key: fold[key] for key in ('id', 'label', 'trained_rows', 'calibration_rows')}
        item['periods'] = periods
        for prefix, period in (('', periods['test']), ('current_year_',
                               {'start': TESTS['test_2026'][0], 'end_exclusive': TESTS['test_2026'][1]})):
            if _dates(fold[prefix + 'report']) != period:
                raise ValueError('Changed report period')
            report = _metrics(fold[prefix + 'report'], fold['trained_rows'], fold['calibration_rows'])
            item[prefix + 'report'] = report
            item[prefix + 'stocks'] = _stocks(fold[prefix + 'stocks'], report['samples'])
            item[prefix + 'examples'] = _examples(fold[prefix + 'examples'], period, created)
        result[fold['id']] = item
    return [result[name] for name in TESTS]


def load_artifact(now=None):
    now = now or datetime.now(timezone.utc)
    root = (Path(settings.BASE_DIR) / 'dashboard' / 'ai_models').resolve()
    try:
        paths = sorted(root.glob('morning-temporal-v1-*.json'), reverse=True)
        if not paths:
            return None
        path = paths[0]
        if path.resolve().parent != root or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        spec, created = model_spec(), _time(value['created_at'])
        if (value['version'] != spec.VERSION or value['entry_allowed'] is not False
                or created > now or value['source_sha256'] != spec.source_hashes()
                or not isinstance(value['protocol_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['protocol_sha256'])
                or value['artifact_sha256'] != digest({k: v for k, v in value.items()
                                                     if k != 'artifact_sha256'})):
            return None
        _folds(value['folds'], created)
        _coverage(value['coverage'], created)
        if any(not _text(value[key], 8000) for key in ('note', 'publication_note')):
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            OverflowError, RecursionError, ImportError):
        return None


def payload(now=None):
    base = {'status': 'unavailable', 'entry_allowed': False, 'notice': NOTICE,
            'message': 'Хронологический отчёт ещё не готов или файл не прошёл проверку.'}
    value = load_artifact(now)
    if value is None:
        return base
    created = _time(value['created_at'])
    return {**base, 'status': 'historical', 'version': value['version'],
            'created_at': value['created_at'], 'folds': _folds(value['folds'], created),
            'coverage': _coverage(value['coverage'], created), 'note': value['note'],
            'publication_note': value['publication_note'],
            'message': 'Сохранённая проверка трёх периодов. Не текущие сигналы покупки.'}
