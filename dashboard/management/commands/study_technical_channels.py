"""Two frozen candle-only channel hypotheses; no fabricated book history."""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
from statistics import mean,median
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from dashboard import technical_context as tech, neural_trader as nn, portfolio_replay
from dashboard.signals import features,MSK
from dashboard.neural_service import load_model
from dashboard.management.commands.train_market_ai import digest
from dashboard.management.commands.train_neural_trader import source_hashes as neural_hashes
from dashboard.management.commands.study_pattern_flow import acquire,write_immutable
from dashboard.management.commands.study_opening_activity import extension
from dashboard.management.commands.verify_neural_trader import load_extension

VARIANTS={'range_break':'Выход из коридора 20 + тренд + объём',
          'regression_break':'Выход из наклонного коридора 60 + тренд + объём'}
PERIODS={'test':('2026-09-01','2026-10-03'),'additional':('2026-10-03','2026-10-13'),'later':('2026-10-13','2026-10-17')}
RULES={'variants':VARIANTS,'trend':'closed price above rising EMA50','volume':1.3,'buffer_atr':.1,
       'crossing':'new upward close across prior-window upper bound; adjacent same-day bars only',
       'replay':'same one-hour next-open stopped LONG labels as frozen neural v1; one shared account',
       'priority':'ticker alphabetical at equal decision times; no future-return selection',
       'independence':'all three periods already examined; two exploratory hypotheses, no new independent evidence',
       'book':'historical L2 not included; this test does not establish value of chart+order-book confirmation',
       'entry_allowed':False}


def source_hashes():
    return {**neural_hashes(),'technical_context.py':hashlib.sha256(Path(tech.__file__).read_bytes()).hexdigest(),
            'channel_study_command':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def freeze(directory,model):
    directory.mkdir(exist_ok=True,parents=True)
    spec=json.loads(json.dumps({'version':tech.VERSION,'rules':RULES,'channel_rules':tech.RULES,
            'portfolio_rules':portfolio_replay.RULES,'periods':PERIODS,'tickers':model['tickers'],
            'neural_model_id':model['artifact_sha256'],'source_sha256':source_hashes()}))
    path=directory/'technical-channels-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec']!=spec:
            raise CommandError('Frozen channel experiment changed; prices not read')
    else:
        write_immutable(path,{'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec})
    return path


def flags(candles,channels,fs,i):
    if i<61:
        return dict.fromkeys(VARIANTS,False)
    c,previous=candles[i],candles[i-1]
    eligible=(c.time-previous.time==timedelta(minutes=15)
              and c.time.astimezone(MSK).date()==previous.time.astimezone(MSK).date()
              and c.close>fs[i]['ema'] and fs[i]['rising'] and fs[i]['rvol']>=RULES['volume'])
    result={}
    for key,field in (('range_break','range'),('regression_break','regression')):
        band,prior=channels[i][field],channels[i-1][field]
        result[key]=bool(eligible and band and prior and c.close>band['upper']+.1*fs[i-1]['atr']
                         and previous.close<=prior['upper']+.1*fs[i-2]['atr'])
    return result


def describe(rows):
    known=[r for r in rows if r['outcome']]
    if not known:
        return {'samples':0}
    probabilities=[r['probability'] for r in known]
    returns=[r['expected_net_pct'] for r in known]
    return {'samples':len(known),'probability_min':min(probabilities),'probability_median':median(probabilities),
            'probability_max':max(probabilities),'expected_net_min_pct':min(returns),
            'expected_net_median_pct':median(returns),'expected_net_max_pct':max(returns),
            'passes_probability':sum(p>=nn.RULES['candidate_threshold'] for p in probabilities),
            'passes_expected_return':sum(r>=nn.RULES['minimum_expected_net_pct'] for r in returns),
            'passes_both':sum(r['probability']>=nn.RULES['candidate_threshold'] and r['expected_net_pct']>=nn.RULES['minimum_expected_net_pct'] for r in known),
            'observed_mean_net_pct':mean(r['outcome']['net_fraction']*100 for r in known)}


class Command(BaseCommand):
    help='Two fixed channel LONG replays plus frozen neural score diagnostics; cached exchange inputs only, no orders.'

    def handle(self,*args,**options):
        model=load_model()
        if model is None:
            raise CommandError('Verified neural artifact required for the paired diagnostic cohort')
        directory=Path(settings.BASE_DIR)/'data'
        protocol=freeze(directory,model)
        groups={p:[] for p in PERIODS};inputs={}
        for ticker in model['tickers']:
            old=acquire(directory,ticker,fetch=False)
            if not all(old['quality'][k] for k in ('exchange_source_verified','exchange_requested')):
                raise CommandError('Unverified exchange candles')
            prior,prior_source=extension(directory,ticker,old['saved']['instrument'],False)
            extra,extra_source=load_extension(directory,ticker,old['saved']['instrument'],False)
            candles=old['candles']+prior+extra
            channels,fs=tech.channel_rows(candles),features(candles)
            indices={(c.time+timedelta(minutes=15)).astimezone(timezone.utc).isoformat():i for i,c in enumerate(candles)}
            for row in nn.replay_rows(candles,ticker,old['lot']):
                for p,(first,last) in PERIODS.items():
                    if first<=row['at'][:10]<last and row['label_end'][:10]<last:
                        estimate=nn.estimate(model['model'],row['x'])
                        groups[p].append({**row,**estimate,'channel_flags':flags(candles,channels,fs,indices[row['at']])})
            inputs[ticker]={'context':old['source'],'prior':prior_source,'additional':extra_source}
            self.stdout.write(f'CHANNEL INPUT {ticker}')
        results,diagnostics={},{}
        for p,rows in groups.items():
            results[p]={}
            diagnostics[p]={'all':describe(rows),'stocks':{t:describe([r for r in rows if r['ticker']==t]) for t in model['tickers']}}
            self.stdout.write(f'NEURAL DIAGNOSTIC {p}: {diagnostics[p]["all"]}')
            for variant in VARIANTS:
                # Zero priority scores force a fixed ticker tie break. Neural
                # predictions do not choose or rank this candle-only policy.
                selected=[{**r,'selected':r['channel_flags'][variant],'expected_net_pct':0.} for r in rows]
                results[p][variant]={cost:portfolio_replay.simulate(selected,m) for cost,m in (('base',1),('double_cost',2))}
                r=results[p][variant]
                self.stdout.write(f'CHANNEL {p} {variant}: trades={r["base"]["trades"]}, net={r["base"]["return_pct"]}, x2={r["double_cost"]["return_pct"]}')
        at=datetime.now(timezone.utc)
        artifact={'version':tech.VERSION,'at':at.isoformat(),'rules':RULES,'periods':PERIODS,
                  'source_sha256':source_hashes(),'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),
                  'inputs':inputs,'neural_model_id':model['artifact_sha256'],'results':results,'diagnostics':diagnostics,
                  'entry_allowed':False,'independent_prospective':False}
        artifact['artifact_sha256']=digest(artifact)
        path=Path(settings.BASE_DIR)/'dashboard'/'ai_models'/f'technical-channels-study-v1-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,artifact)
        self.stdout.write(f'CHANNEL REPORT: {path}')
