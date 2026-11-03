"""Immutable, resumable EXCHANGE history. No orders, bots or training methods.

`root` is the dedicated data/multiyear directory, not the repository root.
DAY buckets are not exact opening/closing auction timestamps. Current lot/UID
metadata does not establish historical corporate-action or execution validity.
"""
import hashlib
import json
import math
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management.base import CommandError

from . import morning_model as m
from .market_provider import open_client, resolve, number
from .signals import Candle


VERSION = 'multiyear_exchange_v1'
END = datetime(2026, 10, 23, tzinfo=timezone.utc)
INTERVALS = {'day': {'chunk_days': 1825, 'step_minutes': 1440,
                     'first_field': 'first_1day_candle_date'},
             '15m': {'chunk_days': 21, 'step_minutes': 15,
                     'first_field': 'first_1min_candle_date'}}
SOURCE = 'CANDLE_SOURCE_EXCHANGE'
MAX_BYTES = 4_000_000


class RateLimiter:
    """One process-wide gap across all stock threads, including metadata calls."""
    def __init__(self, gap=.55):
        self.gap = gap
        self.lock = threading.Lock()
        self.last = None

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.last is not None:
                delay = self.gap-(now-self.last)
                if delay > 0:
                    time.sleep(delay)
            self.last = time.monotonic()


RATE_LIMITER = RateLimiter()
RETRYABLE_CODES = frozenset(('UNAVAILABLE', 'DEADLINE_EXCEEDED', 'RESOURCE_EXHAUSTED'))


def _rpc(call, limiter):
    """Three bounded attempts for transient RPC failures, never validation/auth."""
    for attempt in range(3):
        limiter.wait()
        try:
            return call()
        except Exception as exc:
            code = getattr(exc, 'code', None)
            code = code() if callable(code) else code
            name = getattr(code, 'name', None)
            if name not in RETRYABLE_CODES or attempt == 2:
                raise
            time.sleep(2*(attempt+1))


def digest(value):
    return m.digest(value)


def _date(value):
    if not isinstance(value, str):
        raise ValueError('History timestamp must be ISO text')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('History timestamp must be timezone-aware')
    return result.astimezone(timezone.utc)


def _stock(root, ticker):
    if ticker not in m.TICKERS:
        raise ValueError('Unsupported history ticker')
    return Path(root)/ticker


def _signed(value):
    return {**value, 'artifact_sha256': digest(value)}


def _immutable(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _read(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Oversized history cache')
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('History cache must be an object')
    expected = data.get('artifact_sha256')
    plain = {key: value for key, value in data.items() if key != 'artifact_sha256'}
    if type(expected) is not str or expected != digest(plain):
        raise ValueError('History cache digest mismatch; refusing to overwrite')
    return data, hashlib.sha256(raw).hexdigest()


def _bounds(first, interval):
    start = first.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return {'from': start.isoformat(), 'to_exclusive': END.isoformat(),
            'chunk_days': INTERVALS[interval]['chunk_days']}


def validate_metadata(data, ticker):
    instrument = data['instrument']
    if (data['version'] != VERSION or data.get('entry_allowed') is not False
            or instrument['ticker'] != ticker or not isinstance(instrument['uid'], str)
            or not instrument['uid'] or type(instrument['lot']) is not int or instrument['lot'] <= 0
            or instrument.get('currency') != 'rub' or instrument.get('class_code') != 'TQBR'):
        raise ValueError('History instrument metadata mismatch')
    for interval, spec in INTERVALS.items():
        first = _date(data[spec['first_field']])
        if not datetime(1970, 1, 1, tzinfo=timezone.utc) < first < END:
            raise ValueError('First history date unavailable or outside fixed end')
        if data['bounds'][interval] != _bounds(first, interval):
            raise ValueError('History bounds changed')
    _date(data['created_at'])
    return data


def load_metadata(root, ticker):
    path = _stock(root, ticker)/'metadata.json'
    if not path.exists():
        raise CommandError(f'{ticker}: отсутствует metadata.json; загрузка требует --fetch.')
    data, _ = _read(path)
    return validate_metadata(data, ticker)


def windows(metadata, interval):
    if interval not in INTERVALS:
        raise ValueError('Unsupported history interval')
    bounds = metadata['bounds'][interval]
    cursor, end = _date(bounds['from']), _date(bounds['to_exclusive'])
    while cursor < end:
        until = min(cursor+timedelta(days=INTERVALS[interval]['chunk_days']), end)
        yield cursor, until
        cursor = until


def chunk_name(interval, first, until):
    return f'{interval}-{first.strftime("%Y%m%d")}-{until.strftime("%Y%m%d")}.json'


def _candles(records, first, until, interval):
    if not isinstance(records, list) or len(records) > 2400:
        raise ValueError('Invalid or unexpectedly large history response')
    step = timedelta(minutes=INTERVALS[interval]['step_minutes'])
    previous, candles = None, []
    for record in records:
        at = _date(record['time'])
        values = [record[key] for key in ('open', 'high', 'low', 'close')]
        if (not first <= at < until or at+step > until or record['source'] != SOURCE
                or record['is_complete'] is not True or at.second or at.microsecond
                or at.minute % 15 or interval == 'day' and (at.hour or at.minute)
                or previous is not None and at <= previous
                or not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in values)
                or type(record['volume']) is not int or record['volume'] < 0
                or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]):
            raise ValueError('Invalid completed EXCHANGE history candle')
        candles.append(Candle(at, *values, record['volume']))
        previous = at
    return candles


