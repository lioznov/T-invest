"""Fixed, causal evening→next-morning Ridge research model. Never places orders."""
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev

VERSION = 'morning_ridge_v1'
MSK = timezone(timedelta(hours=3))
TICKERS = ('SBER', 'GAZP', 'LKOH', 'NVTK', 'GMKN', 'ROSN', 'YDEX')
FEATURES = ('return_1d', 'return_5d', 'return_20d', 'day_body', 'afternoon_return',
            'day_range', 'close_position', 'relative_volume', 'volatility_20d',
            'distance_ma20', 'last_bar_body')+tuple('ticker_'+t for t in TICKERS)
PERIODS = {'train': ('2026-03-30', '2026-08-01'), 'calibration': ('2026-08-01', '2026-09-01'),
           'test': ('2026-09-01', '2026-10-03'), 'later': ('2026-10-03', '2026-10-21')}
RULES = {
    'cutoff': '17:45 МСК', 'entry': '18:00 МСК', 'target': '10:00 МСК следующего дня',
    'days': 'Пн–чт; пятница/выходные не прогнозируются',
    'target_definition': 'next calendar day exactly 10:00 EXCHANGE 15m bar open, never next available date',
    'features_available': 'only completed 10:00–17:30 bars, last closes at17:45; 20 prior complete sessions',
    'label': '100*(next10:00open/17:45knownclose-1); available only at10:15',
    'ridge_alpha': 10., 'warmup_days': 20, 'interval_coverage': .8,
    'minimum_train_rows': 200, 'minimum_train_dates': 30, 'minimum_calibration_rows': 50,
    'minimum_calibration_dates': 10, 'feature_clip': 8.,
    'fit': 'one fixed pooled Ridge; train-only standardisation; intercept unpenalised; no parameter search',
    'baseline': 'zero change from cutoff price; second comparator train-only mean return',
    'splits': 'all stocks share chronological date boundaries; label_end strictly before end; no shuffling',
    'interval': '80% marginal absolute-residual interval from August, not probability of profit or a guarantee',
    'direction': 'up/down only if whole calibrated interval is above/below zero, otherwise uncertain',
    'paper_policy': 'separate point policy: predicted change from17:45anchor >0.4%; buy18:00proxy, sellnext10:00proxy',
    'paper_threshold_pct': .4, 'fee_bps_side': 5., 'slippage_bps_side': 5.,
    'paper_capital': 100000., 'max_stock_allocation': .25,
    'paper_sizing': 'equal budgets among selected stocks, <=25%/stock, whole lots, cash only, no shorts',
    'unknowns': 'missing entry/target retained; selected unknown poisons exact account curve thereafter',
    'drawdown': 'at morning exits only; NOT intranight worst drawdown; no stops or borrowing',
    'entry_allowed': False,
    'limitations': 'previously examined history and survivor universe; no point-in-time news/book/dividend features; '
                   'no dividend cash payments, taxes, auction queue or verified fills; not prospective evidence',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def source_hashes():
    root = Path(__file__).resolve().parent
    names = ('morning_model.py', 'morning_inputs.py', 'management/commands/train_morning_forecast.py',
             'management/commands/forecast_morning.py', 'market_provider.py', 'market_stream_provider.py',
             'management/commands/study_pattern_flow.py', 'management/commands/study_opening_activity.py',
             'management/commands/verify_neural_trader.py', 'management/commands/study_diverse_long.py',
             'signals.py', 'method_study.py')
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}


def clock(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK)


def validate(candles):
    previous = None
    for b in candles:
        prices = (b.open, b.high, b.low, b.close)
        if (not isinstance(b.time, datetime) or b.time.tzinfo is None
                or previous is not None and b.time <= previous
                or b.time.second != 0 or b.time.microsecond != 0 or b.time.minute % 15
                or not all(type(p) in (float, int) and math.isfinite(p) and p > 0 for p in prices)
                or type(b.volume) is not int or b.volume < 0
                or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high):
            raise ValueError('Invalid or duplicate EXCHANGE candle')
        previous = b.time


