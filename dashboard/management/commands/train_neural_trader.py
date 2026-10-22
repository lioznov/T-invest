"""One registered real neural experiment, not a parameter sweep."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import market_ai as v1, market_ai_v2 as v2, neural_trader as nn, portfolio_replay
from dashboard.opening_activity import TICKERS
from dashboard.management.commands.train_market_ai import digest
from dashboard.management.commands.train_market_ai_v2 import source_hashes as base_hashes, baseline_model
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable
from dashboard.management.commands.study_opening_activity import extension


def source_hashes():
    root = Path(__file__).resolve().parents[2]
    return {**base_hashes(), **{name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                for name in ('neural_trader.py','portfolio_replay.py','management/commands/train_neural_trader.py')}}


def freeze(directory):
    directory.mkdir(exist_ok=True,parents=True)
    spec = json.loads(json.dumps({'version':nn.VERSION,'features':nn.FEATURES,'rules':nn.RULES,
                'portfolio_rules':portfolio_replay.RULES,'periods':v1.PERIODS,'tickers':TICKERS,'sources':source_hashes()}))
    path = directory/'neural-trader-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != spec:
            raise CommandError('Frozen neural experiment changed; prices not read. Register a new version.')
    else:
        at = datetime.now(timezone.utc)
        write_immutable(path,{'registered_at':at.isoformat(),'spec':spec,
                             'prospective_start':(at+timedelta(days=1)).replace(hour=7,minute=0,second=0,microsecond=0).isoformat()})
    return path


def evaluate(model, rows):
    proxy, projected = nn.projection(model,rows)
    scored = []
    for row in rows:
        e = nn.estimate(model,row['x'])
        selected = e['probability']>=nn.RULES['candidate_threshold'] and e['expected_net_pct']>=nn.RULES['minimum_expected_net_pct']
        scored.append({**row,**e,'selected':selected})
    known = [r for r in scored if r['outcome']]
    return {'classification':v1.metrics(proxy,projected),
            'expected_return_mae_pct':mean(abs(r['expected_net_pct']-r['outcome']['net_fraction']*100) for r in known) if known else None,
            'candidates':sum(r['selected'] for r in scored),
            'base':portfolio_replay.simulate(scored),'double_cost':portfolio_replay.simulate(scored,2)}


def paper_ready(evaluations):
    return all(all((e:=evaluations[p][cost])['trades']>=30 and e['unknown_trades']==0
                   and e['return_pct'] is not None and e['return_pct']>0
                   and e['max_realised_drawdown_pct']<=8 for cost in ('base','double_cost'))
               and evaluations[p]['classification']['brier']<evaluations[p]['classification']['baseline_brier']
               for p in ('test','additional'))


class Command(BaseCommand):
    help = 'Train fixed 20-16-8-2 neural LONG researcher on cached exchange candles; one shared account, no orders.'

    def handle(self,*args,**options):
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory)  # Always before any price or baseline read.
        old = baseline_model()
        baseline_path = Path(settings.BASE_DIR)/'dashboard'/'ai_models'/'market-ai-v2-20261013-173726-534907.json'
        boost = json.loads(baseline_path.read_text(encoding='utf-8'))
        if boost['source_sha256']!=base_hashes() or boost['artifact_sha256']!=digest({k:v for k,v in boost.items() if k!='artifact_sha256'}):
            raise CommandError('Fixed tree comparator identity failed')
        rows, inputs, linear_x = [], {}, {}
        for ticker in TICKERS:
            cached = acquire(directory,ticker,fetch=False)
            if not all(cached['quality'][k] for k in ('exchange_source_verified','exchange_requested')):
                raise CommandError('Unconfirmed exchange source')
            extra, source = extension(directory,ticker,cached['saved']['instrument'],False)
            candles = cached['candles']+extra
            rows.extend(nn.replay_rows(candles,ticker,cached['lot']))
            linear_x.update({(ticker,r['at']):r['x'] for r in v1.examples(candles,ticker,cached['lot'])})
            inputs[ticker] = {'historical':cached['source'],'additional':source,'lot':cached['lot']}
            self.stdout.write(f'NEURAL INPUT {ticker}')
        groups = v1.split(sorted(rows,key=lambda r:(r['at'],r['ticker'])))
        train = [r for r in groups['train'] if r['y'] is not None]
        cal = [r for r in groups['calibration'] if r['y'] is not None]
        if len(train)<nn.RULES['minimum_train_rows'] or len(cal)<nn.RULES['minimum_calibration_rows']:
            raise CommandError('Insufficient chronological training/calibration samples')
        self.stdout.write(f'FITTING NEURAL: {len(train)} train / {len(cal)} calibration, 80 fixed epochs')
        network = nn.fit([r['x'] for r in train],[r['y'] for r in train],[r['outcome']['net_fraction']*100 for r in train])
        raw_cal = [nn.raw(network,r['x']) for r in cal]
        calibrator = v1.fit([[p[0]] for p in raw_cal],[r['y'] for r in cal])
        offset = mean(max(-5,min(5,r['outcome']['net_fraction']*100))-p[1] for r,p in zip(cal,raw_cal))
        model = {'network':network,'calibrator':calibrator,'return_offset':offset}
        evaluations, comparison = {}, {}
        for period, samples in groups.items():
            evaluations[period] = evaluate(model,samples)
            e = evaluations[period]
            self.stdout.write(f'NEURAL {period}: AUC={e["classification"]["auc"]}; candidates={e["candidates"]}; trades={e["base"]["trades"]}; net={e["base"]["return_pct"]}; x2={e["double_cost"]["return_pct"]}')
            comparison[period] = {}
            for name, engine, learned in (('linear',v1,old['model']),('trees',v2,boost['model'])):
                scored = []
                for r in samples:
                    x = linear_x[(r['ticker'],r['at'])] if name=='linear' else r['x']
                    p = engine.predict(learned,x)
                    scored.append({**r,'probability':p,'selected':p>=.60})
                comparison[period][name] = {c:portfolio_replay.simulate(scored,m) for c,m in (('base',1),('double_cost',2))}
        at = datetime.now(timezone.utc)
        frozen = json.loads(protocol.read_text(encoding='utf-8'))
        artifact = {'version':nn.VERSION,'trained_at':at.isoformat(),'features':list(nn.FEATURES),'rules':nn.RULES,
                    'portfolio_rules':portfolio_replay.RULES,'source_sha256':source_hashes(),
                    'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),'protocol':frozen,
                    'tickers':list(TICKERS),'inputs':inputs,'periods':v1.PERIODS,'trained_rows':len(train),'calibration_rows':len(cal),
                    'model':model,'evaluations':evaluations,'comparison':comparison,'paper_ready':paper_ready(evaluations),
                    'entry_allowed':False,'prospective_start':frozen['prospective_start'],
                    'note':'One genuine neural experiment on reused history. Cash=0% is the no-trade comparator. Shared 100000 RUB account, no leverage/shorts; no trained book/news/identities or prospective evidence. Realised drawdown understates open-position risk.'}
        artifact['artifact_sha256'] = digest(artifact)
        output = Path(settings.BASE_DIR)/'dashboard'/'ai_models'
        output.mkdir(exist_ok=True,parents=True)
        path = output/f'neural-trader-v1-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,artifact)
        self.stdout.write(f'SAVED NEURAL: {path}; paper_ready={artifact["paper_ready"]}; entry_allowed=False')