def load_chunk(path, metadata, interval, first, until):
    data, sha = _read(path)
    if (data.get('version') != VERSION or data.get('entry_allowed') is not False
            or data.get('ticker') != metadata['instrument']['ticker']
            or data.get('uid') != metadata['instrument']['uid']
            or data.get('metadata_sha256') != metadata['artifact_sha256']
            or data.get('request') != {'from': first.isoformat(), 'to_exclusive': until.isoformat(),
                                       'interval': interval, 'source': SOURCE}):
        raise ValueError('History chunk provenance mismatch')
    _date(data['created_at'])
    bars = _candles(data['bars'], first, until, interval)
    return bars, {'file': f'{metadata["instrument"]["ticker"]}/{path.name}', 'sha256': sha,
                  'bars': len(bars), 'empty': not bars}


def load_history(root, ticker, interval):
    """Require every expected immutable chunk; no network and no fallback source."""
    metadata = load_metadata(root, ticker)
    candles, sources, seen = [], [], {}
    for first, until in windows(metadata, interval):
        path = _stock(root, ticker)/chunk_name(interval, first, until)
        if not path.exists():
            raise CommandError(f'{ticker}: отсутствует {path.name}; нужны все блоки, повтори --fetch.')
        bars, source = load_chunk(path, metadata, interval, first, until)
        sources.append(source)
        for bar in bars:
            if bar.time in seen and seen[bar.time] != bar:
                raise ValueError('Conflicting history overlap')
            if bar.time not in seen:
                seen[bar.time] = bar
                candles.append(bar)
    if any(b.time <= a.time for a, b in zip(candles, candles[1:])):
        raise ValueError('Non-monotonic history across chunks')
    return {'instrument': metadata['instrument'], 'candles': candles, 'sources': sources,
            'metadata': metadata, 'coverage': {'requested_from': metadata['bounds'][interval]['from'],
                'to': END.isoformat(), 'first_actual': candles[0].time.isoformat() if candles else None,
                'last_actual': candles[-1].time.isoformat() if candles else None, 'bars': len(candles),
                'chunks': len(sources), 'empty_chunks': sum(source['empty'] for source in sources)}}


