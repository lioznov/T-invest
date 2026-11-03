"""Fixed, model-independent diagnostics for causal overnight predictions.

Predictions are percentage changes from the known 17:45 anchor, not prices or
probabilities. This module neither fits models nor selects a winning strategy.
The paper account deliberately retains the frozen morning_model economics.
"""
import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from numbers import Integral, Real
from statistics import mean

from . import morning_model as m


BOOTSTRAP_REPETITIONS = 1000
BOOTSTRAP_BLOCK_DATES = 5
BOOTSTRAP_SEED = 20261024


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    return float(value)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be ISO text')
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Invalid timestamp') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Timezone-aware timestamps are required')
    return result


def _validated(rows, predictions):
    rows, predictions = list(rows), list(predictions)
    if len(rows) != len(predictions):
        raise ValueError('Each eligible row requires exactly one prediction')
    seen, values = set(), []
    for row, prediction in zip(rows, predictions):
        prediction = _finite(prediction, 'Prediction')
        at, until = _time(row['at']), _time(row['label_end'])
        if until <= at:
            raise ValueError('The label must follow the forecast')
        ticker = row['ticker']
        if not isinstance(ticker, str) or not ticker:
            raise ValueError('Ticker is required')
        key = (ticker, at.astimezone(timezone.utc))
        if key in seen:
            raise ValueError('Duplicate ticker and forecast instant')
        seen.add(key)
        lot = row['lot']
        if isinstance(lot, bool) or not isinstance(lot, Integral) or lot <= 0:
            raise ValueError('Lot must be a positive integer')
        anchor = _finite(row['anchor_price'], 'Anchor price')
        if anchor <= 0:
            raise ValueError('Anchor price must be positive')
        for name in ('entry_price', 'actual_price'):
            if row[name] is not None and _finite(row[name], name) <= 0:
                raise ValueError('Prices must be positive or explicitly unknown')
        if row['y'] is None:
            if row['actual_price'] is not None:
                raise ValueError('Unknown return must have unknown actual price')
        else:
            actual = row['actual_price']
            if actual is None:
                raise ValueError('Known return requires an actual price')
            target = _finite(row['y'], 'Target')
            implied = (actual / anchor - 1) * 100
            if not math.isfinite(implied) or not math.isclose(target, implied, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError('Actual price and return label disagree')
            if not math.isfinite(prediction - target):
                raise ValueError('Forecast error overflow')
        values.append((row, prediction, at.astimezone(m.MSK).date()))
    return values


def _paper(values, multiplier, threshold):
    multiplier, threshold = _finite(multiplier, 'Cost multiplier'), _finite(threshold, 'Threshold')
    if multiplier < 0:
        raise ValueError('Cost multiplier cannot be negative')
    fee = slip = multiplier * .0005
    if fee >= 1:
        raise ValueError('Cost multiplier is outside the supported range')
    days = defaultdict(list)
    for row, prediction, day in values:
        if prediction > threshold:
            days[day].append(row)
    cash = peak = 100000.
    dd, trades, unknown, halted, drifts = 0., 0, 0, False, []
    for day in sorted(days):
        selected = days[day]
        missing = [row for row in selected if row['entry_price'] is None or row['actual_price'] is None]
        unknown += len(missing)
        if halted or missing:
            halted = True
            continue
        budget = cash * min(.25, 1 / len(selected))
        pnl = 0.
        for row in selected:
            entry = row['entry_price'] * (1 + slip)
            qty = math.floor(budget / (entry * (1 + fee) * row['lot'])) * row['lot']
            if qty <= 0:
                continue
            trades += 1
            pnl += qty * (row['actual_price'] * (1 - slip) * (1 - fee) - entry * (1 + fee))
            drifts.append((row['entry_price'] / row['anchor_price'] - 1) * 100)
        cash += pnl
        if not math.isfinite(cash):
            raise ValueError('Paper account overflow')
        peak = max(peak, cash)
        dd = max(dd, (peak - cash) / peak)
    return {
        'trades': trades, 'selected_candidates': sum(map(len, days.values())),
        'selected_dates': len(days), 'unknown_trades': unknown,
        'status': 'incomplete' if halted else 'ok',
        'net_return_pct': (cash / 100000. - 1) * 100 if trades and not halted else None,
        'max_drawdown_pct': dd * 100 if trades and not halted else None,
        'mean_anchor_to_entry_pct': mean(drifts) if drifts else None,
    }


def paper_predictions(rows, predictions, multiplier=1, threshold=.4):
    """Frozen long-only paper policy, driven by externally fitted predictions.

All eligible rows, including missing targets, must be supplied. A selected
unknown entry or exit makes the complete account result unknown thereafter.
``selected_dates`` counts candidate dates, including unaffordable or unknowns.
"""
    return _paper(_validated(rows, predictions), multiplier, threshold)


def _percentile(sorted_values, probability):
    position = (len(sorted_values) - 1) * probability
    left = math.floor(position)
    right = math.ceil(position)
    return sorted_values[left] + (sorted_values[right] - sorted_values[left]) * (position - left)


def _bootstrap(values):
    by_date = defaultdict(list)
    for row, prediction, day in values:
        if row['y'] is not None:
            by_date[day].append(abs(prediction - row['y']) - abs(row['y']))
    dates = sorted(by_date)
    count = sum(map(len, by_date.values()))
    result = {
        'difference_pct': mean(value for items in by_date.values() for value in items) if count else None,
        'lower_pct': None, 'upper_pct': None, 'confidence_level': .95,
        'block_dates': BOOTSTRAP_BLOCK_DATES,
        'effective_block_dates': min(BOOTSTRAP_BLOCK_DATES, len(dates)),
        'repetitions': BOOTSTRAP_REPETITIONS, 'seed': BOOTSTRAP_SEED,
        'dates': len(dates), 'samples': count,
        'status': 'ok' if len(dates) >= 2 * BOOTSTRAP_BLOCK_DATES else 'insufficient_dates',
    }
    if result['status'] != 'ok':
        return result
    # Paired errors for every stock on a date travel together. Overlapping
    # moving blocks preserve five consecutive observed dates, not five rows.
    totals = [math.fsum(by_date[day]) for day in dates]
    counts = [len(by_date[day]) for day in dates]
    length = BOOTSTRAP_BLOCK_DATES
    rng = random.Random(BOOTSTRAP_SEED)
    estimates = []
    for _ in range(BOOTSTRAP_REPETITIONS):
        remaining, sums, observations = len(dates), [], 0
        while remaining:
            start = rng.randrange(len(dates) - length + 1)
            take = min(length, remaining)
            sums.extend(totals[start:start + take])
            observations += sum(counts[start:start + take])
            remaining -= take
        estimates.append(math.fsum(sums) / observations)
    estimates.sort()
    result.update(lower_pct=_percentile(estimates, .025), upper_pct=_percentile(estimates, .975))
    return result


def paired_mae_bootstrap(rows, predictions):
    """95% moving-block interval for MAE(model)-MAE(unchanged price).

Negative differences favour the model. This is a paired historical error
diagnostic, not a probability of profit or an adjustment for model searches.
No interval is estimated with fewer than ten observed dates.
"""
    return _bootstrap(_validated(rows, predictions))


def evaluate_predictions(rows, predictions, train_mean, train_median):
    """Score percentage-change predictions against fixed training baselines."""
    values = _validated(rows, predictions)
    train_mean = _finite(train_mean, 'Training mean')
    train_median = _finite(train_median, 'Training median')
    known = [(row, prediction, day) for row, prediction, day in values if row['y'] is not None]
    targets = [row['y'] for row, _, _ in known]
    signed = [prediction - row['y'] for row, prediction, _ in known]
    # A zero forecast is an incorrect direction for a nonzero actual move.
    # Dropping it would let a model improve its accuracy by abstaining silently.
    directional = [(row['y'], prediction) for row, prediction, _ in known if row['y'] != 0]
    directions = [prediction != 0 and ((prediction > 0) == (target > 0))
                  for target, prediction in directional]
    baseline_directions = [train_mean != 0 and ((train_mean > 0) == (target > 0))
                           for target, _ in directional]
    paper, stress = _paper(values, 1, .4), _paper(values, 2, .4)
    paper.update(stress_return_pct=stress['net_return_pct'], stress_trades=stress['trades'],
                 stress_unknown_trades=stress['unknown_trades'])
    return {
        'samples': len(known), 'eligible_forecasts': len(values), 'unknown_targets': len(values) - len(known),
        'days': len({day for _, _, day in known}),
        'mae_pct': mean(abs(value) for value in signed) if signed else None,
        'rmse_pct': math.hypot(*signed) / math.sqrt(len(signed)) if signed else None,
        'baseline_mae_pct': mean(abs(value) for value in targets) if targets else None,
        'baseline_rmse_pct': math.hypot(*targets) / math.sqrt(len(targets)) if targets else None,
        'mean_baseline_mae_pct': mean(abs(value - train_mean) for value in targets) if targets else None,
        'median_baseline_mae_pct': mean(abs(value - train_median) for value in targets) if targets else None,
        'direction_accuracy_pct': mean(directions) * 100 if directions else None,
        'direction_nonzero_samples': len(directions),
        'direction_zero_predictions': sum(prediction == 0 for _, prediction in directional),
        'direction_baseline_pct': mean(baseline_directions) * 100 if baseline_directions else None,
        'direction_baseline_label': 'up' if train_mean > 0 else 'down' if train_mean < 0 else 'zero',
        'paper': paper, 'stress': stress, 'mae_difference_bootstrap': _bootstrap(values),
    }
