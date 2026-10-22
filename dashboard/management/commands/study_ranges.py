"""Register four daily range hypotheses, then load explicit EXCHANGE history."""
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.range_study import VERSION, TICKERS, RESERVES, PERIODS, VARIANTS, RULES, registry, features, simulate, passes
from dashboard.signals import Candle, MSK
from dashboard.strategy_search import validate_bars, aggregate, STRATEGIES, feature_rows, simulate as baseline_simulate
from dashboard.management.commands.study_pattern_flow import write_immutable

START = datetime(2023, 10, 2, tzinfo=timezone.utc)
END = datetime(2026, 10, 3, tzinfo=timezone.utc)
BASELINES = tuple(s for s in STRATEGIES if s.timeframe == 'day' and s.family == 'channel')


def hashes():
    modules = Path(__file__).resolve().parents[2]
    names = ('range_study.py', 'risk.py', 'signals.py', 'strategy_search.py', 'market_provider.py',
             'management/commands/study_ranges.py')
    return {n: hashlib.sha256((modules/n).read_bytes()).hexdigest() for n in names}


def freeze(directory):
    spec = {'version': VERSION, 'registry': registry(), 'rules': RULES, 'periods': PERIODS,
            'tickers': TICKERS, 'reserves_not_read': RESERVES, 'start': START.isoformat(), 'end_exclusive': END.isoformat(),
            'source': 'explicit EXCHANGE DAY, chunks <=365 days', 'entry_allowed': False,
            'paired_baselines': [asdict(s) for s in BASELINES],
            'comparison': 'same inputs and costs; combinations of changes, not isolated trailing effect'}
    signature = json.loads(json.dumps({'spec': spec, 'source_sha256': hashes()}, sort_keys=True))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/'range-swing-protocol-v1.json'
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        if any(old.get(k) != v for k, v in signature.items()):
            raise CommandError('Frozen range rules changed; no prices read; register a new version.')
    else:
        write_immutable(path, {'at': datetime.now(timezone.utc).isoformat(), **signature})
    return path