def _fetch_metadata(client, ticker, limiter):
    from t_tech.invest.schemas import InstrumentIdType
    instrument = _rpc(lambda: resolve(client, ticker), limiter)
    share = _rpc(lambda: client.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                                                    id=instrument['uid']), limiter).instrument
    if (share.uid != instrument['uid'] or share.ticker != ticker or share.class_code != 'TQBR'
            or share.lot != instrument['lot'] or share.currency != 'rub'):
        raise ValueError('Resolved share UID/class/lot mismatch')
    instrument = {**instrument, 'class_code': share.class_code, 'figi': share.figi, 'isin': share.isin}
    first_day, first_minute = share.first_1day_candle_date, share.first_1min_candle_date
    if any(not isinstance(v, datetime) or v.tzinfo is None for v in (first_day, first_minute)):
        raise ValueError('First history timestamps unavailable')
    value = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
             'instrument': instrument, 'first_1day_candle_date': first_day.isoformat(),
             'first_1min_candle_date': first_minute.isoformat(),
             'bounds': {'day': _bounds(first_day, 'day'), '15m': _bounds(first_minute, '15m')},
             'entry_allowed': False,
             'note': 'Current UID/lot, not historical identity/lot or corporate-action verification. '
                     'Daily buckets cover complete provider trading days, not exact auction timestamps.'}
    signed = _signed(value)
    validate_metadata(signed, ticker)
    return signed


def _fetch_chunk(client, metadata, interval, first, until, limiter):
    from t_tech.invest.schemas import CandleInterval, CandleSource
    interval_enum = CandleInterval.CANDLE_INTERVAL_DAY if interval == 'day' else CandleInterval.CANDLE_INTERVAL_15_MIN
    # Do not pass `limit`: its combination with candle_source_type is invalid.
    response = _rpc(lambda: client.market_data.get_candles(instrument_id=metadata['instrument']['uid'],
        from_=first, to=until, interval=interval_enum,
        candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE), limiter)
    records = {}
    for candle in response.candles:
        if not isinstance(candle.time, datetime) or candle.time.tzinfo is None:
            raise ValueError('Naive broker candle time')
        at = candle.time.astimezone(timezone.utc)
        if getattr(candle.candle_source, 'name', None) != SOURCE:
            raise ValueError('EXCHANGE candle source not confirmed')
        # Broker `to` may be inclusive. Only its exact boundary is harmless.
        if at == until:
            continue
        record = {'time': at.isoformat(), **{key: number(getattr(candle, key))
                                           for key in ('open', 'high', 'low', 'close')},
                  'volume': candle.volume, 'is_complete': candle.is_complete, 'source': SOURCE}
        if at in records and records[at] != record:
            raise ValueError('Conflicting broker duplicate')
        records[at] = record
    bars = [records[at] for at in sorted(records)]
    _candles(bars, first, until, interval)
    return _signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'ticker': metadata['instrument']['ticker'], 'uid': metadata['instrument']['uid'],
                    'metadata_sha256': metadata['artifact_sha256'], 'entry_allowed': False,
                    'request': {'from': first.isoformat(), 'to_exclusive': until.isoformat(),
                                'interval': interval, 'source': SOURCE}, 'bars': bars})


def collect_stock(root, ticker, intervals=('day', '15m'), *, fetch=False,
                  progress=None, limiter=RATE_LIMITER):
    """Resume immutable blocks. A client is opened lazily only for missing data."""
    stock = _stock(root, ticker)
    if any(interval not in INTERVALS for interval in intervals):
        raise ValueError('Unsupported history interval')
    with ExitStack() as stack:
        client = None

        def broker():
            nonlocal client
            if not fetch:
                raise CommandError('Read-only cache validation never downloads; use --fetch explicitly.')
            if client is None:
                client = stack.enter_context(open_client())
            return client

        metadata_path = stock/'metadata.json'
        if metadata_path.exists():
            metadata = load_metadata(root, ticker)
        else:
            metadata = _fetch_metadata(broker(), ticker, limiter)
            _immutable(metadata_path, metadata)
        summary = {'metadata': metadata, 'intervals': {}}
        for interval in intervals:
            expected = list(windows(metadata, interval))
            count, empty = 0, 0
            for index, (first, until) in enumerate(expected, 1):
                path = stock/chunk_name(interval, first, until)
                if not path.exists():
                    value = _fetch_chunk(broker(), metadata, interval, first, until, limiter)
                    _immutable(path, value)
                bars, source = load_chunk(path, metadata, interval, first, until)
                count += len(bars)
                empty += source['empty']
                if progress and (index % 10 == 0 or index == len(expected)):
                    progress(f'{ticker} {interval}: блоки {index}/{len(expected)}, свечей {count}, пустых блоков {empty}.')
            summary['intervals'][interval] = {'chunks': len(expected), 'bars': count, 'empty_chunks': empty}
        return summary
