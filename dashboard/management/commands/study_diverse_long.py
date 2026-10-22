"""Freeze 24 hypotheses, lock a single selection, then fetch an unseen period."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import diverse_research as research, portfolio_replay
from dashboard.signals import Candle, MSK
from dashboard.method_study import validate_candles
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable
from dashboard.management.commands.study_opening_activity import extension
from dashboard.management.commands.verify_neural_trader import load_extension
from dashboard.management.commands.train_market_ai import digest


def hashes():
    root = Path(__file__).resolve().parents[2]
    names = ('diverse_research.py', 'portfolio_replay.py', 'signals.py', 'risk.py', 'method_study.py',
             'market_stream_provider.py', 'market_provider.py', 'strategy_search.py',
             'pattern_flow_study.py', 'management/commands/study_pattern_flow.py',
             'management/commands/study_opening_activity.py',
             'management/commands/verify_neural_trader.py', 'management/commands/study_diverse_long.py')
    return {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names}


def lock(path, spec):
    normalized = json.loads(json.dumps(spec, sort_keys=True, allow_nan=False))
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != normalized:
            raise CommandError(f'Immutable experiment mismatch: {path.name}; original retained')
    else:
        write_immutable(path, {'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': normalized})
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fresh_input(directory, ticker, instrument, fetch):
    start, end = (datetime.fromisoformat(s).replace(tzinfo=MSK).astimezone(timezone.utc) for s in research.FRESH)
    request = {'from': start.isoformat(), 'to_exclusive': end.isoformat(), 'minutes': 15,
               'source': 'CANDLE_SOURCE_EXCHANGE'}
    path = directory/f'diverse-input-{ticker}-20261021.json'
    if not path.exists():
        if not fetch:
            raise CommandError('Fresh check not cached. --fetch permits read-only candle requests; no orders.')
        if datetime.now(timezone.utc) < end:
            raise CommandError('Fresh check has not completed yet')
        from dashboard.market_provider import open_client, error_message
        from dashboard.market_stream_provider import exchange_candles
        from t_tech.invest.schemas import CandleInterval
        try:
            with open_client() as client:
                candles = exchange_candles(client, instrument['uid'], start, end,
                                          CandleInterval.CANDLE_INTERVAL_15_MIN, 15)
        except Exception as exc:
            raise CommandError(error_message(exc)) from None
        if not candles:
            raise CommandError('No verified exchange candles; result not fabricated')
        write_immutable(path, {'request': request, 'instrument': instrument,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'candles': [{'at': c.time.isoformat(), **{k: getattr(c,k) for k in ('open','high','low','close','volume')}} for c in candles]})
    if path.resolve().parent != directory.resolve() or path.stat().st_size > 4_000_000:
        raise CommandError('Invalid fresh input path or size')
    raw = path.read_bytes()
    saved = json.loads(raw)
    if saved['instrument'] != instrument or saved['request'] != request:
        raise CommandError('Fresh input identity mismatch')
    candles = [Candle(datetime.fromisoformat(r['at']), *(r[k] for k in ('open','high','low','close','volume')))
               for r in saved['candles']]
    validate_candles(candles)
    if not candles or any(not start <= c.time or c.time+timedelta(minutes=15) > end for c in candles):
        raise CommandError('Empty or out-of-bounds fresh input')
    return candles, {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bars': len(candles)}


def universe(histories):
    result = {t: (v['candles'], research.feature_rows(v['candles'])) for t,v in histories.items()}
    research.add_peer_context(result)
    return result


def evaluate(histories, context, variant, bounds):
    rows = []
    for ticker, data in histories.items():
        candles, features = context[ticker]
        rows.extend(r for r in research.replay_rows(candles, features, ticker, data['lot'], variant)
                    if research.in_period(r, bounds))
    return {name: portfolio_replay.simulate(rows, m)
            for name,m in (('zero_cost',0), ('base',1), ('double_cost',2))}


class Command(BaseCommand):
    help = '24 fixed LONG policies: shared account, cost stress, immutable single-candidate later-date audit.'

    def add_arguments(self, parser):
        parser.add_argument('--fetch', action='store_true')
        parser.add_argument('--discovery-only', action='store_true')

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/'data'
        directory.mkdir(exist_ok=True, parents=True)
        spec = {'version': research.VERSION, 'rules': research.RULES, 'variants': research.VARIANTS,
                'old_periods': research.PERIODS, 'fresh': research.FRESH, 'tickers': research.TICKERS,
                'portfolio': portfolio_replay.RULES, 'source_sha256': hashes(),
                'sources': ['https://www.nber.org/papers/w30917',
                            'https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum'],
                'source_scope': 'economic hypotheses only, not reproduction of published horizons/markets/returns'}
        protocol_sha = lock(directory/'diverse-long-protocol-v1.json', spec)
        histories, inputs = {}, {}
        for ticker in research.TICKERS:
            old = acquire(directory, ticker, fetch=False)
            if not old['quality']['exchange_source_verified'] or not old['quality']['exchange_requested']:
                raise CommandError('Unverified historical input')
            extra, extra_source = extension(directory, ticker, old['saved']['instrument'], False)
            later, later_source = load_extension(directory, ticker, old['saved']['instrument'], False)
            histories[ticker] = {'candles': old['candles']+extra+later, 'lot': old['lot'],
                                 'instrument': old['saved']['instrument']}
            inputs[ticker] = {'context': old['source'], 'october': extra_source, 'later': later_source}
            self.stdout.write(f'INPUT {ticker}: {len(histories[ticker]["candles"])} bars')
        context = universe(histories)
        results = {}
        for variant in research.VARIANTS:
            results[variant] = {p: evaluate(histories, context, variant, bounds) for p,bounds in research.PERIODS.items()}
            self.stdout.write(f'OLD {variant}: '+', '.join(f'{p}={r["base"]["return_pct"]}' for p,r in results[variant].items()))
        selection = research.choose(results)
        frozen_selection = {'protocol_sha256': protocol_sha, 'old_inputs': inputs, 'selection': selection,
                            'results_sha256': digest(results)}
        selection_sha = lock(directory/'diverse-long-selection-v1.json', frozen_selection)
        self.stdout.write(f'SELECTION LOCKED: candidate={selection["audit_candidate"]}; passed={selection["passed"]}')
        fresh = None
        # No unseen file/API access happens until AFTER the immutable selection.
        if not options['discovery_only'] and selection['audit_candidate']:
            logger = logging.getLogger('t_tech.invest')
            level = logger.level
            logger.setLevel(logging.CRITICAL)
            try:
                for ticker in research.TICKERS:
                    new, source = fresh_input(directory, ticker, histories[ticker]['instrument'], options['fetch'])
                    histories[ticker]['candles'] = histories[ticker]['candles']+new
                    inputs[ticker] = {**inputs[ticker], 'fresh': source}
                    self.stdout.write(f'FRESH INPUT {ticker}: {len(new)} bars')
            finally:
                logger.setLevel(level)
            fresh = evaluate(histories, universe(histories), selection['audit_candidate'], research.FRESH)
            self.stdout.write(f'FRESH AUDIT {selection["audit_candidate"]}: {json.dumps({k:{j:v[j] for j in ("trades","unknown_trades","return_pct")} for k,v in fresh.items()})}')
        at = datetime.now(timezone.utc)
        report = {'at': at.isoformat(), 'spec': spec, 'protocol_sha256': protocol_sha,
                  'selection_sha256': selection_sha, 'inputs': inputs, 'results': results,
                  'selection': selection, 'fresh': fresh, 'entry_allowed': False,
                  'fresh_status': 'not_evaluated' if fresh is None else 'later_history_only_not_prospective',
                  'caution': 'No profit guarantee. Old folds are reused research. Only one rule checked on two new sessions; L2 contribution not tested.'}
        report['artifact_sha256'] = digest(report)
        path = Path(settings.BASE_DIR)/'dashboard'/'ai_models'/f'diverse-long-v1-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path, report)
        self.stdout.write(f'REPORT {path}')
