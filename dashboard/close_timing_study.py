"""Fixed clock comparison; only retrospective research, never a trade signal.

Do not edit the frozen morning v1 model. A separate control uses shared *past*
weekday reference dates, so an earlier forecast never depends on tonight's data.
"""
import hashlib
import math
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev

from . import morning_model as m

VERSION = 'close_timing_v1'
VARIANTS = (
    {'id':'control', 'cutoff':'17:45', 'entry':'18:00', 'label':'Прежнее время · новый контроль'},
    {'id':'main_hour', 'cutoff':'18:00', 'entry':'18:15', 'label':'Около часа до конца основной сессии'},
    {'id':'evening_hour', 'cutoff':'22:45', 'entry':'23:00', 'label':'Около часа до конца вечерней сессии'},
)
NOTE = ('Сравнение заранее заданных часов на ранее изученной истории. Это не новые живые сигналы '
        'и не доказательство будущей доходности. Старая модель 17:45 не заменена.')
PAIRED_NOTE = ('Парная таблица использует общие тикеры/даты, где доступны признаки всех трёх часов; '
               'неизвестные будущие входы/исходы не исключаются. Это условное ретроспективное сравнение '
               'доступных окон, не правило отбора акций в 17:45. Модели не переобучаются на парной таблице.')
RULES = {
    'hypothesis':'Does later same-day information improve a fixed next10:00 forecast? Three prespecified clocks only.',
    'schedule_basis':'MOEX s1167 checked2026-10-23: current main auction18:55,end18:59:30; eveningend23:49:59. '
                     'Fixed research clocks, not a reconstructed historical instrument schedule.',
    'schedule_url':'https://www.moex.com/s1167',
    'target':'next calendar day exactly10:00 EXCHANGE15mbaropen; known after10:15; Mon–Thu origins only',
    'feature_windows':'control10:00..17:30starts; main10:00..17:45starts; '
                      'evening10:00..18:15 plus19:15..22:30starts; all must be complete positive-volume15mbars',
    'excluded_transition_starts':['18:30','18:45','19:00'],
    'reference_history':'20 strictly previous WEEKDAYdates with all three fixed feature windows complete; '
                        'only update after constructing current date rows; never use current late availability for early row',
    'features':'same18-dimensional formula as morningv1, but selected-window volume/range and each own-cutoffclose; '
               'last8bars return; same set of shared prior dates. No news, historical book or extra tuned features.',
    'fit':'same frozen morning_model.fit: Ridgealpha10, training beforeAugust, Augustcalibration; no search',
    'split':'same frozen date boundaries and strict label_end purge; no random shuffle',
    'control_difference':'new17:45control excludes weekend reference sessions and shares prior complete weekdays; '
                         'not numerically identical to published morningv1 and does not replace it',
    'paper':'same frozen pointprediction>0.4% from owncutoffanchor, enter15minlater, exitnext10:00; '
            '100000RUBcommoncash,<=25%/stock,wholelots,longonly;fee5bps+slip5bps per side, stressbothx2',
    'paper_unknowns':'selected missing entry/outcome poisons whole subsequent exact account result, never erased',
    'paired':PAIRED_NOTE,
    'selection':'report all clocks and both periods; do not promote a best clock after seeing these reused results',
    'limitations':'no historical schedule, overnight worst drawdown, dividend payments, taxes or actual fills; '
                  'allseven stocks and history previously studied; no prospective outcome claims',
    'entry_allowed':False,
}

digest = m.digest


def source_hashes():
    root = Path(__file__).resolve().parent
    extra = ('close_timing_study.py', 'management/commands/study_close_timing.py', 'morning_service.py')
    return {**m.source_hashes(), **{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in extra}}


def at_clock(day, value):
    hour, minute = map(int, value.split(':'))
    return m.clock(day, hour, minute)


def required_starts(day, variant):
    if variant['id'] == 'evening_hour':
        # Omit transition/auction-contaminated intervals by fixed design, not by P&L.
        return ([m.clock(day,10)+timedelta(minutes=15*i) for i in range(34)]
                +[m.clock(day,19,15)+timedelta(minutes=15*i) for i in range(14)])
    count = int((at_clock(day,variant['cutoff'])-m.clock(day,10)).total_seconds()/900)
    return [m.clock(day,10)+timedelta(minutes=15*i) for i in range(count)]


