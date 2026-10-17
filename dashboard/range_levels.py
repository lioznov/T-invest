"""Descriptive prior 15-minute extrema, isolated from trading decisions."""
from datetime import datetime, timedelta, timezone

from .strategy_search import validate_bars


def range_levels(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    base = {'status': 'insufficient', 'entry_allowed': False, 'timeframe': '15m', 'levels': [], 'evaluated_at': None}
    try:
        bars = [c for c in snapshot.get('candles', []) if c.time+timedelta(minutes=15) <= now]
        validate_bars(bars)
        if len(bars) < 21:
            return base
        end = bars[-1].time+timedelta(minutes=15)
        if not timedelta(0) <= now-end <= timedelta(minutes=17):
            return {**base, 'status': 'stale'}
        latest = bars[-1]
        levels = []
        for n in (20, 55):
            if len(bars) <= n:
                continue
            prior = bars[-n-1:-1]
            if any(c.volume <= 0 for c in prior+[latest]):
                continue
            support, resistance = min(c.low for c in prior), max(c.high for c in prior)
            levels.append({'bars': n, 'support': support, 'resistance': resistance,
                           'close': latest.close, 'above': latest.close > resistance,
                           'below': latest.close < support,
                           'distance_to_high_pct': (resistance/latest.close-1)*100})
        return {**base, 'status': 'ok' if levels else 'insufficient', 'levels': levels, 'evaluated_at': end.isoformat(),
                'source_confirmed': snapshot.get('provider_version') == 'stream_exchange_v1',
                'note': 'Предыдущие 20/55 закрытых свечей по 15 минут, не дневные уровни из теста. Пробой — наблюдение, не команда покупки.'}
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return {**base, 'status': 'invalid'}
