"""One locked, later historical period; never fits or adjusts the network."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import neural_trader as nn
from dashboard.signals import Candle, MSK
from dashboard.method_study import validate_candles
from dashboard.neural_service import load_model
from dashboard.management.commands.train_neural_trader import source_hashes as training_hashes, evaluate
from dashboard.management.commands.train_market_ai import digest
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable
from dashboard.management.commands.study_opening_activity import extension

START = datetime(2026,10,13,tzinfo=MSK).astimezone(timezone.utc)
END = datetime(2026,10,17,tzinfo=MSK).astimezone(timezone.utc)


def source_hashes():
    from dashboard import market_stream_provider
    return {**training_hashes(), 'verification_command':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'exchange_adapter':hashlib.sha256(Path(market_stream_provider.__file__).read_bytes()).hexdigest()}


def freeze(directory, model):
    spec = {'version':nn.VERSION,'model_id':model['artifact_sha256'],'start':START.isoformat(),'end_exclusive':END.isoformat(),
            'tickers':model['tickers'],'source_sha256':source_hashes(),
            'method':'one later historical check, not prospective; no fitting, calibration, threshold search or changed trade rules'}
    path = directory/'neural-check-protocol-v1.json'
    directory.mkdir(exist_ok=True,parents=True)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != spec:
            raise CommandError('Verification already locked to another identity; original preserved')
    else:
        write_immutable(path,{'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec})
    return path


def load_extension(directory, ticker, instrument, fetch=False):
    path = directory/f'neural-check-input-{ticker}-20261017.json'
    request = {'from':START.isoformat(),'to_exclusive':END.isoformat(),'minutes':15,'source':'CANDLE_SOURCE_EXCHANGE'}
    if not path.exists():
        if not fetch:
            raise CommandError('New historical candles missing; use --fetch for read-only acquisition')
        if datetime.now(timezone.utc)<END:
            raise CommandError('Registered verification period has not finished')
        from dashboard.market_provider import open_client, error_message
        from dashboard.market_stream_provider import exchange_candles
        from t_tech.invest.schemas import CandleInterval
        try:
            with open_client() as client:
                candles = exchange_candles(client,instrument['uid'],START,END,CandleInterval.CANDLE_INTERVAL_15_MIN,15)
        except Exception as exc:
            raise CommandError(error_message(exc)) from None
        if not candles:
            raise CommandError('No verified exchange candles in new period')
        write_immutable(path,{'instrument':instrument,'request':request,'fetched_at':datetime.now(timezone.utc).isoformat(),
                    'candles':[{'at':c.time.isoformat(),**{k:getattr(c,k) for k in ('open','high','low','close','volume')}} for c in candles]})
    if path.resolve().parent!=directory.resolve() or path.stat().st_size>4_000_000:
        raise CommandError('Invalid input path/size')
    raw = path.read_bytes()
    saved = json.loads(raw)
    if saved['request']!=request or saved['instrument']!=instrument:
        raise CommandError('New history identity mismatch')
    candles = []
    for row in saved['candles']:
        at = datetime.fromisoformat(row['at'])
        if at.tzinfo is None or not START<=at or at+timedelta(minutes=15)>END:
            raise CommandError('Candle outside registered verification interval')
        candles.append(Candle(at,*(row[k] for k in ('open','high','low','close','volume'))))
    if not candles:
        raise CommandError('Empty extension')
    validate_candles(candles)
    return candles, {'file':path.name,'sha256':hashlib.sha256(raw).hexdigest(),'bars':len(candles)}


class Command(BaseCommand):
    help = 'Verify trained neural model on Oct13–16 exchange history; optional read-only SDK fetch, no retraining or orders.'

    def add_arguments(self,parser):
        parser.add_argument('--fetch',action='store_true')

    def handle(self,*args,**options):
        model = load_model()
        if model is None:
            raise CommandError('Train and validate a neural artifact first')
        directory = Path(settings.BASE_DIR)/'data'
        protocol = freeze(directory,model)
        rows, inputs = [], {}
        logger = logging.getLogger('t_tech.invest')
        level = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            for ticker in model['tickers']:
                old = acquire(directory,ticker,fetch=False)
                if not all(old['quality'][k] for k in ('exchange_source_verified','exchange_requested')):
                    raise CommandError('Unverified context')
                prior, source = extension(directory,ticker,old['saved']['instrument'],False)
                new, new_source = load_extension(directory,ticker,old['saved']['instrument'],options['fetch'])
                examples = nn.replay_rows(old['candles']+prior+new,ticker,old['lot'])
                rows.extend(r for r in examples if START<=datetime.fromisoformat(r['at'])<END
                            and datetime.fromisoformat(r['label_end'])<END)
                inputs[ticker] = {'context':old['source'],'prior':source,'new':new_source}
                self.stdout.write(f'NEURAL VERIFY INPUT {ticker}: {len(new)} new exchange candles')
        finally:
            logger.setLevel(level)
        result = evaluate(model['model'],rows)
        if result['classification']['samples']==0:
            raise CommandError('No known outcomes; no report fabricated')
        at = datetime.now(timezone.utc)
        report = {'version':nn.VERSION,'model_id':model['artifact_sha256'],'source_sha256':source_hashes(),
                  'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),'inputs':inputs,
                  'start':START.isoformat(),'end_exclusive':END.isoformat(),'at':at.isoformat(),
                  'result':result,'entry_allowed':False,'independent_prospective':False,
                  'note':'Later historical dates only; four sessions do not establish persistent profitability. Model/policy unchanged.'}
        report['artifact_sha256'] = digest(report)
        path = Path(settings.BASE_DIR)/'dashboard'/'ai_models'/f'neural-check-v1-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(path,report)
        self.stdout.write(f'NEURAL NEW PERIOD: n={result["classification"]["samples"]}, trades={result["base"]["trades"]}, net={result["base"]["return_pct"]}, x2={result["double_cost"]["return_pct"]}; saved={path}')
