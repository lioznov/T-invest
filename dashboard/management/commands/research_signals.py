"""Read-only, reproducible candle research; writes no trading orders."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.market_provider import open_client, resolve, fetch_candles, error_message
from dashboard.signals import Candle, backtest


class Command(BaseCommand):
    help = 'Compare fixed candle rules on T-Bank data; save the report and input candles locally.'

    def add_arguments(self, parser):
        parser.add_argument('--tickers', nargs='+', default=['SBER','GAZP','LKOH'])
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--fee', type=float, default=5, help='Basis points per side')
        parser.add_argument('--slip', type=float, default=5, help='Basis points per side')
        parser.add_argument('--input', help='Reuse candles from an earlier data/research-*.json report')

    def handle(self, *args, **options):
        symbols=list(dict.fromkeys(t.upper() for t in options['tickers']))
        if not 1<=len(symbols)<=20 or any(not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,14}',t) for t in symbols):
            raise CommandError('Use 1–20 valid exchange tickers.')
        if not 3<=options['days']<=365 or not all(0<=options[k]<=100 for k in ('fee','slip')):
            raise CommandError('Days must be 3–365; costs must be 0–100 basis points.')
        now=datetime.now(timezone.utc)
        directory=Path(settings.BASE_DIR)/'data'
        report_days=options['days']
        if options['input']:
            source=Path(options['input']).resolve()
            if not source.is_relative_to(directory.resolve()) or not source.is_file():
                raise CommandError('Input report must be an existing file inside data/.')
            saved=json.loads(source.read_text(encoding='utf-8'))
            report_days=saved.get('days',report_days)
            rows=[]
            for row in saved.get('results',[]):
                if 'candles' not in row:
                    continue
                candles=[Candle(datetime.fromisoformat(c['time']),c['open'],c['high'],c['low'],c['close'],c['volume']) for c in row['candles']]
                rows.append({'ticker':row['ticker'],'instrument':row.get('instrument',{}),
                             'result':backtest(candles,options['fee'],options['slip']),
                             'candles':[asdict(c) for c in candles]})
            if not rows:
                raise CommandError('The input report has no reusable candles.')
        else:
            rows=None
        def run(ticker):
            try:
                with open_client() as client:
                    from t_tech.invest.schemas import CandleInterval
                    inst=resolve(client,ticker)
                    candles=fetch_candles(client,inst['uid'],now-timedelta(days=options['days']),now,CandleInterval.CANDLE_INTERVAL_15_MIN,15)
                result=backtest(candles,options['fee'],options['slip'])
                return {'ticker':ticker,'instrument':inst,'result':result,'candles':[asdict(c) for c in candles]}
            except Exception as exc:
                return {'ticker':ticker,'error':error_message(exc)}
        if rows is None:
            with ThreadPoolExecutor(max_workers=3) as pool:
                rows=list(pool.map(run,symbols))
        directory.mkdir(exist_ok=True)
        path=directory/f"research-{now.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps({'at':now,'days':report_days,'results':rows},ensure_ascii=False,default=lambda x:x.isoformat()),encoding='utf-8')
        for row in rows:
            if 'error' in row:
                self.stdout.write(f"{row['ticker']}: {row['error']}")
                continue
            r=row['result']
            if r['status']!='ok':
                self.stdout.write(f"{row['ticker']}: {r['message']}")
                continue
            self.stdout.write(f"{row['ticker']}: {r['count']} candles; holdout {r['holdout_start']} – {r['end']}; buy & hold {r['buy_hold_pct']:.2f}%")
            for s in r['strategies']:
                h=s['holdout']
                self.stdout.write(f"  {s['key']}: {h['return_pct']:.2f}%; {h['trades']} trades; drawdown {h['max_drawdown_pct']:.2f}%")
        self.stdout.write(f'Report with input candles: {path}')
