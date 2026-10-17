"""Freeze three hypotheses before cached replay or bounded public SDK history."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import opening_activity as model
from dashboard.signals import Candle, MSK
from dashboard.method_study import validate_candles
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable

START = datetime(2026, 10, 3, tzinfo=MSK).astimezone(timezone.utc)
END = datetime(2026, 10, 13, tzinfo=MSK).astimezone(timezone.utc)


def hashes():
    root = Path(__file__).resolve().parents[2]
    names = ('opening_activity.py', 'management/commands/study_opening_activity.py', 'signals.py', 'risk.py',
             'method_study.py', 'management/commands/study_pattern_flow.py', 'pattern_flow_study.py')
    return {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names}


def freeze(directory):
    spec = json.loads(json.dumps({'version': model.VERSION, 'rules': model.RULES, 'variants': model.VARIANTS,
            'tickers': model.TICKERS, 'periods': model.PERIODS, 'scenarios': model.SCENARIOS,
            'sources': model.SOURCES, 'source_sha256': hashes(), 'reserved_stocks_not_read': ['TATN','CHMF','PHOR'],
            'extension': {'from':START.isoformat(),'to_exclusive':END.isoformat(),'source':'CANDLE_SOURCE_EXCHANGE'}}))
    path = directory/'opening-activity-protocol-v1.json'
    directory.mkdir(exist_ok=True, parents=True)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != spec:
            raise CommandError('Frozen opening-activity code changed; prices not read. Register a separate version.')
    else:
        now = datetime.now(timezone.utc)
        tomorrow = (now.astimezone(MSK)+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
        write_immutable(path, {'at':now.isoformat(), 'spec':spec,
            'prospective_start':tomorrow.isoformat(), 'prospective_end':(tomorrow+timedelta(days=30)).isoformat(),
            'prospective_note':'Registered window only. No automatic collection, real orders or trading authorization.'})
    return path


def extension(directory, ticker, instrument, fetch):
    path = directory/f'opening-activity-input-{ticker}-20261013.json'
    if not path.exists():
        if not fetch:
            raise CommandError('Additional October history not downloaded; no result fabricated.')
        if datetime.now(timezone.utc) < END:
            raise CommandError('Registered history endpoint not yet completed')
        from dashboard.market_provider import open_client, number, error_message
        from t_tech.invest.schemas import CandleInterval, CandleSource
        records, cursor = {}, START
        try:
            with open_client() as client:
                while cursor < END:
                    until = min(cursor+timedelta(days=7), END)
                    response = client.market_data.get_candles(instrument_id=instrument['uid'],from_=cursor,to=until,
                        interval=CandleInterval.CANDLE_INTERVAL_15_MIN, candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE)
                    for c in response.candles:
                        if not c.is_complete or not cursor <= c.time < until or c.time+timedelta(minutes=15) > END:
                            continue
                        source = getattr(c,'candle_source',getattr(c,'candle_source_type',None))
                        if getattr(source,'name',None) != 'CANDLE_SOURCE_EXCHANGE':
                            raise ValueError('Unconfirmed exchange source')
                        row = {'time':c.time.isoformat(),**{k:number(getattr(c,k)) for k in ('open','high','low','close')},
                               'volume':c.volume,'is_complete':True,'source':source.name}
                        if c.time in records and records[c.time] != row:
                            raise ValueError('Conflicting duplicate quotes')
                        records[c.time] = row
                    cursor = until
        except Exception as exc:
            raise CommandError(error_message(exc)) from None
        if not records:
            raise CommandError('No completed additional exchange candles')
        write_immutable(path, {'version':model.VERSION,'instrument':instrument,'at':datetime.now(timezone.utc).isoformat(),
            'request':{'from':START.isoformat(),'to_exclusive':END.isoformat(),'interval':15,'source':'CANDLE_SOURCE_EXCHANGE'},
            'bars':[records[t] for t in sorted(records)]})
    if path.resolve().parent != directory.resolve() or path.stat().st_size > 4_000_000:
        raise CommandError('Invalid additional input path/size')
    raw = path.read_bytes()
    saved = json.loads(raw)
    if (saved['version'] != model.VERSION or saved['instrument']['uid'] != instrument['uid']
            or saved['instrument']['lot'] != instrument['lot'] or saved['instrument']['ticker'] != ticker
            or saved['request'] != {'from':START.isoformat(),'to_exclusive':END.isoformat(),'interval':15,'source':'CANDLE_SOURCE_EXCHANGE'}):
        raise CommandError('Additional input identity/request mismatch')
    bars = []
    for r in saved['bars']:
        at = datetime.fromisoformat(r['time'])
        if (at.tzinfo is None or not START <= at or at+timedelta(minutes=15)>END or r['is_complete'] is not True
                or r['source'] != 'CANDLE_SOURCE_EXCHANGE' or type(r['volume']) is not int or r['volume'] < 0):
            raise CommandError('Invalid completed additional bar')
        bars.append(Candle(at,*(float(r[k]) for k in ('open','high','low','close')),r['volume']))
    validate_candles(bars)
    return bars, {'file':path.name,'sha256':hashlib.sha256(raw).hexdigest(),'bars':len(bars)}


class Command(BaseCommand):
    help = 'Three fixed opening-activity/retest LONG strategies; cached exchange inputs and optional bounded history, no orders.'

    def add_arguments(self, parser):
        parser.add_argument('--fetch', action='store_true', help='Read only October3–12 historical public candles through T-Bank SDK.')

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory)
        datasets, errors = {}, {}
        log = logging.getLogger('t_tech.invest')
        old_level = log.level
        log.setLevel(logging.CRITICAL)
        try:
            for ticker in model.TICKERS:
                data = acquire(directory,ticker,fetch=False)
                if not data['quality']['exchange_source_verified'] or not data['quality']['exchange_requested']:
                    raise CommandError('Old source not confirmed EXCHANGE')
                extra = None
                try:
                    bars, extra = extension(directory,ticker,data['saved']['instrument'],options['fetch'])
                except (CommandError, ValueError, KeyError, OSError, TypeError):
                    bars = []
                    errors[ticker] = 'Additional historical input unavailable; base archive retained. No October performance claimed for this stock.'
                candles = data['candles']+bars
                validate_candles(candles)
                datasets[ticker] = {'candles':candles,'lot':data['lot'],'source':data['source'],'additional_source':extra,
                                    'instrument':data['saved']['instrument'],'old_bars':len(data['candles'])}
                self.stdout.write(f"INPUT {ticker}: {len(data['candles'])} cached + {len(bars)} additional completed bars")
        finally:
            log.setLevel(old_level)
        scans = {ticker:model.scan(data['candles']) for ticker,data in datasets.items()}
        chosen = model.select_active(datasets,scans)
        results = {}
        for ticker, data in datasets.items():
            results[ticker] = {'source':data['source'],'additional_source':data['additional_source'],
                              'lot':data['lot'],'diagnostics':scans[ticker]['diagnostics'],
                              'variants':{key:{p:{s:model.simulate(data['candles'],scans[ticker]['events'][key],first,last,data['lot'],cost,delay)
                                           for s,(cost,delay) in model.SCENARIOS.items()} for p,(first,last) in model.PERIODS.items()}
                                          for key in model.VARIANTS}}
        summaries = []
        for key,name in model.VARIANTS.items():
            periods = {p:{s:model.aggregate([r['variants'][key][p][s] for r in results.values()]) for s in model.SCENARIOS} for p in model.PERIODS}
            summaries.append({'key':key,'name':name,'periods':periods,'passes_screen':model.passes(periods),'entry_allowed':False})
            self.stdout.write(name+': '+'; '.join(f"{p} {m['base']['return_pct']:+.4f}% ({m['base']['trades']} trades, {m['base']['unverified_trades']} unknown)" if m['base']['return_pct'] is not None else f'{p}: no data' for p,m in periods.items()))
        at = datetime.now(timezone.utc)
        report = {'version':model.VERSION,'at':at.isoformat(),'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),
                  'source_sha256':hashes(),'results':results,'summaries':summaries,'selected_at_1015':chosen,'errors':errors,
                  'simulations':len(results)*len(model.VARIANTS)*len(model.PERIODS)*len(model.SCENARIOS),
                  'rules':model.RULES,'sources':model.SOURCES,'entry_allowed':False,
                  'note':'Seven equal-capital separate accounts, not one executable shared portfolio. October3–12 existed at registration. No independent prospective confirmation.'}
        path = directory/f'opening-activity-study-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,report)
        self.stdout.write(f'Report: {path}; no real orders; retained all 3 hypotheses after 95 previous attempts.')