def load_input(path, ticker):
    if path.stat().st_size > 3_000_000:
        raise ValueError('Oversized input')
    raw = path.read_bytes()
    saved = json.loads(raw)
    if (saved['version'] != VERSION or saved['instrument']['ticker'] != ticker
            or saved['request'] != {'from': START.isoformat(), 'to_exclusive': END.isoformat(), 'source': 'CANDLE_SOURCE_EXCHANGE', 'interval': 'DAY'}):
        raise ValueError('Input mismatch')
    candles = []
    for r in saved['bars']:
        at = datetime.fromisoformat(r['time'])
        if (at.tzinfo is None or not START <= at or at+timedelta(days=1) > END or r['is_complete'] is not True
                or r['source'] != 'CANDLE_SOURCE_EXCHANGE' or type(r['volume']) is not int or r['volume'] < 0):
            raise ValueError('Bad completed EXCHANGE candle')
        candles.append(Candle(at, *(float(r[k]) for k in ('open','high','low','close')), r['volume']))
    validate_bars(candles)
    candles = [c for c in candles if c.time.astimezone(MSK).weekday() < 5]
    if type(saved['instrument']['lot']) is not int or saved['instrument']['lot'] < 1:
        raise ValueError('Invalid lot')
    return {'candles': candles, 'lot': saved['instrument']['lot'], 'quality': bool(len(candles) >= 120),
            'source': {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bars': len(candles)}}


def acquire(directory, ticker, fetch):
    path = directory/f'range-input-{ticker}-20261003.json'
    if path.exists():
        return load_input(path, ticker)
    if not fetch:
        raise CommandError(f'Missing {path.name}; use --fetch for read-only broker history.')
    from dashboard.market_provider import open_client, resolve, number, error_message
    from t_tech.invest.schemas import CandleInterval, CandleSource
    records, cursor = {}, START
    try:
        with open_client() as client:
            instrument = resolve(client, ticker)
            while cursor < END:
                until = min(END, cursor+timedelta(days=365))
                response = client.market_data.get_candles(instrument_id=instrument['uid'], from_=cursor, to=until,
                    interval=CandleInterval.CANDLE_INTERVAL_DAY, candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE)
                for c in response.candles:
                    if not c.is_complete or not START <= c.time or c.time+timedelta(days=1) > END:
                        continue
                    if getattr(c.candle_source, 'name', None) != 'CANDLE_SOURCE_EXCHANGE':
                        raise ValueError('EXCHANGE source not confirmed')
                    r = {'time': c.time.isoformat(), **{k: number(getattr(c,k)) for k in ('open','high','low','close')},
                         'volume': c.volume, 'is_complete': True, 'source': c.candle_source.name}
                    if c.time in records and records[c.time] != r:
                        raise ValueError('Contradictory duplicates')
                    records[c.time] = r
                cursor = until
    except Exception as exc:
        raise CommandError(ticker+': '+error_message(exc)) from None
    saved = {'version': VERSION, 'instrument': instrument, 'at': datetime.now(timezone.utc).isoformat(),
             'request': {'from': START.isoformat(), 'to_exclusive': END.isoformat(), 'source': 'CANDLE_SOURCE_EXCHANGE', 'interval': 'DAY'},
             'bars': [records[t] for t in sorted(records)]}
    if not saved['bars']:
        raise CommandError('Empty history; no input written')
    write_immutable(path, saved)
    return load_input(path, ticker)


class Command(BaseCommand):
    help = 'Four fixed daily range/trailing LONG tests; no real orders or reserve reads.'

    def add_arguments(self, parser):
        parser.add_argument('--fetch', action='store_true')

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory)  # BEFORE fetching or reading any prices.
        log = logging.getLogger('t_tech.invest')
        old_level = log.level
        log.setLevel(logging.CRITICAL)
        results = []
        try:
            for ticker in TICKERS:
                data = acquire(directory, ticker, options['fetch'])
                rows = features(data['candles'])
                result = {'ticker': ticker, 'quality': data['quality'], 'source': data['source'], 'variants': {}, 'baselines': {}}
                for rule in VARIANTS:
                    result['variants'][rule.key] = {name: simulate(data['candles'], rows, rule, first, last, data['lot'])
                                                     for name, (first, last) in PERIODS.items()}
                baseline_rows = feature_rows(data['candles'])
                for baseline in BASELINES:
                    result['baselines'][baseline.key] = {name: baseline_simulate(data['candles'], baseline_rows, baseline, first, last, data['lot'])
                                                         for name, (first, last) in PERIODS.items()}
                results.append(result)
                self.stdout.write(f"{ticker}: {len(data['candles'])} completed weekday EXCHANGE daily bars")
        finally:
            log.setLevel(old_level)
        summaries = []
        for rule in VARIANTS:
            periods = {name: aggregate([r['variants'][rule.key][name] for r in results]) for name in PERIODS}
            summaries.append({'key': rule.key, 'periods': periods,
                              'passes_exploratory_screen': passes(periods, {r['ticker']:r['quality'] for r in results}),
                              'entry_allowed': False})
            self.stdout.write(rule.key+': '+ '; '.join(f"{n} {p['return_pct']:+.3f}% / stress {p['stress_return_pct']:+.3f}% ({p['trades']} trades, {p['unverified_trades']} unverified)" for n,p in periods.items()))
        at = datetime.now(timezone.utc)
        baselines = [{'key': b.key, 'periods': {name: aggregate([r['baselines'][b.key][name] for r in results]) for name in PERIODS}}
                     for b in BASELINES]
        path = directory/f'range-swing-study-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path, {'version': VERSION, 'at': at.isoformat(), 'protocol_sha256': hashlib.sha256(protocol.read_bytes()).hexdigest(),
                               'source_sha256': hashes(), 'results': results, 'summaries': summaries, 'baselines': baselines, 'entry_allowed': False,
                               'note': 'Four further exploratory variants after 69 prior trials. Previously examined periods, not independent holdout. EXCHANGE source verified; no historical book, dividends, tax or guaranteed execution. Current lot size and missing exchange calendar limit validity.'})
        self.stdout.write(str(path))
