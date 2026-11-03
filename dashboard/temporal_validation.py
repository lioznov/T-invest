"""Three fixed expanding-time validations of the same 17:45 Ridge.

New research only: never mutates previous weights, protocols, or trading rules.
Actual future prices are scoring labels, never features or execution advice.
"""
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from . import morning_model as m
from . import multiyear_model as multi


VERSION = 'morning_temporal_validation_v1'
CURRENT_YEAR = {'start': '2026-01-01', 'end_exclusive': '2026-10-23'}
FOLDS = tuple({
    'id': f'test_{year}', 'label': f'Обучение до {year-1}-го · проверка {year}',
    'periods': {
        'train': {'start': '2018-03-07', 'end_exclusive': f'{year-1}-01-01'},
        'calibration': {'start': f'{year-1}-01-01', 'end_exclusive': f'{year}-01-01'},
        'test': {'start': f'{year}-01-01',
                 'end_exclusive': f'{year+1}-01-01' if year < 2026 else CURRENT_YEAR['end_exclusive']},
    },
} for year in (2024, 2025, 2026))
RULES = {
    'task': 'same causal 17:45 features, exact next-calendar-day10:00 target; Mon-Thu only; entry18:00proxy',
    'models': 'exactly three fixed Ridgealpha10 fits; expanding training windows; no shuffle/search/winner promotion',
    'features': 'unchanged multiyear_model.build_intraday control; 20 prior complete weekday reference dates',
    'leakage': 'train-only standardisation; each label_end strictly before period end; calibration follows all train labels',
    'calibration': 'one intervening year calibrates 80% absolute residual interval, not weights or profit probability',
    'common_2026': 'same ordered eligible ticker/date rows for all models; no refit or recalibration; foldC repeats its primary test',
    'metrics': 'change MAE/RMSE in bps of anchor; signed error=predicted-actual change; priceMAPE uses actual price denominator',
    'price_metrics': 'ruble MAE/RMSE separately per ticker only; actual price is label, not hindsight-perfect strategy',
    'direction_baseline': 'fixed sign of train-only mean return, evaluated on same nonzero direction denominator',
    'examples': 'last10 known SBER origins plus single largest percentage price error across all known rows; no signal/profit filtering',
    'paper': 'unchanged point>0.4%,100000RUB,<=25%/stock,whole current lots,long only;fee5bps+slip5bps/side;stressx2',
    'missing': 'unknown targets remain in eligible rows and paper selection; selected unknown poisons exact cashcurve',
    'limits': 'retrospective previously studied prices, current survivor UID/lot universe, adjusted prices possible; '
              'no dividends/taxes/orderbook/news/verified fills/overnightworstdrawdown; not prospective evidence',
    'entry_allowed': False,
}
NOTE = ('Три хронологических разбиения одной модели 17:45 на реальных сохранённых ценах. '
        'Это ретроспектива: часть истории уже изучалась. Результаты разных лет не доказывают, '
        'что более поздняя модель лучше; общий 2026 год сравнивает их на одних исходах. '
        'Ни одна модель не заменяет старую и не разрешает реальные покупки.')


def source_hashes():
    root = Path(__file__).resolve().parent
    names = ('temporal_validation.py', 'management/commands/validate_morning_splits.py')
    return {**multi.source_hashes(), **{
        name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}}


def _time(value):
    if not isinstance(value, str):
        raise ValueError('Expected timestamp text')
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError('Naive timestamp is forbidden')
    return at


def _bounds(period):
    values = []
    for name in ('start', 'end_exclusive'):
        value = period[name]
        at = datetime.fromisoformat(value)
        if len(value) != 10 or at.isoformat()[:10] != value:
            raise ValueError('Expected ISO calendar date')
        values.append(at.replace(tzinfo=m.MSK))
    if values[0] >= values[1]:
        raise ValueError('Empty or inverted period')
    return values


def select_rows(rows, period):
    begin, end = _bounds(period)
    selected, seen = [], set()
    for row in rows:
        at, until = _time(row['at']), _time(row['label_end'])
        if until <= at or row['ticker'] not in m.TICKERS:
            raise ValueError('Invalid label horizon or stock')
        key = (row['ticker'], at.astimezone(timezone.utc))
        if key in seen:
            raise ValueError('Duplicate stock/date example')
        seen.add(key)
        if begin <= at < end and until < end:
            selected.append(row)
    return sorted(selected, key=lambda row: (_time(row['at']), row['ticker']))


def group_rows(rows, periods):
    rows = list(rows)
    bounds = {key: _bounds(periods[key]) for key in ('train', 'calibration', 'test')}
    if not (bounds['train'][1] <= bounds['calibration'][0]
            and bounds['calibration'][1] <= bounds['test'][0]):
        raise ValueError('Training, calibration and test must be chronological and disjoint')
    return {key: select_rows(rows, periods[key]) for key in bounds}


