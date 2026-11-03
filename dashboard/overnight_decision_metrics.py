"""Fixed diagnostics for entry-to-morning net-return/profit classifiers.

This research module receives already fitted predictions. It cannot fit,
choose a model, tune thresholds, send orders, or change live recommendations.
"""
import math
import random
from collections import defaultdict
from datetime import datetime
from numbers import Real
from statistics import mean

from . import morning_model as morning
from .nonlinear_metrics import paper_predictions


NET_THRESHOLD_PCT = .10
PROBABILITY_THRESHOLD = .55
FEE_SIDE = SLIPPAGE_SIDE = .0005
FLAT_GROSS_RETURN_PCT = 100 * ((1 - SLIPPAGE_SIDE) * (1 - FEE_SIDE)
                             / ((1 + SLIPPAGE_SIDE) * (1 + FEE_SIDE)) - 1)
BOOTSTRAP_DATES = 5
BOOTSTRAP_REPETITIONS = 1000
BOOTSTRAP_SEED = 20261024
LOG_EPSILON = 1e-15


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    return float(value)


def _probability(value, name):
    value = _number(value, name)
    if not 0 <= value <= 1:
        raise ValueError(f'{name} must be between zero and one')
    return value


def _prepare(rows, net_predictions, probabilities):
    rows, net_predictions = list(rows), list(net_predictions)
    probabilities = None if probabilities is None else list(probabilities)
    if len(rows) != len(net_predictions) or probabilities is not None and len(probabilities) != len(rows):
        raise ValueError('Every eligible row requires aligned predictions')
    # Validate original anchor labels, aware horizons, uniqueness and prices
    # without accidentally filtering the rows with an unavailable entry/exit.
    paper_predictions(rows, [0.] * len(rows), threshold=.5)
    predictions = [_number(value, 'Net prediction') for value in net_predictions]
    probs = ([_probability(value, 'Probability') for value in probabilities]
             if probabilities is not None else None)
    for row, prediction in zip(rows, predictions):
        missing = row['entry_price'] is None or row['actual_price'] is None
        if missing:
            if row['net_y'] is not None or row['profit_y'] is not None:
                raise ValueError('Unknown entry or exit requires unknown net/profit labels')
            continue
        target = _number(row['net_y'], 'Net target')
        expected = 100 * (row['actual_price'] * (1 - SLIPPAGE_SIDE) * (1 - FEE_SIDE)
                          / (row['entry_price'] * (1 + SLIPPAGE_SIDE) * (1 + FEE_SIDE)) - 1)
        if not math.isfinite(expected) or not math.isclose(target, expected, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError('Net label and entry/exit economics disagree')
        label = row['profit_y']
        if not isinstance(label, Real) or label not in (0, 1) or label != int(target > 0):
            raise ValueError('Profit label must be one exactly when net return is positive')
        if not math.isfinite(prediction - target):
            raise ValueError('Net prediction error overflow')
    return rows, predictions, probs


def _policy(rows, selection):
    # Encoding only externally generated decisions ensures the stress policy
    # keeps identical candidates. Future price labels never choose entries.
    decisions = [1. if selected else 0. for selected in selection]
    return {
        'paper': paper_predictions(rows, decisions, threshold=.5),
        'stress': paper_predictions(rows, decisions, multiplier=2, threshold=.5),
    }


def validate_rows(rows):
    """Validate original prices and enriched net/profit labels before fitting."""
    rows = list(rows)
    _prepare(rows, [0.] * len(rows), None)
    return rows


def always_buy(rows):
    """Separate fixed comparison, including every eligible unknown candidate."""
    rows = list(rows)
    return _policy(rows, [True] * len(rows))


def _auc(labels, probabilities):
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ordered = sorted(zip(probabilities, labels))
    positive_ranks, offset = 0., 0
    while offset < len(ordered):
        end = offset + 1
        while end < len(ordered) and ordered[end][0] == ordered[offset][0]:
            end += 1
        rank = (offset + 1 + end) / 2
        positive_ranks += rank * sum(label for _, label in ordered[offset:end])
        offset = end
    return (positive_ranks - positives * (positives + 1) / 2) / (positives * negatives)


def _probability_metrics(labels, probabilities):
    if not labels:
        return {'brier': None, 'log_loss': None, 'auc': None, 'accuracy_pct': None}
    clipped = [min(1 - LOG_EPSILON, max(LOG_EPSILON, value)) for value in probabilities]
    return {
        'brier': mean((probability - label) ** 2 for label, probability in zip(labels, probabilities)),
        'log_loss': -mean(label * math.log(probability) + (1 - label) * math.log1p(-probability)
                          for label, probability in zip(labels, clipped)),
        'auc': _auc(labels, probabilities),
        'accuracy_pct': 100 * mean((probability >= .5) == bool(label)
                                   for label, probability in zip(labels, probabilities)),
    }


def _reliability(labels, probabilities):
    bins = [{'lower': index / 10, 'upper': (index + 1) / 10, 'count': 0,
             'mean_probability': None, 'observed_profit_rate': None} for index in range(10)]
    members = [[] for _ in range(10)]
    for label, probability in zip(labels, probabilities):
        members[min(9, int(probability * 10))].append((label, probability))
    for bucket, values in zip(bins, members):
        if values:
            bucket.update(count=len(values), mean_probability=mean(prob for _, prob in values),
                          observed_profit_rate=mean(label for label, _ in values))
    return bins


def _percentile(values, fraction):
    position = (len(values) - 1) * fraction
    left, right = math.floor(position), math.ceil(position)
    return values[left] + (values[right] - values[left]) * (position - left)


def _paired_bootstrap(rows, differences, unit):
    """Same-date stocks travel together in consecutive five-observed-date blocks."""
    by_day = defaultdict(list)
    for row, difference in zip(rows, differences):
        day = datetime.fromisoformat(row['at']).astimezone(morning.MSK).date()
        by_day[day].append(difference)
    days = sorted(by_day)
    result = {
        'difference': mean(differences) if differences else None,
        'lower': None, 'upper': None, 'unit': unit, 'confidence_level': .95,
        'dates': len(days), 'samples': len(differences), 'block_dates': BOOTSTRAP_DATES,
        'repetitions': BOOTSTRAP_REPETITIONS, 'seed': BOOTSTRAP_SEED,
        'status': 'ok' if len(days) >= 2 * BOOTSTRAP_DATES else 'insufficient_dates',
        'note': 'Paired historical diagnostic; negative favours model; no adjustment for prior model searches.',
    }
    if result['status'] != 'ok':
        return result
    totals = [math.fsum(by_day[day]) for day in days]
    counts = [len(by_day[day]) for day in days]
    rng, estimates = random.Random(BOOTSTRAP_SEED), []
    for _ in range(BOOTSTRAP_REPETITIONS):
        remaining, sums, observations = len(days), [], 0
        while remaining:
            start = rng.randrange(len(days) - BOOTSTRAP_DATES + 1)
            take = min(BOOTSTRAP_DATES, remaining)
            sums.extend(totals[start:start + take])
            observations += sum(counts[start:start + take])
            remaining -= take
        estimates.append(math.fsum(sums) / observations)
    estimates.sort()
    result.update(lower=_percentile(estimates, .025), upper=_percentile(estimates, .975))
    return result


def evaluate(rows, net_predictions, probabilities, train_net_mean, train_profit_prior,
             calibration_profit_prior, *, train_net_median=None):
    """Evaluate externally fitted net-return estimates and optional profit probabilities.

    ``net_y`` is the percentage per-share result after the fixed two-sided costs;
    ``profit_y`` is one exactly when ``net_y > 0``. Both remain None whenever
    the 18:00 entry or next-day 10:00 exit price is unavailable. Probability
    metrics, the joint policy and its bootstrap are None for a regression-only
    model. Regression errors are percentage points; Brier errors are unscaled.
    The optional training-median baseline does not change the registered
    paired MAE bootstrap, which always compares against the training mean.
    """
    rows, predictions, probs = _prepare(rows, net_predictions, probabilities)
    train_mean = _number(train_net_mean, 'Training net mean')
    train_median = (_number(train_net_median, 'Training net median')
                    if train_net_median is not None else None)
    train_prior = _probability(train_profit_prior, 'Training profit prior')
    calibration_prior = _probability(calibration_profit_prior, 'Calibration profit prior')
    known_indices = [index for index, row in enumerate(rows) if row['net_y'] is not None]
    known = [rows[index] for index in known_indices]
    targets = [row['net_y'] for row in known]
    errors = [predictions[index] - rows[index]['net_y'] for index in known_indices]
    mean_errors = [target - train_mean for target in targets]
    median_errors = [target - train_median for target in targets] if train_median is not None else []
    flat_errors = [target - FLAT_GROSS_RETURN_PCT for target in targets]
    rms = lambda values: math.hypot(*values) / math.sqrt(len(values)) if values else None
    absolute = lambda values: mean(abs(value) for value in values) if values else None
    regression = {
        'mae_pct': absolute(errors), 'rmse_pct': rms(errors),
        'mean_baseline_mae_pct': absolute(mean_errors), 'mean_baseline_rmse_pct': rms(mean_errors),
        'median_baseline_mae_pct': absolute(median_errors), 'median_baseline_rmse_pct': rms(median_errors),
        'flat_gross_baseline_mae_pct': absolute(flat_errors), 'flat_gross_baseline_rmse_pct': rms(flat_errors),
        'flat_gross_return_pct': FLAT_GROSS_RETURN_PCT, 'train_mean_pct': train_mean,
        'train_median_pct': train_median,
    }
    mae_differences = [abs(error) - abs(baseline) for error, baseline in zip(errors, mean_errors)]
    classification = brier_bootstrap = joint_policy = None
    if probs is not None:
        labels = [int(row['profit_y']) for row in known]
        known_probs = [probs[index] for index in known_indices]
        classification = {
            **_probability_metrics(labels, known_probs), 'samples': len(labels),
            'positive_labels': sum(labels), 'negative_labels': len(labels) - sum(labels),
            'reliability': _reliability(labels, known_probs),
            'train_baseline': {'probability': train_prior,
                               **_probability_metrics(labels, [train_prior] * len(labels))},
            'calibration_baseline': {'probability': calibration_prior,
                                     **_probability_metrics(labels, [calibration_prior] * len(labels))},
        }
        brier_differences = [(probability - label) ** 2 - (calibration_prior - label) ** 2
                             for label, probability in zip(labels, known_probs)]
        brier_bootstrap = _paired_bootstrap(known, brier_differences, 'brier')
        joint_policy = _policy(rows, [prediction > NET_THRESHOLD_PCT and probability >= PROBABILITY_THRESHOLD
                                     for prediction, probability in zip(predictions, probs)])
    return {
        'samples': len(known), 'eligible_forecasts': len(rows), 'unknown_targets': len(rows) - len(known),
        'days': len({datetime.fromisoformat(row['at']).astimezone(morning.MSK).date() for row in known}),
        'regression': regression, 'classification': classification,
        'mae_difference_bootstrap': _paired_bootstrap(known, mae_differences, 'percentage_points'),
        'brier_difference_bootstrap': brier_bootstrap,
        'policies': {'regression': _policy(rows, [prediction > NET_THRESHOLD_PCT for prediction in predictions]),
                     'joint': joint_policy},
    }
