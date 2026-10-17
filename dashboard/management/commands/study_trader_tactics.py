import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from dashboard.trader_tactics import VERSION,RULES,VARIANTS,registry,features,simulate,passes
from dashboard.range_study import TICKERS,RESERVES,PERIODS
from dashboard.strategy_search import aggregate, STRATEGIES, feature_rows, simulate as old_simulate
from dashboard.management.commands.study_ranges_full import load_input,hashes as input_hashes
from dashboard.management.commands.study_pattern_flow import write_immutable

BASELINES = tuple(s for s in STRATEGIES if s.family=='rsi' and s.timeframe=='day' and s.threshold==5 and s.trend_filter)


def hashes():
    root = Path(__file__).resolve().parents[2]
    return {**input_hashes(),**{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in ('trader_tactics.py','management/commands/study_trader_tactics.py')}}


def freeze(directory):
    spec = {'version':VERSION,'rules':RULES,'registry':registry(),'tickers':TICKERS,'reserves_not_read':RESERVES,
            'periods':PERIODS,'entry_allowed':False,'baseline_keys':[s.key for s in BASELINES]}
    signature = json.loads(json.dumps({'spec':spec,'source_sha256':hashes()},sort_keys=True))
    directory.mkdir(parents=True,exist_ok=True)
    path = directory/'trader-tactics-protocol-v1.json'
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if any(saved.get(k)!=v for k,v in signature.items()):
            raise CommandError('Frozen trader tactics changed; no prices read.')
    else:
        write_immutable(path,{'at':datetime.now(timezone.utc).isoformat(),**signature})
    return path


class Command(BaseCommand):
    help = 'Three registered trader-inspired pullbacks; local prices only, no real orders.'

    def handle(self,*args,**options):
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory)
        results = []
        for ticker in TICKERS:
            data = load_input(directory/f'range-input-{ticker}-20261003.json',ticker)
            rows,old_rows = features(data['candles']),feature_rows(data['candles'])
            results.append({'ticker':ticker,'quality':data['quality'],'source':data['source'],
                            'variants':{r.key:{n:simulate(data['candles'],rows,r,first,last,data['lot']) for n,(first,last) in PERIODS.items()} for r in VARIANTS},
                            'baselines':{r.key:{n:old_simulate(data['candles'],old_rows,r,first,last,data['lot']) for n,(first,last) in PERIODS.items()} for r in BASELINES}})
        summaries = []
        for rule in VARIANTS:
            periods = {n:aggregate([r['variants'][rule.key][n] for r in results]) for n in PERIODS}
            summaries.append({'key':rule.key,'periods':periods,'passes_exploratory_screen':passes(periods), 'entry_allowed':False})
            self.stdout.write(rule.key+': '+'; '.join(f"{n} {m['return_pct']:+.4f}% / stress {m['stress_return_pct']:+.4f}% ({m['trades']} trades; {m['unverified_trades']} unknown)" for n,m in periods.items()))
        at = datetime.now(timezone.utc)
        path = directory/f'trader-tactics-study-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,{'version':VERSION,'at':at.isoformat(),'entry_allowed':False,'source_sha256':hashes(),
                              'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),'results':results,'summaries':summaries,
                              'baselines':[{'key':r.key,'periods':{n:aggregate([s['baselines'][r.key][n] for s in results]) for n in PERIODS}} for r in BASELINES],
                              'note':'Three exploratory adapted tactics after 77 attempts. Conditional daily-bar limit fills, not real execution; previously viewed periods, no independent holdout.'})
        self.stdout.write(str(path))
