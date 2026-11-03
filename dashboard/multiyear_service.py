"""Read-only public projection of the separate multi-year experiment.

No quote retrieval, model fit or trade dispatch occurs when this module is read.
Daily full-bar observations are not a backtest of buying at a known close.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from .morning_service import TICKER, _count, _report, _text, _time, digest


MAX_ARTIFACT_BYTES = 5_000_000
VARIANTS = {
    'daily_full': ('daily_observational', None, None),
    'control': ('intraday', '17:45', '18:00'),
    'main_hour': ('intraday', '18:00', '18:15'),
    'evening_hour': ('intraday', '22:45', '23:00'),
}
DAILY_NOTICE = ('Только оценка цены, покупка по закрытию не моделировалась. '
                'Полная дневная свеча известна после её завершения; её уже известную '
                'цену закрытия нельзя использовать как цену более ранней покупки.')
NOTICE = ('Отдельное многолетнее исследование на прошлых данных, не текущий торговый сигнал. '
          'Дневная и внутридневные модели имеют разные цели и не сравниваются как одна стратегия. '
          'Реальные заявки, шорт и Telegram не подключены.')


def model_spec():
    from . import multiyear_model
    return multiyear_model


def _variants(value):
    if not isinstance(value, list) or len(value) != len(VARIANTS):
        raise ValueError('Missing fixed model variants')
    projected = {}
    for variant in value:
        if not isinstance(variant, dict):
            raise ValueError('Invalid model variant')
        name = variant['id']
        if name not in VARIANTS or name in projected:
            raise ValueError('Unknown or duplicated model variant')
        kind, cutoff, entry = VARIANTS[name]
        if variant['kind'] != kind or not _text(variant['label'], 250):
            raise ValueError('Invalid model kind or label')
        report = _report(variant['report'])
        item = {'id': name, 'kind': kind, 'label': variant['label'], 'report': report}
        if kind == 'intraday':
            if variant['cutoff'] != cutoff or variant['entry'] != entry:
                raise ValueError('Changed fixed clock')
            item.update(cutoff=cutoff, entry=entry)
        else:
            # Daily metrics may describe a price prediction, never a tradable close fill.
            for period in report['periods'].values():
                if period['paper']['trades'] != 0:
                    raise ValueError('Daily observation cannot prove close-fill trading')
                period.pop('paper', None)
                period.pop('always_buy', None)
            item['notice'] = DAILY_NOTICE
        projected[name] = item
    return [projected[name] for name in VARIANTS]


def _coverage(value, created=None):
    if not isinstance(value, dict) or not value or len(value) > 100:
        raise ValueError('Invalid coverage stocks')
    result = {}
    for ticker, clocks in value.items():
        if not isinstance(ticker, str) or not TICKER.fullmatch(ticker) or not isinstance(clocks, dict):
            raise ValueError('Invalid coverage stock')
        item = {}
        for name in ('day', 'intraday'):
            window = clocks[name]
            if (not isinstance(window, dict) or not _count(window['bars'])
                    or window['bars'] > 2**53 - 1):
                raise ValueError('Invalid coverage count')
            begin, end = _time(window['requested_from']), _time(window['to'])
            if begin >= end:
                raise ValueError('Invalid requested coverage range')
            first, last = window['first_actual'], window['last_actual']
            if window['bars'] == 0:
                if first is not None or last is not None:
                    raise ValueError('Absent observations cannot have actual dates')
            else:
                first_at, last_at = _time(first), _time(last)
                if not begin <= first_at <= last_at < end:
                    raise ValueError('Actual coverage outside requested window')
                if window['bars'] == 1 and first_at != last_at:
                    raise ValueError('Single candle cannot have two dates')
                if window['bars'] > 1 and first_at == last_at:
                    raise ValueError('Multiple candles must have distinct times')
                if created is not None and last_at > created:
                    raise ValueError('Actual observations cannot come from the future')
            item[name] = {key: window[key] for key in
                          ('bars', 'first_actual', 'last_actual', 'requested_from', 'to')}
        result[ticker] = item
    return result


def load_artifact(now=None):
    """Only the newest immutable file is eligible; never silently fall back."""
    now = now or datetime.now(timezone.utc)
    root = (Path(settings.BASE_DIR) / 'dashboard' / 'ai_models').resolve()
    try:
        paths = sorted(root.glob('multiyear-morning-v1-*.json'), reverse=True)
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
        created = _time(artifact['created_at'])
        if (artifact['version'] != spec.VERSION or artifact['entry_allowed'] is not False
                or artifact['source_sha256'] != spec.source_hashes()
                or artifact['artifact_sha256'] != digest({k: v for k, v in artifact.items()
                                                        if k != 'artifact_sha256'})
                or created > now):
            return None
        _variants(artifact['variants'])
        _coverage(artifact['coverage'], created)
        for name in ('note', 'publication_note'):
            if not _text(artifact[name], 8000):
                return None
        return artifact
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            OverflowError, RecursionError, ImportError):
        return None


def payload(now=None):
    base = {'status': 'unavailable', 'entry_allowed': False, 'notice': NOTICE,
            'message': 'Многолетний отчёт ещё не готов или его файл не прошёл проверку.'}
    artifact = load_artifact(now)
    if artifact is None:
        return base
    return {**base, 'status': 'historical', 'version': artifact['version'],
            'created_at': artifact['created_at'], 'variants': _variants(artifact['variants']),
            'coverage': _coverage(artifact['coverage'], _time(artifact['created_at'])), 'note': artifact['note'],
            'publication_note': artifact['publication_note'],
            'message': 'Сохранённая многолетняя проверка. Новые прогнозы, сделки и уведомления здесь не создаются.'}
