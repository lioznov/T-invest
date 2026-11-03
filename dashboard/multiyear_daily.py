"""Completed DAY close → next observed DAY open: research, never an entry.

The full current DAY bar is conservatively known at bucket start +24h. Its
final close is not an executable buy price at that knowledge time. Aggregate
DAY prices are not assumed to be opening/closing auction prices.
"""
import math
from datetime import timedelta, timezone
from statistics import mean, pstdev

from . import morning_model as m

VERSION = 'multiyear_daily_observational_v1'
LOOKBACK = 20
MAX_TARGET_GAP_DAYS = 4
FEATURES = (
    'return_1_observed_bar', 'return_5_observed_bars', 'return_20_observed_bars',
    'day_body', 'day_range', 'close_position', 'relative_volume_20_observed_bars',
    'volatility_20_observed_bars', 'distance_ma20_observed_bars',
    'current_open_previous_close_gap', 'previous_open_preprevious_close_gap',
)+tuple('ticker_'+ticker for ticker in m.TICKERS)
RULES = {
    'task': 'completed DAY close to next observed DAY open; observational research only',
    'knowledge_time': 'current DAY bucket start +24h; never buy at the already known final close',
    'label_available': 'next observed DAY bucket start +24h; no assumed10:00 or auction price',
    'target_gap': 'next observed record only; gap>4calendar days is unknown, never bridge to another record',
    'features': 'current completed DAY bar and exactly20 previous observed completed DAY bars; '
                'observed positive-volume weekends retained; zero-volume feature bars unavailable, not skipped over',
    'feature_units': 'price returns/range/volatility in percent, close position0..1, log volume ratio, ticker onehot',
    'missing_last_target': 'y/actual_price/target_at/target_date=None; label_end=at+24h only an unknown-row '
                           'purging placeholder, never an observed target or fabricated outcome',
    'entry_allowed': False,
    'paper': 'no executable entry and no trade-performance calculation; zero trades and null P&L',
    'limitations': 'DAY boundaries need not match exchange sessions/auctions; missing sessions not reconstructed; '
                  'adjusted historical prices, changing lots, corporate actions and dividend cash need separate treatment',
}
NOTE = ('Исследовательский прогноз от закрытия завершённой дневной свечи к открытию следующей '
        'наблюдавшейся дневной свечи. Это не цена аукциона или обязательная цена10:00. '
        'Полное закрытие известно после завершения дня: купить задним числом по нему нельзя. '
        'Исполнимого входа и расчёта торговой доходности здесь нет.')


def validate(candles):
    """Validate DAY-shaped records without inventing historical session clocks."""
    m.validate(candles)
    for previous, current in zip(candles, candles[1:]):
        seconds = (current.time.astimezone(timezone.utc)-previous.time.astimezone(timezone.utc)).total_seconds()
        if seconds < 86400 or seconds % 86400:
            raise ValueError('Expected distinct daily buckets on a common UTC day grid')


