"""Register one nonlinear challenger before reading cached public prices."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import market_ai as v1, market_ai_v2 as ai
from dashboard.opening_activity import TICKERS
from dashboard.management.commands.train_market_ai import source_hashes as old_hashes, digest, aggregate
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable
from dashboard.management.commands.study_opening_activity import extension


def source_hashes():
    root = Path(__file__).resolve().parents[2]
    return {**old_hashes(), **{n:hashlib.sha256((root/n).read_bytes()).hexdigest()
            for n in ('market_ai_v2.py','management/commands/train_market_ai_v2.py')}}


def baseline_model():
    # Fixed v1 artifact, not whichever model happens to be newest after retraining.
    path = Path(settings.BASE_DIR)/'dashboard'/'ai_models'/'market-ai-v1-20261013-170413-450504.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    if (saved['version']!=v1.VERSION or saved['source_sha256']!=old_hashes()
            or saved['artifact_sha256']!=digest({k:v for k,v in saved.items() if k!='artifact_sha256'})):
        raise CommandError('Fixed v1 comparator failed identity check')
    return saved


def freeze(directory):
    spec = json.loads(json.dumps({'version':ai.VERSION,'rules':ai.RULES,'features':ai.FEATURES,
                                 'periods':v1.PERIODS,'tickers':TICKERS,'sources':source_hashes(),
                                 'baseline_artifact':'d12e3dec8fa3f07d55ee7b04f5e6967840ac4e4beb45dbd98158c0f9ba7ef5fe'}))
    directory.mkdir(exist_ok=True,parents=True)
    path = directory/'market-ai-training-protocol-v2.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec']!=spec:
            raise CommandError('Frozen v2 changed; prices not read. Register a new experiment.')
    else:
        now = datetime.now(timezone.utc)
        write_immutable(path,{'registered_at':now.isoformat(),'spec':spec,
            'prospective_start':(now+timedelta(days=1)).replace(hour=7,minute=0,second=0,microsecond=0).isoformat()})
    return path


def evaluate(model, rows, projector=None):
    if projector:
        model,rows = projector(model,rows)
    result = {'classification':v1.metrics(model,rows)}
    for key,cost in (('base',1),('double_cost',2)):
        accounts = {t:v1.simulate(model,[r for r in rows if r['ticker']==t],cost) for t in TICKERS}
        result[key],result[key+'_accounts'] = aggregate(list(accounts.values())),accounts
    return result


class Command(BaseCommand):
    help = 'Train fixed nonlinear challenger on cached prices, compare paired v1; no orders/downloads.'

    def handle(self,*args,**options):
        data = Path(settings.BASE_DIR)/'data'
        protocol = freeze(data)
        baseline = baseline_model()
        datasets,sources,base_x = {},{},{}
        for ticker in TICKERS:
            old = acquire(data,ticker,fetch=False)
            if not old['quality']['exchange_source_verified'] or not old['quality']['exchange_requested']:
                raise CommandError('Only verified exchange candles allowed')
            extra,source = extension(data,ticker,old['saved']['instrument'],False)
            bars = old['candles']+extra
            datasets[ticker] = ai.examples(bars,ticker,old['lot'])
            base_x.update({(ticker,r['at']):r['x'] for r in v1.examples(bars,ticker,old['lot'])})
            sources[ticker] = {'historical':old['source'],'additional':source,'lot':old['lot']}
            self.stdout.write(f'V2 INPUT {ticker}: {len(datasets[ticker])} available decision examples')
        groups = v1.split([r for values in datasets.values() for r in values])
        train = [r for r in groups['train'] if r['y'] is not None]
        calibration = [r for r in groups['calibration'] if r['y'] is not None]
        if len(train)<ai.RULES['minimum_train_rows'] or len(calibration)<ai.RULES['minimum_calibration_rows']:
            raise CommandError('Insufficient chronological samples')
        self.stdout.write(f'FITTING: {len(train)} rows, {ai.RULES["trees"]} fixed depth-2 trees')
        classifier = ai.fit([r['x'] for r in train],[r['y'] for r in train])
        calibrator = v1.fit([[ai.raw(classifier,r['x'])] for r in calibration],[r['y'] for r in calibration])
        learned = {'classifier':classifier,'calibrator':calibrator}
        evaluations,comparison = {},{}
        for period,rows in groups.items():
            evaluations[period] = evaluate(learned,rows,ai.projection)
            paired = [{**r,'x':base_x[(r['ticker'],r['at'])]} for r in rows]
            comparison[period] = evaluate(baseline['model'],paired)
            e,b = evaluations[period],comparison[period]
            self.stdout.write(f'V2 {period}: n={e["classification"]["samples"]}, AUC={e["classification"]["auc"]:.4f} / v1={b["classification"]["auc"]:.4f}, Brier={e["classification"]["brier"]:.6f} / v1={b["classification"]["brier"]:.6f}, trades={e["base"]["trades"]}, net={e["base"]["return_pct"]:+.4f}% / x2={e["double_cost"]["return_pct"]:+.4f}%')
        at = datetime.now(timezone.utc)
        artifact = {'version':ai.VERSION,'trained_at':at.isoformat(),'features':list(ai.FEATURES),'rules':ai.RULES,
            'tickers':list(TICKERS),'source_sha256':source_hashes(),'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),
            'model':learned,'inputs':sources,'periods':v1.PERIODS,'trained_rows':len(train),'calibration_rows':len(calibration),
            'evaluations':evaluations,'comparison_v1':comparison,'comparison_model_id':baseline['artifact_sha256'],
            'observation_ready':ai.observation_ready(evaluations,comparison),'paper_ready':v1.paper_ready(evaluations),
            'entry_allowed':False,'prospective_start':json.loads(protocol.read_text(encoding='utf-8'))['prospective_start'],
            'note':'One new fixed trial on previously examined dates. Paired comparison only; no independent prospective proof or historical order-book training.'}
        artifact['artifact_sha256'] = digest(artifact)
        output = Path(settings.BASE_DIR)/'dashboard'/'ai_models'
        output.mkdir(exist_ok=True,parents=True)
        path = output/f'market-ai-v2-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,artifact)
        self.stdout.write(f'TRAINED V2: {path}; observation_ready={artifact["observation_ready"]}; paper_ready={artifact["paper_ready"]}; entry_allowed=False')