def build_examples(candles, ticker, lot):
    if ticker not in TICKERS or type(lot) is not int or lot < 1:
        raise ValueError('Unsupported stock or lot')
    validate(candles)
    by_time = {b.time.astimezone(MSK): b for b in candles}
    days = sorted({at.date() for at in by_time})
    history, rows = [], []
    coverage = {'observed_dates': len(days), 'incomplete_day_features': 0, 'warmup_dates': 0,
                'excluded_friday_weekend': 0, 'unknown_targets': 0, 'unknown_entries': 0}
    for day in days:
        times = [clock(day, 10)+timedelta(minutes=15*i) for i in range(31)]
        bars = [by_time.get(at) for at in times]
        if any(b is None or b.volume <= 0 for b in bars):
            coverage['incomplete_day_features'] += 1
            continue
        p = bars[-1].close
        today = {'close': p, 'volume': sum(b.volume for b in bars), 'date': day}
        if len(history) < 20:
            coverage['warmup_dates'] += 1
        elif day.weekday() <= 3:
            prior = history[-20:]
            # Prior observations are full matching-cutoff sessions, never future whole-day data.
            vol_ref = mean(d['volume'] for d in prior)
            high, low = max(b.high for b in bars), min(b.low for b in bars)
            returns = [(b['close']/a['close']-1)*100 for a,b in zip(prior, prior[1:])]
            x = [(p/history[-n]['close']-1)*100 for n in (1, 5, 20)]+[
                (p/bars[0].open-1)*100, (p/bars[-8].open-1)*100,
                (high-low)/p*100, (p-low)/(high-low) if high>low else .5,
                math.log(today['volume']/vol_ref), pstdev(returns),
                (p/mean(d['close'] for d in prior)-1)*100, (p/bars[-1].open-1)*100,
            ]+[float(t == ticker) for t in TICKERS]
            at = clock(day, 17, 45)
            target_at = clock(day+timedelta(days=1), 10)
            entry = by_time.get(clock(day, 18))
            target = by_time.get(target_at)
            entry_price = entry.open if entry is not None and entry.volume > 0 else None
            actual = target.open if target is not None and target.volume > 0 else None
            coverage['unknown_entries'] += entry_price is None
            coverage['unknown_targets'] += actual is None
            rows.append({'ticker': ticker, 'lot': lot, 'at': at.isoformat(), 'target_at': target_at.isoformat(),
                'label_end': (target_at+timedelta(minutes=15)).isoformat(), 'anchor_price': p,
                'entry_price': entry_price, 'actual_price': actual, 'x': x,
                'y': (actual/p-1)*100 if actual is not None else None,
                'reason': 'target_missing_or_no_volume' if actual is None else 'ok'})
        else:
            coverage['excluded_friday_weekend'] += 1
        history.append(today)
    return rows, coverage


def split(rows):
    groups = {name: [] for name in PERIODS}
    for row in rows:
        at, until = datetime.fromisoformat(row['at']), datetime.fromisoformat(row['label_end'])
        for name, (first, last) in PERIODS.items():
            begin = datetime.fromisoformat(first).replace(tzinfo=MSK)
            end = datetime.fromisoformat(last).replace(tzinfo=MSK)
            if begin <= at < end and until < end:
                groups[name].append(row)
    return groups


def fit(train, calibration):
    import numpy as np
    for row in list(train)+list(calibration):
        if (len(row['x']) != len(FEATURES) or not all(type(v) in (float,int) and math.isfinite(v) for v in row['x'])
                or row['y'] is not None and (type(row['y']) not in (float,int) or not math.isfinite(row['y']))):
            raise ValueError('Invalid training feature/target')
    known = [r for r in train if r['y'] is not None]
    cal = [r for r in calibration if r['y'] is not None]
    if (len(known)<RULES['minimum_train_rows'] or len({r['at'][:10] for r in known})<RULES['minimum_train_dates']
            or len(cal)<RULES['minimum_calibration_rows'] or len({r['at'][:10] for r in cal})<RULES['minimum_calibration_dates']):
        raise ValueError('Недостаточно независимых дат обучения или калибровки.')
    if max(datetime.fromisoformat(r['label_end']) for r in known) >= min(datetime.fromisoformat(r['at']) for r in cal):
        raise ValueError('Calibration must follow every known training label; overlap is forbidden.')
    x = np.array([r['x'] for r in known], dtype=float)
    y = np.array([r['y'] for r in known], dtype=float)
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-12] = 1.
    z = np.clip((x-center)/scale, -8, 8)
    z = np.column_stack((np.ones(len(z)), z))
    penalty = np.eye(z.shape[1])*RULES['ridge_alpha']; penalty[0,0] = 0
    weights = np.linalg.solve(z.T@z+penalty, z.T@y)
    model = {'center': center.tolist(), 'scale': scale.tolist(), 'weights': weights.tolist(),
             'interval_abs_pct': 0., 'train_mean_pct': float(y.mean())}
    errors = sorted(abs(r['y']-predict(model, r['x'])) for r in cal)
    rank = min(len(errors), math.ceil((len(errors)+1)*RULES['interval_coverage']))
    model['interval_abs_pct'] = float(errors[rank-1])
    return model


