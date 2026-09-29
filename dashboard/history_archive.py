"""Reuse dated broker candle datasets saved by the local research command."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings

from .signals import Candle


def load_long_history(ticker, now=None):
    now = now or datetime.now(timezone.utc)
    directory = Path(settings.BASE_DIR) / 'data'
    best = None
    for path in directory.glob('research-*.json'):
        try:
            if path.stat().st_size > 100_000_000:
                continue
            saved = json.loads(path.read_text(encoding='utf-8'))
            for row in saved.get('results', []):
                if row.get('ticker') != ticker or not row.get('candles'):
                    continue
                candles = {}
                for raw in row['candles']:
                    at = datetime.fromisoformat(raw['time'])
                    prices = [float(raw[key]) for key in ('open', 'high', 'low', 'close')]
                    volume = int(raw['volume'])
                    if not at.tzinfo or not all(math.isfinite(p) and p > 0 for p in prices) or volume < 0:
                        raise ValueError('Invalid candle')
                    if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                        raise ValueError('Invalid OHLC range')
                    if at + timedelta(minutes=15) <= now:
                        candles[at] = Candle(at, *prices, volume)
                ordered = [candles[at] for at in sorted(candles)]
                if not ordered:
                    continue
                rank = (len(ordered), ordered[-1].time)
                if best is None or rank > best['rank']:
                    best = {'candles': ordered, 'rank': rank, 'saved_at': saved.get('at')}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            # A partial/corrupted report must not prevent reading other valid datasets.
            continue
    return best
