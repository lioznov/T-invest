"""Read-only, fail-closed delivery of the separate next-morning experiment.

This module never fetches market data, fits a model, or dispatches a trade/alert.
An old forecast remains an explicitly historical observation, not a new signal.
"""
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings


MAX_ARTIFACT_BYTES = 3_000_000
MSK = ZoneInfo('Europe/Moscow')
TICKER = re.compile(r'[A-Z0-9][A-Z0-9.\-]{0,14}\Z')
NOTICE = ('Это отдельный исследовательский прогноз, не приказ на покупку или продажу. '
          'Доходность не доказана; реальные сделки и Telegram-сигналы не подключены.')


def model_spec():
    # A status request must not import training commands or a broker client.
    from . import morning_model
    return morning_model


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def _count(value):
    return type(value) is int and value >= 0


def _text(value, limit=2000):
    return isinstance(value, str) and len(value) <= limit


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Invalid timestamp')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Naive timestamp')
    return parsed.astimezone(timezone.utc)


def _report(value):
    """Explicit public projection; model internals and unexpected keys stay local."""
    if not isinstance(value, dict):
        raise ValueError('Invalid report')
    result = {}
    for name in ('trained_rows', 'calibration_rows'):
        if not _count(value[name]):
            raise ValueError('Invalid report count')
        result[name] = value[name]
    for name in ('total_rows', 'excluded_rows', 'purged_rows', 'features_count'):
        if name in value:
            if not _count(value[name]):
                raise ValueError('Invalid report count')
            result[name] = value[name]
    if not isinstance(value['periods'], dict) or set(value['periods']) != {'test', 'later'}:
        raise ValueError('Missing independent periods')
    result['periods'] = {}
    for name in ('test', 'later'):
        period = value['periods'][name]
        if not isinstance(period, dict):
            raise ValueError('Invalid period')
        item = {}
        if 'start' in period or 'end_exclusive' in period:
            for key in ('start', 'end_exclusive'):
                if (not isinstance(period.get(key), str)
                        or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', period[key])):
                    raise ValueError('Invalid period date')
                item[key] = date.fromisoformat(period[key]).isoformat()
            if item['start'] >= item['end_exclusive']:
                raise ValueError('Invalid period date range')
        for key in ('samples', 'days'):
            if not _count(period[key]):
                raise ValueError('Invalid period count')
            item[key] = period[key]
        for key in ('mae_bps', 'baseline_mae_bps', 'direction_accuracy_pct', 'coverage_pct'):
            metric = period[key]
            if metric is not None and not _finite(metric):
                raise ValueError('Invalid metric')
            if metric is not None and (metric < 0 or key.endswith('_pct') and metric > 100):
                raise ValueError('Invalid metric range')
            item[key] = metric
        if (item['days'] > item['samples'] or bool(item['days']) != bool(item['samples'])
                or item['samples'] == 0 and any(item[k] is not None for k in (
                    'mae_bps', 'baseline_mae_bps', 'direction_accuracy_pct', 'coverage_pct'))):
            raise ValueError('Inconsistent sample coverage')
        for key in ('unknown_targets', 'eligible_forecasts', 'direction_nonzero_samples'):
            if key in period:
                if not _count(period[key]):
                    raise ValueError('Invalid period count')
                item[key] = period[key]
        if ('eligible_forecasts' in item and 'unknown_targets' in item
                and item['samples'] + item['unknown_targets'] != item['eligible_forecasts']
                or item.get('direction_nonzero_samples', 0) > item['samples']):
            raise ValueError('Inconsistent period count')
        for key in ('mean_baseline_mae_bps', 'interval_width_bps', 'abstention_pct'):
            if key not in period:
                continue
            metric = period[key]
            if metric is not None and not _finite(metric):
                raise ValueError('Invalid extra period metric')
            if metric is not None and (metric < 0 or key == 'abstention_pct' and metric > 100):
                raise ValueError('Invalid extra period metric range')
            item[key] = metric
        item['paper'] = _paper(period['paper'])
        if 'always_buy' in period:
            item['always_buy'] = _paper(period['always_buy'])
        if 'note' in period:
            if not _text(period['note']):
                raise ValueError('Invalid period note')
            item['note'] = period['note']
        result['periods'][name] = item
    if 'note' in value:
        if not _text(value['note']):
            raise ValueError('Invalid report note')
        result['note'] = value['note']
    return result


def _paper(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid paper result')
    result = {}
    for key in ('trades', 'unknown_trades'):
        if not _count(value[key]):
            raise ValueError('Invalid paper count')
        result[key] = value[key]
    for key in ('selected_candidates', 'stress_trades', 'stress_unknown_trades'):
        if key in value:
            if not _count(value[key]):
                raise ValueError('Invalid paper count')
            result[key] = value[key]
    for key in ('net_return_pct', 'max_drawdown_pct', 'stress_return_pct',
                'mean_anchor_to_entry_pct', 'exposure_pct'):
        if key not in value and key not in ('net_return_pct', 'max_drawdown_pct'):
            continue
        metric = value[key]
        if metric is not None and not _finite(metric):
            raise ValueError('Invalid paper metric')
        result[key] = metric
    if 'status' in value:
        if value['status'] not in ('ok', 'incomplete'):
            raise ValueError('Invalid paper status')
        if value['status'] == 'ok' and value['unknown_trades'] > 0:
            raise ValueError('Unknown trades cannot produce a complete account curve')
        if value['status'] == 'incomplete' and (value['net_return_pct'] is not None
                                               or value['max_drawdown_pct'] is not None):
            raise ValueError('Unknown trades cannot prove full returns')
        result['status'] = value['status']
    if (value['unknown_trades'] > 0 or value['trades'] == 0) and (
            value['net_return_pct'] is not None or value['max_drawdown_pct'] is not None):
        raise ValueError('Unknown or absent trades cannot prove returns or drawdown')
    if value.get('stress_unknown_trades', 0) > 0 and value.get('stress_return_pct') is not None:
        raise ValueError('Unknown stress trades cannot prove full returns')
    if value.get('stress_trades') == 0 and value.get('stress_return_pct') is not None:
        raise ValueError('Absent stress trades cannot prove full returns')
    return result


def _forecast(value):
    if not isinstance(value, dict) or type(value['historical']) is not bool:
        raise ValueError('Invalid forecast')
    as_of, target = _time(value['as_of']), _time(value['target_at'])
    if as_of >= target:
        raise ValueError('Invalid horizon')
    result = {key: value[key] for key in ('as_of', 'target_at', 'historical')}
    for key in ('anchor_price', 'predicted_price', 'lower_price', 'upper_price'):
        if not _finite(value[key]) or value[key] <= 0:
            raise ValueError('Invalid price')
        result[key] = value[key]
    if not value['lower_price'] <= value['predicted_price'] <= value['upper_price']:
        raise ValueError('Unordered forecast interval')
    change = value['predicted_change_pct']
    expected = (value['predicted_price'] / value['anchor_price'] - 1) * 100
    if not _finite(change) or not isfinite(expected) or abs(expected - change) > 0.01:
        raise ValueError('Inconsistent forecast change')
    direction = value['direction']
    if direction not in ('up', 'down', 'uncertain'):
        raise ValueError('Invalid direction')
    expected_direction = ('up' if value['lower_price'] > value['anchor_price'] else
                          'down' if value['upper_price'] < value['anchor_price'] else 'uncertain')
    if direction != expected_direction:
        raise ValueError('Inconsistent direction')
    result.update(predicted_change_pct=change, direction=direction)
    if value.get('actual_price') is not None:
        if not value['historical'] or not _finite(value['actual_price']) or value['actual_price'] <= 0:
            raise ValueError('Invalid historical outcome')
        result['actual_price'] = value['actual_price']
    if 'note' in value:
        if not _text(value['note']):
            raise ValueError('Invalid forecast note')
        result['note'] = value['note']
    return result


def load_artifact(now=None):
    """Only the latest immutable artifact is eligible; never silently fall back."""
    now = now or datetime.now(timezone.utc)
    root = (Path(settings.BASE_DIR) / 'dashboard' / 'ai_models').resolve()
    try:
        paths = sorted(root.glob('morning-ridge-v1-*.json'), reverse=True)
        if not paths:
            return None
        path = paths[0]
        if path.resolve().parent != root or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            return None
        artifact = json.loads(raw)
        if not isinstance(artifact, dict):
            return None
        spec = model_spec()
        if (artifact['version'] != spec.VERSION or artifact['entry_allowed'] is not False
                or artifact['features'] != list(spec.FEATURES)
                or artifact['rules'] != json.loads(json.dumps(spec.RULES))
                or artifact['source_sha256'] != spec.source_hashes()
                or artifact['artifact_sha256'] != digest({k: v for k, v in artifact.items()
                                                        if k != 'artifact_sha256'})):
            return None
        trained = _time(artifact['trained_at'])
        if trained > now:
            return None
        tickers = artifact['tickers']
        if (not isinstance(tickers, list) or not tickers or len(tickers) > 1000
                or any(not isinstance(t, str) or not TICKER.fullmatch(t) for t in tickers)
                or len(tickers) != len(set(tickers))):
            return None
        ridge, n = artifact['model'], len(spec.FEATURES)
        if not isinstance(ridge, dict):
            return None
        for key, length in (('center', n), ('scale', n), ('weights', n + 1)):
            if (not isinstance(ridge[key], list) or len(ridge[key]) != length
                    or not all(_finite(v) for v in ridge[key])):
                return None
        if (any(v <= 0 for v in ridge['scale']) or not _finite(ridge['interval_abs_pct'])
                or ridge['interval_abs_pct'] < 0
                or 'train_mean_pct' in ridge and not _finite(ridge['train_mean_pct'])):
            return None
        _report(artifact['report'])
        forecasts = artifact['forecasts']
        if not isinstance(forecasts, dict) or any(t not in tickers for t in forecasts):
            return None
        for value in forecasts.values():
            _forecast(value)
            if (_time(value['as_of']) > now or value.get('actual_price') is not None
                    and _time(value['target_at']) + timedelta(minutes=15) > now):
                return None
        if 'note' in artifact and not _text(artifact['note']):
            return None
        return artifact
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError, ImportError):
        return None


def load_forecast_reference(artifact, now):
    """A separately generated forecast is bound to this exact trained artifact.

    Return (exists, forecasts). An invalid latest reference is deliberately not
    replaced with an older reference or a historical price from the model file.
    """
    root = (Path(settings.BASE_DIR) / 'data' / 'morning').resolve()
    try:
        paths = sorted(root.glob('forecast-*.json'), reverse=True)
        if not paths:
            return False, None
        path = paths[0]
        if path.resolve().parent != root or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return True, None
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            return True, None
        reference = json.loads(raw)
        if (not isinstance(reference, dict) or reference['version'] != artifact['version']
                or reference['entry_allowed'] is not False
                or reference['model_artifact_sha256'] != artifact['artifact_sha256']
                or reference['source_sha256'] != artifact['source_sha256']
                or reference['artifact_sha256'] != digest({k: v for k, v in reference.items()
                                                         if k != 'artifact_sha256'})):
            return True, None
        created = _time(reference['created_at'])
        if created > now or created < _time(artifact['trained_at']):
            return True, None
        forecasts = reference['forecasts']
        if (not isinstance(forecasts, dict) or not forecasts
                or any(t not in artifact['tickers'] for t in forecasts)):
            return True, None
        for value in forecasts.values():
            _forecast(value)
            if (value['historical'] is not False or value.get('actual_price') is not None
                    or not _time(value['as_of']) <= created < _time(value['target_at'])):
                return True, None
        return True, forecasts
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        return True, None


def _current(value, now):
    if value['historical']:
        return False
    as_of, target = _time(value['as_of']), _time(value['target_at'])
    local = as_of.astimezone(MSK)
    expected = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    # The experiment intentionally excludes Fridays/weekends and unknown multi-day gaps.
    return (local.weekday() < 4 and (local.hour, local.minute) == (17, 45)
            and local.second == 0 and local.microsecond == 0
            and target == expected.astimezone(timezone.utc)
            and 0 <= (now - as_of).total_seconds() < 15 * 60 and now < target)


def forecast_payload(ticker='SBER', mode='live', now=None):
    now = now or datetime.now(timezone.utc)
    base = {'status': 'unavailable', 'entry_allowed': False, 'ticker': ticker, 'note': NOTICE,
            'message': 'Отдельная утренняя модель ещё не обучена или файл не прошёл проверку.'}
    artifact = load_artifact(now)
    if artifact is None:
        if mode == 'demo':
            return {**base, 'status': 'demo', 'message': 'Учебные цены не используются для прогноза следующего утра.'}
        return base
    base.update(version=artifact['version'], trained_at=artifact['trained_at'], tickers=list(artifact['tickers']),
                report=_report(artifact['report']), rules=artifact['rules'])
    if mode == 'demo':
        return {**base, 'status': 'demo',
                'message': 'Это учебный режим. Ниже — результаты реальной истории; прогноз учебной цены не рассчитывается.'}
    if ticker not in artifact['tickers']:
        return {**base, 'message': 'Эта акция не входила в обучение утренней модели. Прогноз не подставляется.'}
    reference_exists, forecasts = load_forecast_reference(artifact, now)
    if reference_exists and forecasts is None:
        return {**base, 'message': 'Последний файл прогноза не прошёл проверку; более старый прогноз вместо него не подставляется.'}
    value = (forecasts if reference_exists else artifact['forecasts']).get(ticker)
    if value is None:
        return {**base, 'message': 'Для этой акции ещё нет сохранённого утреннего прогноза.'}
    forecast = _forecast(value)
    if not _current(forecast, now):
        forecast['historical'] = True
        return {**base, 'status': 'historical', 'forecast': forecast,
                'message': 'Сохранённый прогноз показан только как история, не как текущий сигнал на покупку или продажу.'}
    return {**base, 'status': 'ok', 'forecast': forecast,
            'message': 'Свежий исследовательский прогноз до следующего утра. Покупки и продажи автоматически не выполняются.'}
