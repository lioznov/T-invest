"""Three pre-registered LONG-only pivot hypotheses, cached prices only."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from dashboard import market_ai as ai, market_structure as structure
from dashboard.opening_activity import TICKERS
from dashboard.management.commands.train_market_ai import source_hashes as ai_hashes, digest, aggregate
from dashboard.management.commands.study_pattern_flow import acquire,write_immutable
from dashboard.management.commands.study_opening_activity import extension


def source_hashes():
    root=Path(__file__).resolve().parents[2]
    return {**ai_hashes(), **{n:hashlib.sha256((root/n).read_bytes()).hexdigest()
        for n in ('market_structure.py','management/commands/study_structure.py')}}


def freeze(directory):
    spec=json.loads(json.dumps({'version':structure.VERSION,'rules':structure.RULES,'variants':structure.VARIANTS,
        'periods':ai.PERIODS,'tickers':TICKERS,'sources':source_hashes()}))
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'market-structure-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec']!=spec:
            raise CommandError('Frozen structure changed; prices not read. Register a separate experiment.')
    else:
        write_immutable(path,{'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec})
    return path


def proxy_model():
    return {'classifier':{'center':[0.],'scale':[1.],'weights':[0.,1.],'prior':.5},
            'calibrator':{'center':[0.],'scale':[1.],'weights':[0.,1.]}}


def replay(rows,key):
    projected=[{**r,'x':[8. if r['structure_flags'][key] else -8.]} for r in rows]
    result={}
    for name,cost in (('base',1),('double_cost',2)):
        accounts={t:ai.simulate(proxy_model(),[r for r in projected if r['ticker']==t],cost) for t in TICKERS}
        result[name]=aggregate(list(accounts.values()))
        result[name+'_accounts']=accounts
    result['signals']=sum(r['structure_flags'][key] for r in rows)
    return result


class Command(BaseCommand):
    help='Test three causal pivot entry rules with frozen one-hour candle exits and costs; no downloads/orders.'

    def handle(self,*args,**options):
        data=Path(settings.BASE_DIR)/'data'
        protocol=freeze(data)
        rows,sources=[],{}
        for ticker in TICKERS:
            old=acquire(data,ticker,fetch=False)
            if not old['quality']['exchange_source_verified'] or not old['quality']['exchange_requested']:
                raise CommandError('Exchange candles required')
            extra,source=extension(data,ticker,old['saved']['instrument'],False)
            bars=old['candles']+extra
            parsed=structure.scan(bars)
            by_at={(c.time+timedelta(minutes=15)).astimezone(timezone.utc).isoformat():structure.flags(r)
                   for c,r in zip(bars,parsed['rows'])}
            examples=ai.examples(bars,ticker,old['lot'])
            rows.extend({**r,'structure_flags':by_at[r['at']]} for r in examples)
            sources[ticker]={'historical':old['source'],'additional':source,'lot':old['lot']}
            self.stdout.write(f'STRUCTURE {ticker}: {len(examples)} decision rows, {len(parsed["pivots"])} confirmed extrema')
        groups=ai.split(rows)
        results={}
        for key in structure.VARIANTS:
            results[key]={p:replay(values,key) for p,values in groups.items()}
            for p,r in results[key].items():
                self.stdout.write(f'{key} {p}: signals={r["signals"]}, trades={r["base"]["trades"]}, unknown={r["base"]["unknown_trades"]}, return={r["base"]["return_pct"]:+.4f}% / x2={r["double_cost"]["return_pct"]:+.4f}%')
        passing={k:all(results[k][p][s]['trades']>=30 and results[k][p][s]['unknown_trades']==0
                         and results[k][p][s]['return_pct']>0 for p in ('test','additional') for s in ('base','double_cost'))
                 for k in structure.VARIANTS}
        at=datetime.now(timezone.utc)
        saved={'version':structure.VERSION,'at':at.isoformat(),'rules':structure.RULES,'variants':structure.VARIANTS,
            'source_sha256':source_hashes(),'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),
            'inputs':sources,'periods':ai.PERIODS,'results':results,'paper_screen':passing,'entry_allowed':False,
            'note':'Three deterministic structure hypotheses, not newly trained AI. Reused dates, separate stock accounts; no independent future proof or book history.'}
        saved['artifact_sha256']=digest(saved)
        # Portable report, without price arrays/private account information.
        output=Path(settings.BASE_DIR)/'dashboard'/'ai_models'
        output.mkdir(parents=True,exist_ok=True)
        path=output/f'market-structure-study-v1-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,saved)
        self.stdout.write(f'SAVED STRUCTURE: {path}; paper_screen={passing}; entry_allowed=False')