def build_examples(candles, ticker, lot):
    if ticker not in m.TICKERS or type(lot) is not int or lot <= 0:
        raise ValueError('Unsupported stock/lot')
    m.validate(candles)
    by_time = {b.time.astimezone(m.MSK):b for b in candles}
    days = sorted({at.date() for at in by_time})
    history, rows = [], {v['id']:[] for v in VARIANTS}
    coverage = {'observed_weekdays':0, 'shared_reference_dates':0, 'weekend_dates_excluded':0,
                'variants':{v['id']:{'incomplete_feature_dates':0,'warmup_dates':0,'examples':0,
                                     'unknown_entries':0,'unknown_targets':0} for v in VARIANTS}}
    for day in days:
        if day.weekday() >= 5:
            coverage['weekend_dates_excluded'] += 1
            continue
        coverage['observed_weekdays'] += 1
        windows = {v['id']:[by_time.get(at) for at in required_starts(day,v)] for v in VARIANTS}
        complete = {key:all(b is not None and b.volume>0 for b in bars) for key,bars in windows.items()}
        for variant in VARIANTS:
            key = variant['id']; quality = coverage['variants'][key]
            if not complete[key]:
                quality['incomplete_feature_dates'] += 1
                continue
            if len(history) < 20:
                quality['warmup_dates'] += 1
                continue
            if day.weekday() > 3:
                continue
            bars = windows[key]; p = bars[-1].close
            prior = [d[key] for d in history[-20:]]
            high, low = max(b.high for b in bars), min(b.low for b in bars)
            returns = [(b['close']/a['close']-1)*100 for a,b in zip(prior,prior[1:])]
            x = [(p/prior[-n]['close']-1)*100 for n in (1,5,20)]+[
                (p/bars[0].open-1)*100, (p/bars[-8].open-1)*100, (high-low)/p*100,
                (p-low)/(high-low) if high>low else .5,
                math.log(sum(b.volume for b in bars)/mean(d['volume'] for d in prior)),
                pstdev(returns), (p/mean(d['close'] for d in prior)-1)*100,
                (p/bars[-1].open-1)*100,
            ]+[float(t==ticker) for t in m.TICKERS]
            at = at_clock(day,variant['cutoff']); target_at = m.clock(day+timedelta(days=1),10)
            entry, target = by_time.get(at_clock(day,variant['entry'])), by_time.get(target_at)
            entry_price = entry.open if entry is not None and entry.volume>0 else None
            actual = target.open if target is not None and target.volume>0 else None
            rows[key].append({'ticker':ticker,'lot':lot,'at':at.isoformat(),'target_at':target_at.isoformat(),
                'label_end':(target_at+timedelta(minutes=15)).isoformat(), 'anchor_price':p,
                'entry_price':entry_price, 'actual_price':actual, 'x':x,
                'y':(actual/p-1)*100 if actual is not None else None,
                'reason':'target_missing_or_no_volume' if actual is None else 'ok'})
            quality['examples'] += 1
            quality['unknown_entries'] += entry_price is None
            quality['unknown_targets'] += actual is None
        # Only earlier dates enter any forecast's history. Today's evening may be
        # absent in a17:45 prefix without changing today's17:45 row or its features.
        if all(complete.values()):
            history.append({key:{'close':bars[-1].close,'volume':sum(b.volume for b in bars)}
                            for key,bars in windows.items()})
            coverage['shared_reference_dates'] += 1
    return rows, coverage


def paired(rows_by_variant):
    def key(row):
        at = datetime.fromisoformat(row['at'])
        if at.tzinfo is None:
            raise ValueError('Naive forecast time')
        return row['ticker'], at.astimezone(m.MSK).date()
    expected = {v['id'] for v in VARIANTS}
    if set(rows_by_variant) != expected:
        raise ValueError('All three clocks are required')
    keys = {}
    for name,rows in rows_by_variant.items():
        keys[name] = {key(row) for row in rows}
        if len(keys[name]) != len(rows):
            raise ValueError('Duplicate ticker/date forecast')
    common = set.intersection(*keys.values())
    return {name:sorted((r for r in rows if key(r) in common),key=lambda r:(r['at'],r['ticker']))
            for name,rows in rows_by_variant.items()}
