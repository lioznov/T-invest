"""Separate fixed correction: include weekend bars while positions are held."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.range_study import TICKERS, RESERVES, PERIODS, RULES as OLD_RULES, VARIANTS, registry, features, simulate, passes
from dashboard.signals import Candle
from dashboard.strategy_search import validate_bars, aggregate, feature_rows, simulate as baseline_simulate
from dashboard.management.commands.study_ranges import load_input as validate_input, hashes as original_hashes, BASELINES
from dashboard.management.commands.study_pattern_flow import write_immutable

VERSION = 'range_swing_v2_full_history'
RULES = {**OLD_RULES, 'previous_trials':73,
         'data_correction': 'all completed DAY EXCHANGE bars including weekends; weekday entry only; held stops evaluated on EVERY observed bar',
         'time_exit_bars_unit': 'all observed daily trading bars, including weekends',
         'lookback_unit': 'all preceding observed daily trading bars, including weekends',
         'first_version': 'v1 discarded weekend prices; descriptive only, incomplete stop paths; preserved unchanged'}


def hashes():
    return {**original_hashes(), 'management/commands/study_ranges_full.py':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def freeze(directory):
    spec = {'version':VERSION, 'registry':registry(), 'rules':RULES, 'periods':PERIODS, 'tickers':TICKERS,
            'reserves_not_read':RESERVES, 'entry_allowed':False,
            'paired_baselines':[b.key for b in BASELINES]}
    signature = json.loads(json.dumps({'spec':spec,'source_sha256':hashes()},sort_keys=True))
    directory.mkdir(exist_ok=True,parents=True)
    path = directory/'range-swing-protocol-v2.json'
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if any(saved.get(k) != v for k,v in signature.items()):
            raise CommandError('Frozen full-history rules changed; no prices read.')
    else:
        write_immutable(path, {'at':datetime.now(timezone.utc).isoformat(),**signature})
    return path


def load_input(path, ticker):
    # Reuse exact v1 provenance/OHLC checks, but not its weekday-only price path.
    checked = validate_input(path, ticker)
    raw = json.loads(path.read_text(encoding='utf-8'))
    candles = [Candle(datetime.fromisoformat(r['time']),*(float(r[k]) for k in ('open','high','low','close')),r['volume']) for r in raw['bars']]
    validate_bars(candles)
    return {**checked,'candles':candles,
            'source':{**checked['source'],'bars':len(candles),'weekend_bars':len(candles)-checked['source']['bars']}}


class Command(BaseCommand):
    help = 'Registered full daily-history correction; reads local EXCHANGE data only, no orders.'

    def handle(self,*args,**options):
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory)
        results = []
        for ticker in TICKERS:
            data = load_input(directory/f'range-input-{ticker}-20261003.json',ticker)
            rows, old_rows = features(data['candles']), feature_rows(data['candles'])
            results.append({'ticker':ticker,'quality':data['quality'],'source':data['source'],
                            'variants':{r.key:{n:simulate(data['candles'],rows,r,first,last,data['lot']) for n,(first,last) in PERIODS.items()} for r in VARIANTS},
                            'baselines':{r.key:{n:baseline_simulate(data['candles'],old_rows,r,first,last,data['lot']) for n,(first,last) in PERIODS.items()} for r in BASELINES}})
        summaries = []
        for rule in VARIANTS:
            periods = {n:aggregate([r['variants'][rule.key][n] for r in results]) for n in PERIODS}
            summaries.append({'key':rule.key,'periods':periods,'entry_allowed':False,
                              'passes_exploratory_screen':passes(periods,{r['ticker']:r['quality'] for r in results})})
            self.stdout.write(rule.key+': '+'; '.join(f"{n} {m['return_pct']:+.4f}% / stress {m['stress_return_pct']:+.4f}% ({m['trades']} trades, {m['unverified_trades']} unknown)" for n,m in periods.items()))
        at = datetime.now(timezone.utc)
        path = directory/f'range-swing-v2-study-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,{'version':VERSION,'at':at.isoformat(),'entry_allowed':False,'source_sha256':hashes(),
                              'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),'results':results,'summaries':summaries,
                              'baselines':[{'key':b.key,'periods':{n:aggregate([r['baselines'][b.key][n] for r in results]) for n in PERIODS}} for b in BASELINES],
                              'note':'Full-price-path correction. Four further explorations after 73; 77 including alternative data processing. Weekday entry only, ALL daily bars while holding. Already seen periods, no independent holdout.'})
        self.stdout.write(str(path))
