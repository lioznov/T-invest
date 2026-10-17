"""Freeze a bounded momentum family, test local broker history, retain every result."""
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import monthly_momentum as model
from dashboard.management.commands.study_ranges_full import load_input
from dashboard.management.commands.study_pattern_flow import write_immutable


def hashes():
    root = Path(__file__).resolve().parents[2]
    names = ('monthly_momentum.py', 'management/commands/study_monthly_momentum.py',
             'management/commands/study_ranges_full.py', 'management/commands/study_ranges.py',
             'range_study.py', 'strategy_search.py', 'signals.py', 'management/commands/study_pattern_flow.py')
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}


def freeze(directory):
    spec = json.loads(json.dumps({'version': model.VERSION, 'rules': model.RULES,
         'variants': {r.key: asdict(r) for r in model.VARIANTS}, 'tickers': model.TICKERS,
         'periods': model.PERIODS, 'folds': model.FOLDS, 'scenarios': model.SCENARIOS,
         'reserves_not_read': ['TATN', 'CHMF', 'PHOR'], 'source_sha256': hashes()}))
    path = directory/'monthly-momentum-protocol-v1.json'
    directory.mkdir(exist_ok=True, parents=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding='utf-8'))
        if existing['spec'] != spec:
            raise CommandError('Registered rules changed; prices not read. Create a new experiment version.')
    else:
        write_immutable(path, {'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': spec})
    return path


def brief(result):
    return {k: v for k, v in result.items() if k not in ('curve', 'trade_log', 'decisions')}


class Command(BaseCommand):
    help = '12 preregistered monthly LONG-only portfolios, execution stress and chronological selection. No orders.'

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory)
        prepared, provenance = {}, {}
        for ticker in model.TICKERS:
            data = load_input(directory/f'range-input-{ticker}-20261003.json', ticker)
            prepared[ticker] = model.prepare(data['candles'], data['lot'])
            provenance[ticker] = {**data['source'], 'lot': data['lot'],
                                  'first': data['candles'][0].time.isoformat(), 'last': data['candles'][-1].time.isoformat()}
        results, summaries, count = {}, [], 0
        for rule in model.VARIANTS:
            periods = {}
            for period, (first, last) in model.PERIODS.items():
                periods[period] = {}
                for scenario, (cost, delay) in model.SCENARIOS.items():
                    periods[period][scenario] = model.simulate(prepared, rule, first, last, cost, delay)
                    count += 1
            results[rule.key] = periods
            summary = {'key': rule.key, 'passes_screen': model.screen(periods), 'entry_allowed': False,
                       'periods': {p: {s: brief(v) for s, v in scenarios.items()} for p, scenarios in periods.items()}}
            summaries.append(summary)
            self.stdout.write(rule.key+': '+'; '.join(f"{p} {scenarios['base']['return_pct']:+.3f}% / x2 {scenarios['double_cost']['return_pct']:+.3f}% ({scenarios['base']['trades']} trades)" for p, scenarios in periods.items()))
        folds = []
        for first, last in model.FOLDS:
            training = {r.key: model.simulate(prepared, r, model.PERIODS['full'][0], first, 2) for r in model.VARIANTS}
            count += len(training)
            key = model.choose(training)
            chosen = next((r for r in model.VARIANTS if r.key == key), None)
            # Choice is made only from the prefix above. Test-period results are
            # never arguments to choose(), including when the chosen rule loses.
            tests = {s: model.simulate(prepared, chosen, first, last, cost, delay)
                     for s, (cost, delay) in model.SCENARIOS.items() if s != 'gross'} if chosen else {}
            count += len(tests)
            folds.append({'first': first, 'last_exclusive': last, 'choice': key,
                          'training': {k: brief(v) for k, v in training.items()}, 'tests': tests,
                          'cash_when_no_candidate': key is None})
        rolling = {}
        for scenario in ('base', 'double_cost', 'delayed'):
            equity, trades, unknown, selected = 100000., 0, 0, 0
            for fold in folds:
                if fold['choice'] is not None:
                    r = fold['tests'][scenario]
                    equity *= 1+r['return_pct']/100
                    trades += r['trades']
                    unknown += len(r['quality_incidents'])
                    selected += 1
            rolling[scenario] = {'return_pct': (equity/100000-1)*100, 'trades': trades,
                                 'unknown_incidents': unknown, 'selected_folds': selected,
                                 'note': 'Product of separately capitalised 100000-ruble fold returns; rounded lots reset at each fold, not a single continuous account.'}
        bootstrap = model.bootstrap_family({k: v['full']['double_cost'] for k, v in results.items()})
        benchmarks = {f'allocation_{int(a*100)}': {p: {s: model.buy_hold(prepared, first, last, a, cost)
                      for s, cost in (('base', 1), ('double_cost', 2))} for p, (first, last) in model.PERIODS.items()}
                      for a in (.25, .75)}
        count += 2*len(model.PERIODS)*2
        passed = [r['key'] for r in summaries if r['passes_screen']]
        at = datetime.now(timezone.utc)
        report = {'version': model.VERSION, 'at': at.isoformat(), 'protocol_sha256': hashlib.sha256(protocol.read_bytes()).hexdigest(),
                  'source_sha256': hashes(), 'inputs': provenance, 'results': results, 'summaries': summaries,
                  'folds': folds, 'rolling_summary': rolling, 'bootstrap': bootstrap, 'simulations': count, 'benchmarks': benchmarks,
                  'screen_candidates': passed, 'entry_allowed': False,
                  'previous_trials': 83, 'new_trials': 12, 'total_trials': 95,
                  'limitations': model.RULES['limitations'],
                  'confirmation': 'No independent reserved-stock or prospective confirmation; no reserve prices read.'}
        path = directory/f'monthly-momentum-study-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path, report)
        self.stdout.write('ROLLING '+json.dumps(rolling, ensure_ascii=True))
        self.stdout.write('BOOTSTRAP '+json.dumps(bootstrap, ensure_ascii=True))
        self.stdout.write(f'Simulations: {count}; passed: {passed}; report: {path}')