def build_examples(candles, ticker, lot):
    if ticker not in m.TICKERS or type(lot) is not int or lot < 1:
        raise ValueError('Unsupported stock or lot')
    if len(FEATURES) != len(m.FEATURES):
        raise ValueError('Incompatible frozen Ridge feature dimension')
    validate(candles)
    rows = []
    coverage = {'observed_dates': len(candles), 'observed_weekend_dates': 0,
                'warmup_dates': 0, 'unavailable_feature_dates': 0, 'examples': 0,
                'unknown_targets': 0, 'missing_next_records': 0, 'target_gap_exceeds_four_days': 0,
                'zero_volume_targets': 0, 'unknown_entries': 0}
    for index, bar in enumerate(candles):
        coverage['observed_weekend_dates'] += bar.time.astimezone(m.MSK).weekday() >= 5
        if index < LOOKBACK:
            coverage['warmup_dates'] += 1
            continue
        prior = candles[index-LOOKBACK:index]
        if bar.volume <= 0 or any(b.volume <= 0 for b in prior):
            # Never replace an observed zero-volume feature bar by an older one.
            coverage['unavailable_feature_dates'] += 1
            continue
        p, span = bar.close, bar.high-bar.low
        changes = [(b.close/a.close-1)*100 for a,b in zip(prior,prior[1:])]
        x = [(p/prior[-n].close-1)*100 for n in (1,5,20)]+[
            (p/bar.open-1)*100, span/p*100, (p-bar.low)/span if span>0 else .5,
            math.log(bar.volume/mean(b.volume for b in prior)), pstdev(changes),
            (p/mean(b.close for b in prior)-1)*100,
            (bar.open/prior[-1].close-1)*100,
            (prior[-1].open/prior[-2].close-1)*100,
        ]+[float(value == ticker) for value in m.TICKERS]
        if not all(math.isfinite(value) for value in x):
            raise ValueError('Non-finite daily features')
        at = bar.time+timedelta(days=1)
        target = candles[index+1] if index+1 < len(candles) else None
        gap = ((target.time.astimezone(timezone.utc)-bar.time.astimezone(timezone.utc)).days
               if target is not None else None)
        if target is None:
            reason = 'next_observed_bar_missing'; coverage['missing_next_records'] += 1
        elif gap > MAX_TARGET_GAP_DAYS:
            reason = 'target_gap_exceeds_four_days'; coverage['target_gap_exceeds_four_days'] += 1
        elif target.volume <= 0:
            reason = 'target_zero_volume'; coverage['zero_volume_targets'] += 1
        else:
            reason = 'ok'
        actual = target.open if reason == 'ok' else None
        until = target.time+timedelta(days=1) if target is not None else at+timedelta(days=1)
        rows.append({'ticker':ticker, 'lot':lot, 'at':at.isoformat(),
            'origin_date':bar.time.astimezone(m.MSK).date().isoformat(),
            'target_date':target.time.astimezone(m.MSK).date().isoformat() if target is not None else None,
            'target_at':target.time.isoformat() if target is not None else None,
            'target_gap_days':gap, 'label_end':until.isoformat(), 'label_end_is_placeholder':target is None,
            'anchor_price':p, 'entry_price':None, 'actual_price':actual, 'x':x,
            'y':(actual/p-1)*100 if actual is not None else None, 'reason':reason, 'entry_allowed':False})
        coverage['examples'] += 1
        coverage['unknown_targets'] += actual is None
        coverage['unknown_entries'] += 1
    return rows, coverage


def evaluate(rows, model):
    """Regression diagnostics only: never call the trading simulator."""
    interval, train_mean = model['interval_abs_pct'], model['train_mean_pct']
    if (type(interval) not in (float,int) or not math.isfinite(interval) or interval < 0
            or type(train_mean) not in (float,int) or not math.isfinite(train_mean)):
        raise ValueError('Invalid daily calibration or mean baseline')
    known, preds = [], []
    for row in rows:
        prediction = m.predict(model,row['x'])
        target = row['y']
        if target is not None:
            if type(target) not in (float,int) or not math.isfinite(target):
                raise ValueError('Invalid daily target')
            known.append(row); preds.append(prediction)
    errors = [abs(prediction-row['y']) for row,prediction in zip(known,preds)]
    directions = [(prediction>0)==(row['y']>0) for row,prediction in zip(known,preds)
                  if prediction != 0 and row['y'] != 0]
    paper = {'trades':0, 'selected_candidates':0, 'unknown_trades':0, 'status':'ok',
             'net_return_pct':None, 'max_drawdown_pct':None, 'stress_return_pct':None,
             'stress_trades':0, 'stress_unknown_trades':0, 'mean_anchor_to_entry_pct':None,
             'note':'Не рассчитывается: известная окончательная цена закрытия не является исполнимым входом.'}
    return {'samples':len(known), 'eligible_forecasts':len(rows), 'unknown_targets':len(rows)-len(known),
        'days':len({row['at'][:10] for row in known}),
        'mae_bps':mean(errors)*100 if errors else None,
        'baseline_mae_bps':mean(abs(row['y']) for row in known)*100 if known else None,
        'mean_baseline_mae_bps':mean(abs(row['y']-train_mean) for row in known)*100 if known else None,
        'direction_accuracy_pct':mean(directions)*100 if directions else None,
        'direction_nonzero_samples':len(directions),
        'coverage_pct':mean(error<=interval for error in errors)*100 if errors else None,
        'interval_width_bps':interval*200,
        'abstention_pct':mean(abs(prediction)<=interval for prediction in preds)*100 if preds else None,
        'paper':paper, 'entry_allowed':False, 'note':NOTE}
