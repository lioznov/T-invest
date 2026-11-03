"""Separate, fixed multi-year experiments. Never changes the frozen old models."""
import hashlib
import math
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev

from . import morning_model as m
from .close_timing_study import VARIANTS, required_starts, at_clock

VERSION = 'multiyear_morning_v1'
PERIODS = {'train':('2000-01-01','2024-01-01'), 'calibration':('2024-01-01','2025-01-01'),
           'test':('2025-01-01','2026-01-01'), 'later':('2026-01-01','2026-10-23')}
NOTE = ('Многолетняя ретроспективная проверка, не гарантия доходности и не текущая торговая команда. '
        '2025–2026 не участвуют в обучении; часть этой истории уже изучалась в проекте. '
        'Старые модели, биржевые заявки и Telegram не изменены.')
RULES = {
    'universe':m.TICKERS,
    'data':'all available verified EXCHANGE DAY from advertisedfirstdailydate and15m fromadvertisedfirstminutedate '
           'through2026-10-23T00:00UTC; preserve weekends, immutable per-request source snapshots',
    'scope':'available history of current instrumentUIDs, not all stock exchange records or all delisted securities; '
            'firstminute is advertised coverage hint, not proof of earliest possible15m history',
    'daily':'separate observational fullDAYclose→next observedDAYopen; not auction prices, not10:00; '
            'fullbar known only afterbucket+24h; no assumed buy at alreadyknownclose; no tradingP&L',
    'intraday':'same fixed17:45/18:00/22:45 clocks and18-dimensional formulas; '
               'positive complete selected windows, own20previouscompleteWEEKDAYreference dates per clock',
    'history_change':'own past reference dates, not sharedlatecompleteness: years before evening trading remain '
                     'available to earlier clocks; comparisons have different train coverage, not a pure isolated data-size effect',
    'target':'intraday EXACTnextcalendarday10:00, Mon–Thu origins, entry15minaftercutoff; no missing-date shifting',
    'fit':'one fixed Ridgealpha10 per each of4models; train-onlystandardisation; no parameter search or winner promotion',
    'periods':PERIODS,
    'calibration':'2024only absolute-residual80% interval; report later empiricalcoverage, not confidence of profit',
    'paper':'intraday only same pointthreshold>0.4%,100000RUBcash,<=25%/stock,wholecurrentlots,longonly; '
            'fee5bps+slip5bps per side, stressbothx2. Unknownselectedoutcomes poisonexactcashcurve.',
    'limitations':'provider may adjust split history, current lot sizes not historical; no dividends/taxes/truefills/overnightworstdrawdown; '
                  'calendar/regimes changed; no point-in-time news/book; no claim of independent prospective profitability',
    'entry_allowed':False,
}
digest = m.digest


def source_hashes():
    root = Path(__file__).resolve().parent
    names = ('multiyear_model.py','multiyear_daily.py','multiyear_inputs.py','close_timing_study.py',
             'management/commands/fetch_multiyear_history.py','management/commands/train_multiyear_morning.py',
             'morning_service.py')
    return {**m.source_hashes(), **{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}}


def build_intraday(candles, ticker, lot):
    if ticker not in m.TICKERS or type(lot) is not int or lot < 1:
        raise ValueError('Unsupported stock/lot')
    m.validate(candles)
    by_time = {b.time.astimezone(m.MSK):b for b in candles}
    days = sorted({at.date() for at in by_time})
    result, coverage = {}, {}
    for variant in VARIANTS:
        key = variant['id']; history=[]; rows=[]
        quality = {'observed_dates':len(days),'weekend_dates_excluded':0,'incomplete_feature_dates':0,
                   'warmup_dates':0,'unknown_entries':0,'unknown_targets':0,'examples':0}
        for day in days:
            if day.weekday() >= 5:
                quality['weekend_dates_excluded'] += 1
                continue
            bars = [by_time.get(at) for at in required_starts(day,variant)]
            if any(b is None or b.volume <= 0 for b in bars):
                quality['incomplete_feature_dates'] += 1
                continue
            p = bars[-1].close
            today = {'close':p,'volume':sum(b.volume for b in bars)}
            if len(history) < 20:
                quality['warmup_dates'] += 1
            elif day.weekday() <= 3:
                prior=history[-20:]
                high,low=max(b.high for b in bars),min(b.low for b in bars)
                returns=[(b['close']/a['close']-1)*100 for a,b in zip(prior,prior[1:])]
                x=[(p/prior[-n]['close']-1)*100 for n in (1,5,20)]+[
                    (p/bars[0].open-1)*100,(p/bars[-8].open-1)*100,(high-low)/p*100,
                    (p-low)/(high-low) if high>low else .5,
                    math.log(today['volume']/mean(d['volume'] for d in prior)),pstdev(returns),
                    (p/mean(d['close'] for d in prior)-1)*100,(p/bars[-1].open-1)*100,
                ]+[float(t==ticker) for t in m.TICKERS]
                at=at_clock(day,variant['cutoff']); target_at=m.clock(day+timedelta(days=1),10)
                entry=by_time.get(at_clock(day,variant['entry'])); target=by_time.get(target_at)
                ep=entry.open if entry is not None and entry.volume>0 else None
                actual=target.open if target is not None and target.volume>0 else None
                rows.append({'ticker':ticker,'lot':lot,'at':at.isoformat(),'target_at':target_at.isoformat(),
                    'label_end':(target_at+timedelta(minutes=15)).isoformat(),'anchor_price':p,
                    'entry_price':ep,'actual_price':actual,'x':x,
                    'y':(actual/p-1)*100 if actual is not None else None,
                    'reason':'target_missing_or_no_volume' if actual is None else 'ok'})
                quality['unknown_entries'] += ep is None
                quality['unknown_targets'] += actual is None
            history.append(today)
        quality['examples']=len(rows)
        result[key]=rows; coverage[key]=quality
    return result,coverage


def split(rows):
    groups={name:[] for name in PERIODS}
    for row in rows:
        at=datetime.fromisoformat(row['at']); until=datetime.fromisoformat(row['label_end'])
        if at.tzinfo is None or until.tzinfo is None or until<=at:
            raise ValueError('Invalid information horizon')
        for name,(first,last) in PERIODS.items():
            begin=datetime.fromisoformat(first).replace(tzinfo=m.MSK)
            end=datetime.fromisoformat(last).replace(tzinfo=m.MSK)
            if begin<=at<end and until<end:
                groups[name].append(row)
    return groups