def predict(model, x):
    if len(x) != len(FEATURES) or not all(type(v) in (float,int) and math.isfinite(v) for v in x):
        raise ValueError('Invalid feature vector')
    n = len(FEATURES)
    if (len(model['center']) != n or len(model['scale']) != n or len(model['weights']) != n+1
            or not all(type(v) in (float,int) and math.isfinite(v)
                       for key in ('center','scale','weights') for v in model[key])
            or any(s <= 0 for s in model['scale'])):
        raise ValueError('Invalid ridge parameters')
    z = [max(-8., min(8., (v-c)/s)) for v,c,s in zip(x, model['center'], model['scale'])]
    return model['weights'][0]+sum(w*v for w,v in zip(model['weights'][1:], z))


def forecast(model, row):
    change = predict(model, row['x'])
    q = model['interval_abs_pct']
    p = row['anchor_price']
    return {'as_of': row['at'], 'target_at': row['target_at'], 'anchor_price': p,
            'predicted_price': p*(1+change/100), 'lower_price': max(0., p*(1+(change-q)/100)),
            'upper_price': p*(1+(change+q)/100), 'predicted_change_pct': change,
            'direction': 'up' if change-q > 0 else 'down' if change+q < 0 else 'uncertain',
            'historical': True, 'actual_price': row['actual_price'],
            'note': 'Относительно известной цены17:45, не прибыли покупки18:00. Не рекомендация покупки/шорта.'}


def paper(rows, model, *, multiplier=1, always=False):
    days = defaultdict(list)
    for row in rows:
        if always or predict(model, row['x']) > RULES['paper_threshold_pct']:
            days[row['at'][:10]].append(row)
    cash, peak, dd = RULES['paper_capital'], RULES['paper_capital'], 0.
    trades, unknown, halted, drifts = 0, 0, False, []
    fee, slip = multiplier*.0005, multiplier*.0005
    for selected in (days[day] for day in sorted(days)):
        missing = [r for r in selected if r['entry_price'] is None or r['actual_price'] is None]
        unknown += len(missing)
        if halted or missing:
            halted = True
            continue  # Never release fictitious capital on following dates.
        budget = cash*min(.25, 1/len(selected))
        pnl = 0.
        for r in selected:
            entry = r['entry_price']*(1+slip)
            qty = math.floor(budget/(entry*(1+fee)*r['lot']))*r['lot']
            if qty <= 0:
                continue
            trades += 1
            pnl += qty*(r['actual_price']*(1-slip)*(1-fee)-entry*(1+fee))
            drifts.append((r['entry_price']/r['anchor_price']-1)*100)
        cash += pnl
        peak = max(peak, cash); dd = max(dd, (peak-cash)/peak)
    return {'trades': trades, 'selected_candidates': sum(map(len,days.values())), 'unknown_trades': unknown,
            'status': 'incomplete' if halted else 'ok',
            'net_return_pct': (cash/RULES['paper_capital']-1)*100 if trades and not halted else None,
            'max_drawdown_pct': dd*100 if trades and not halted else None,
            'mean_anchor_to_entry_pct': mean(drifts) if drifts else None}


def evaluate(rows, model):
    known = [r for r in rows if r['y'] is not None]
    preds = [predict(model, r['x']) for r in known]
    errors = [abs(p-r['y']) for r,p in zip(known,preds)]
    signs = [(p>0) == (r['y']>0) for r,p in zip(known,preds) if p != 0 and r['y'] != 0]
    interval = model['interval_abs_pct']
    base, stress = paper(rows, model), paper(rows, model, multiplier=2)
    base['stress_return_pct'] = stress['net_return_pct']
    base['stress_trades'] = stress['trades']
    base['stress_unknown_trades'] = stress['unknown_trades']
    return {'samples': len(known), 'eligible_forecasts': len(rows), 'unknown_targets': len(rows)-len(known),
            'days': len({r['at'][:10] for r in known}),
            'mae_bps': mean(errors)*100 if errors else None,
            'baseline_mae_bps': mean(abs(r['y']) for r in known)*100 if known else None,
            'mean_baseline_mae_bps': mean(abs(r['y']-model['train_mean_pct']) for r in known)*100 if known else None,
            'direction_accuracy_pct': mean(signs)*100 if signs else None,
            'direction_nonzero_samples': len(signs),
            'coverage_pct': mean(e<=interval for e in errors)*100 if errors else None,
            'interval_width_bps': interval*200,
            'abstention_pct': mean(abs(p)<=interval for p in preds)*100 if preds else None,
            'paper': base, 'always_buy': paper(rows, model, always=True),
            'note': 'Ранее изученная история. Бумажный порог относится к точке, направление — к интервалу; это разные оценки. '
                    'Просадка только по утренним выходам; дивиденды и реальные исполнения не учтены.'}
