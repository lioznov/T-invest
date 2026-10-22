"""Train one fixed local model; later-date labels never enter fitting."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from statistics import mean
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import market_ai as ai
from dashboard.opening_activity import TICKERS
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable
from dashboard.management.commands.study_opening_activity import extension


def source_hashes():
    root=Path(__file__).resolve().parents[2]
    names=('market_ai.py','management/commands/train_market_ai.py','signals.py','risk.py','method_study.py',
           'management/commands/study_pattern_flow.py','management/commands/study_opening_activity.py')
    return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def freeze(directory):
    spec=json.loads(json.dumps({'version':ai.VERSION,'features':ai.FEATURES,'rules':ai.RULES,
                               'periods':ai.PERIODS,'tickers':TICKERS,'source_sha256':source_hashes()}))
    path=directory/'market-ai-training-protocol-v1.json'
    directory.mkdir(exist_ok=True,parents=True)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec']!=spec:
            raise CommandError('Frozen AI protocol changed. Preserve v1 and register a new experiment; prices not read.')
    else:
        now=datetime.now(timezone.utc)
        write_immutable(path,{'registered_at':now.isoformat(),'spec':spec,
                        'prospective_start':(now+timedelta(days=1)).replace(hour=7,minute=0,second=0,microsecond=0).isoformat()})
    return path


def aggregate(models):
    return {'trades':sum(m['trades'] for m in models),'unknown_trades':sum(m['unknown_trades'] for m in models),
            'return_pct':mean(m['return_pct'] for m in models),'accounts':len(models),
            'positive_accounts':sum(m['return_pct']>0 for m in models)}


class Command(BaseCommand):
    help='Train local calibrated logistic market classifier on cached T-Bank history; no paid AI API or orders.'

    def handle(self,*args,**options):
        data=Path(settings.BASE_DIR)/'data'
        protocol=freeze(data)
        datasets,sources={},{}
        for ticker in TICKERS:
            old=acquire(data,ticker,fetch=False)
            if not old['quality']['exchange_source_verified'] or not old['quality']['exchange_requested']:
                raise CommandError('Training requires confirmed exchange candles')
            extra,extra_source=extension(data,ticker,old['saved']['instrument'],False)
            datasets[ticker]=ai.examples(old['candles']+extra,ticker,old['lot'])
            sources[ticker]={'historical':old['source'],'additional':extra_source,'lot':old['lot']}
            self.stdout.write(f"AI INPUT {ticker}: {len(datasets[ticker])} historical decision examples")
        groups=ai.split([r for values in datasets.values() for r in values])
        train=[r for r in groups['train'] if r['y'] is not None]
        calibration=[r for r in groups['calibration'] if r['y'] is not None]
        if len(train)<ai.RULES['minimum_train_rows'] or len(calibration)<ai.RULES['minimum_calibration_rows']:
            raise CommandError('Insufficient chronological training/calibration data; no model fabricated')
        classifier=ai.fit([r['x'] for r in train],[r['y'] for r in train])
        calibrator=ai.fit([[ai.logit(classifier,r['x'])] for r in calibration],[r['y'] for r in calibration])
        learned={'classifier':classifier,'calibrator':calibrator}
        evaluations={}
        for period,rows in groups.items():
            evaluations[period]={'classification':ai.metrics(learned,rows)}
            for scenario,cost in (('base',1),('double_cost',2)):
                accounts={ticker:ai.simulate(learned,[r for r in rows if r['ticker']==ticker],cost) for ticker in TICKERS}
                evaluations[period][scenario]=aggregate(list(accounts.values()))
                evaluations[period][scenario+'_accounts']=accounts
            m=evaluations[period]
            self.stdout.write(f"AI {period}: samples={m['classification']['samples']}; AUC={m['classification']['auc']}; Brier={m['classification']['brier']}; LONG={m['base']['trades']}; net={m['base']['return_pct']:+.4f}% / x2={m['double_cost']['return_pct']:+.4f}%")
        at=datetime.now(timezone.utc)
        artifact={'version':ai.VERSION,'trained_at':at.isoformat(),'features':list(ai.FEATURES),'tickers':list(TICKERS),
                  'model':learned,'source_sha256':source_hashes(),'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),
                  'inputs':sources,'periods':ai.PERIODS,'rules':ai.RULES,'evaluations':evaluations,
                  'paper_ready':ai.paper_ready(evaluations),'entry_allowed':False,'trained_rows':len(train),'calibration_rows':len(calibration),
                  'unknown_training_examples':sum(r['y'] is None for r in groups['train']),
                  'prospective_start':json.loads(protocol.read_text(encoding='utf-8'))['prospective_start'],
                  'runtime':{'numpy':version('numpy')},
                  'note':'Model estimates net-positive one-hour candle replay with an initial stop, not actual future profit. Previously examined dates. No independent prospective trading proof.'}
        artifact['artifact_sha256']=digest(artifact)
        output=Path(settings.BASE_DIR)/'dashboard'/'ai_models'
        output.mkdir(exist_ok=True,parents=True)
        path=output/f'market-ai-v1-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,artifact)
        self.stdout.write(f'TRAINED MODEL: {path}; paper_ready={artifact["paper_ready"]}; entry_allowed=False')