def summarize(rows, model):
    """Compare raw forecasts with actual labels, including bad predictions."""
    rows = list(rows)
    report = m.evaluate(rows, model)
    train_mean = model['train_mean_pct']
    values, signed, naive, mean_errors, directions, stock_values = [], [], [], [], [], {}
    actual_counts = {'actual_up': 0, 'actual_down': 0, 'actual_zero': 0}
    for row in rows:
        if row['y'] is None:
            continue
        anchor, actual = row['anchor_price'], row['actual_price']
        if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (anchor, actual)):
            raise ValueError('Known price labels must be finite and positive')
        target = (actual/anchor-1)*100
        if not math.isclose(target, row['y'], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError('Actual price and return label disagree')
        prediction = m.predict(model, row['x'])
        predicted = anchor*(1+prediction/100)
        error = prediction-row['y']
        percentage_error = abs(predicted-actual)/actual*100
        if not all(math.isfinite(v) for v in (predicted, error, percentage_error)):
            raise ValueError('Nonfinite forecast diagnostics')
        signed.append(error)
        naive.append(row['y'])
        mean_errors.append(train_mean-row['y'])
        actual_counts['actual_up' if row['y'] > 0 else 'actual_down' if row['y'] < 0 else 'actual_zero'] += 1
        if prediction != 0 and row['y'] != 0:
            directions.append((train_mean > 0) == (row['y'] > 0))
        item = {'ticker': row['ticker'], 'as_of': row['at'], 'target_at': row['target_at'],
                'anchor_price': anchor, 'predicted_price': predicted, 'actual_price': actual,
                'absolute_error_rub': abs(predicted-actual), 'absolute_error_pct': percentage_error}
        values.append(item)
        stock_values.setdefault(row['ticker'], []).append({**item,
            'baseline_error_rub': abs(anchor-actual),
            'mean_baseline_error_rub': abs(anchor*(1+train_mean/100)-actual),
            'baseline_mape_pct': abs(anchor-actual)/actual*100,
            'mean_baseline_mape_pct': abs(anchor*(1+train_mean/100)-actual)/actual*100})
    rms = lambda items: math.sqrt(mean(value*value for value in items))*100 if items else None
    report.update(rmse_bps=rms(signed), baseline_rmse_bps=rms(naive),
                  mean_baseline_rmse_bps=rms(mean_errors),
                  mape_pct=mean(value['absolute_error_pct'] for value in values) if values else None,
                  baseline_mape_pct=mean(item['baseline_mape_pct'] for stock in stock_values.values()
                                         for item in stock) if values else None,
                  mean_baseline_mape_pct=mean(item['mean_baseline_mape_pct'] for stock in stock_values.values()
                                              for item in stock) if values else None,
                  mean_signed_error_bps=mean(signed)*100 if signed else None,
                  direction_baseline_pct=mean(directions)*100 if directions and train_mean != 0 else None,
                  direction_baseline_label='Всегда рост' if train_mean > 0 else 'Всегда падение' if train_mean < 0 else 'Нет направления',
                  **actual_counts)
    stocks = {}
    for ticker in m.TICKERS:
        stock = stock_values.get(ticker, [])
        if not stock:
            continue
        stocks[ticker] = {
            'samples': len(stock), 'mae_rub': mean(item['absolute_error_rub'] for item in stock),
            'rmse_rub': math.sqrt(mean(item['absolute_error_rub']**2 for item in stock)),
            'baseline_mae_rub': mean(item['baseline_error_rub'] for item in stock),
            'mean_baseline_mae_rub': mean(item['mean_baseline_error_rub'] for item in stock),
            'baseline_rmse_rub': math.sqrt(mean(item['baseline_error_rub']**2 for item in stock)),
            'mean_baseline_rmse_rub': math.sqrt(mean(item['mean_baseline_error_rub']**2 for item in stock)),
            'baseline_mape_pct': mean(item['baseline_mape_pct'] for item in stock),
            'mean_baseline_mape_pct': mean(item['mean_baseline_mape_pct'] for item in stock),
            'mape_pct': mean(item['absolute_error_pct'] for item in stock)}
    latest = sorted((item for item in values if item['ticker'] == 'SBER'),
                    key=lambda item: _time(item['as_of']))[-10:]
    examples = [{**item, 'kind': 'latest'} for item in latest]
    if values:
        worst = max(values, key=lambda item: (item['absolute_error_pct'], _time(item['as_of']), item['ticker']))
        examples.append({**worst, 'kind': 'largest_error'})
    return {'report': report, 'stocks': stocks, 'examples': examples}


def run_folds(rows, progress=None):
    rows = list(rows)
    current = select_rows(rows, CURRENT_YEAR)
    folds = []
    for spec in FOLDS:
        groups = group_rows(rows, spec['periods'])
        if progress:
            progress(f'{spec["id"]}: обучение на прошлых датах, без тестовых ответов.')
        model = m.fit(groups['train'], groups['calibration'])
        primary, common = summarize(groups['test'], model), summarize(current, model)
        primary['report'].update(spec['periods']['test'])
        common['report'].update(CURRENT_YEAR)
        folds.append({**spec, 'trained_rows': sum(row['y'] is not None for row in groups['train']),
                      'calibration_rows': sum(row['y'] is not None for row in groups['calibration']),
                      'model': model, **primary,
                      **{f'current_year_{key}': value for key, value in common.items()}})
    return folds
