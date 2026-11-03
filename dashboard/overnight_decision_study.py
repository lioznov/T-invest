"""Fixed overnight net-return and calibrated-probability research; never trades.

Run with the pinned isolated research Python: -m dashboard.overnight_decision_study
All choices and code are registered before opening the verified example cache.
"""
import hashlib
import importlib.metadata
import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median, pstdev

from . import morning_model as m
from . import nonlinear_study as old
from . import nonlinear_metrics as paper
from . import temporal_validation as temporal

ROOT = old.ROOT
VERSION = 'overnight_decision_v1'
PEER_FEATURES = ('peer_previous_complete_return_mean', 'peer_return_dispersion',
                 'peer_positive_fraction', 'relative_to_peers', 'peer_coverage')
VARIANTS = (
    {'id': 'net_ridge', 'kind': 'ridge', 'features': 'base'},
    {'id': 'net_boost', 'kind': 'hist', 'features': 'base'},
    {'id': 'peer_boost', 'kind': 'hist', 'features': 'peer'},
)
REG_PARAMS = dict(old.VARIANTS[1]['params'])
CLASS_PARAMS = {**REG_PARAMS, 'loss': 'log_loss'}
CAL_PARAMS = {'C': 1., 'solver': 'lbfgs', 'max_iter': 1000, 'random_state': old.SEED}
RULES = {
    'label': '100*(next10open*(1-.0005)*(1-.0005)/(entry18open*(1+.0005)*(1+.0005))-1)',
    'unknowns': 'entry OR exit missing => unknown net/profit; preserve every eligible forecast',
    'features': 'original causal17:45 base18; optional5 leave-self-out features from same UTC instant',
    'peers': 'all eligible peer X before any outcome filtering; prior complete reference may be older than1day; '
             'no backfill; zero peers=>five zeros; coverage=count/6; only current seven-stock basket',
    'training': '15 fixed base fits:3netRidge+6HGBregression+6HGBclassification; no early stopping/search',
    'calibration': '6 separate L2 logistic sigmoid fits on next chronological year raw decision scores; '
                   'no test data, no random CV; regression80% absolute residual interval from same calibration year',
    'policies': 'regression net>.10percent; joint net>.10percent AND calibratedP(net>0)>=.55; '
                'thresholds fixed before fits; stress repeats identical decisions',
    'paper': 'same100000RUB cash account; max25percent/stock, whole lots; fee/slip .05percent each per side; '
             'stress doubles both; selected unknown makes entire subsequent P&L undefined',
    'comparison': 'three chronological test years2024/25/26; old anchorRidge saved control matched by ticker/instant; '
                  'old price MAE and new net-return MAE are different targets, not comparable',
    'baselines': 'train mean and median net return; flat gross price after costs; train and calibration profit prevalence; '
                 'always buy common account; holding cash returns0 before interest/inflation',
    'reuse': 'all years already inspected, exploratory reuse, NOT untouched holdout; no winning-model promotion',
    'bootstrap': 'paired5-observed-date blocks,1000reps seed20261024,95percent; '
                 'not corrected for repeated experiments, not probability of profit',
    'limits': 'current survivor stocks/UID/lots; missing holidays remain unknown; no dividend cash/tax/auctionqueue; '
              'no historical orderbook/news/traderidentity, no worst overnight drawdown or verified fills',
    'reserve': 'TATN/CHMF/PHOR unopened', 'entry_allowed': False,
}


def source_hashes():
    return {**old.source_hashes(), **{
        name: hashlib.sha256((ROOT/'dashboard'/name).read_bytes()).hexdigest()
        for name in ('overnight_decision_study.py', 'overnight_decision_metrics.py')}}


def freeze():
    from .overnight_decision_metrics import NET_THRESHOLD_PCT, PROBABILITY_THRESHOLD
    if NET_THRESHOLD_PCT != .10 or PROBABILITY_THRESHOLD != .55:
        raise ValueError('Policy constants differ from the registered design')
    spec = jsonable({'version': VERSION, 'variants': VARIANTS, 'regression': REG_PARAMS,
        'classifier': CLASS_PARAMS, 'sigmoid': CAL_PARAMS, 'rules': RULES,
        'features': m.FEATURES, 'peer_features': PEER_FEATURES, 'folds': temporal.FOLDS,
        'packages': old.PACKAGES, 'source_sha256': source_hashes()})
    path = ROOT/'data'/'overnight-decision-protocol-v1.json'
    if path.exists():
        if old.checked(path)['spec'] != spec:
            raise ValueError('Frozen research changed. Create a new version.')
    else:
        old.immutable(path, old.signed({'registered_at': datetime.now(timezone.utc).isoformat(),
                                      'spec': spec, 'entry_allowed': False}))
    return m.digest(spec)


def jsonable(value):
    import json
    return json.loads(json.dumps(value, allow_nan=False))


def _features(row, key, width):
    x = row[key]
    if len(x) != width or not all(type(v) in (float, int) and math.isfinite(v) for v in x):
        raise ValueError('Invalid finite feature vector')
    return x


def enrich_rows(rows):
    """Future prices define labels only; peer features depend exclusively on X and time."""
    rows = list(rows)
    paper.paper_predictions(rows, [0.] * len(rows))  # Validate original labels and unique keys.
    groups = defaultdict(list)
    for row in rows:
        if row['ticker'] not in m.TICKERS:
            raise ValueError('Unsupported peer basket member')
        _features(row, 'x', len(m.FEATURES))
        groups[temporal._time(row['at']).astimezone(timezone.utc)].append(row)
    result = []
    for row in rows:
        peers = [other['x'][0] for other in groups[temporal._time(row['at']).astimezone(timezone.utc)]
                 if other['ticker'] != row['ticker']]
        extra = ([mean(peers), pstdev(peers), mean(p > 0 for p in peers),
                  row['x'][0]-mean(peers), len(peers)/6] if peers else [0.] * 5)
        entry, actual = row['entry_price'], row['actual_price']
        net = (100 * (actual * .9995 * .9995 / (entry * 1.0005 * 1.0005)-1)
               if entry is not None and actual is not None else None)
        if net is not None and not math.isfinite(net):
            raise ValueError('Nonfinite net target')
        result.append({**row, 'x_peer': list(row['x']) + extra, 'net_y': net,
                       'profit_y': int(net > 0) if net is not None else None})
    return result


def fit_variant(spec, train, calibration):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.linear_model import LogisticRegression
    from .overnight_decision_metrics import validate_rows
    if spec not in VARIANTS:
        raise ValueError('Unregistered variant')
    validate_rows(train); validate_rows(calibration)
    key = 'x_peer' if spec['features'] == 'peer' else 'x'
    width = len(m.FEATURES) + (5 if key == 'x_peer' else 0)
    for row in train + calibration:
        _features(row, key, width)
    known = [r for r in train if r['net_y'] is not None]
    cal = [r for r in calibration if r['net_y'] is not None]
    if (len(known) < 200 or len(cal) < 50
            or len({temporal._time(r['at']).astimezone(m.MSK).date() for r in known}) < 30
            or len({temporal._time(r['at']).astimezone(m.MSK).date() for r in cal}) < 10):
        raise ValueError('Insufficient training/calibration observations')
    if max(temporal._time(r['label_end']) for r in train) >= min(temporal._time(r['at']) for r in calibration):
        raise ValueError('Training labels overlap calibration')
    x = np.asarray([r[key] for r in known]); y = np.asarray([r['net_y'] for r in known])
    cx = np.asarray([r[key] for r in cal]); cy = np.asarray([r['profit_y'] for r in cal])
    if spec['kind'] == 'ridge':
        regressor = m.fit([{**r, 'y': r['net_y']} for r in known], [{**r, 'y': r['net_y']} for r in cal])
        classifier = calibrator = None
    else:
        if set(r['profit_y'] for r in known) != {0, 1} or set(cy) != {0, 1}:
            raise ValueError('Both profit classes required in train and calibration')
        regressor = HistGradientBoostingRegressor(**REG_PARAMS).fit(x, y)
        classifier = HistGradientBoostingClassifier(**CLASS_PARAMS).fit(x, [r['profit_y'] for r in known])
        raw_cal = classifier.decision_function(cx).reshape(-1, 1)
        calibrator = LogisticRegression(**CAL_PARAMS).fit(raw_cal, cy)
    bundle = {'spec': spec, 'regressor': regressor, 'classifier': classifier, 'calibrator': calibrator,
              'train_net_mean': float(y.mean()), 'train_net_median': median(y.tolist()),
              'train_profit_prior': mean(r['profit_y'] for r in known),
              'calibration_profit_prior': mean(r['profit_y'] for r in cal),
              'trained_rows': len(known), 'calibration_rows': len(cal)}
    values, _ = predict(bundle, cal)
    residuals = sorted(abs(r['net_y']-p) for r, p in zip(cal, values))
    bundle['interval_abs_pct'] = residuals[min(len(residuals), math.ceil((len(residuals)+1)*.8))-1]
    return bundle


def predict(bundle, rows):
    """Inference reads feature vectors only, never future labels or entry/exit prices."""
    import numpy as np
    spec = bundle['spec']; key = 'x_peer' if spec['features'] == 'peer' else 'x'
    width = len(m.FEATURES) + (5 if key == 'x_peer' else 0)
    x = [_features(r, key, width) for r in rows]
    if spec['kind'] == 'ridge':
        net = [m.predict(bundle['regressor'], values) for values in x]; probs = None
    elif not rows:
        net, probs = [], []
    else:
        net = bundle['regressor'].predict(np.asarray(x)).tolist()
        scores = bundle['classifier'].decision_function(np.asarray(x)).reshape(-1, 1)
        probs = bundle['calibrator'].predict_proba(scores)[:, 1].tolist()
    if (len(net) != len(rows) or not all(math.isfinite(v) for v in net)
            or probs is not None and (len(probs) != len(rows) or not all(math.isfinite(p) and 0 <= p <= 1 for p in probs))):
        raise ValueError('Invalid predictions')
    return net, probs


def score(bundle, rows):
    from .overnight_decision_metrics import evaluate
    net, probs = predict(bundle, rows)
    report = evaluate(rows, net, probs, bundle['train_net_mean'], bundle['train_profit_prior'],
                      bundle['calibration_profit_prior'], train_net_median=bundle['train_net_median'])
    errors = [abs(r['net_y']-p) for r, p in zip(rows, net) if r['net_y'] is not None]
    q = bundle['interval_abs_pct']
    report.update(interval_abs_pct=q, interval_coverage_pct=100*mean(e <= q for e in errors) if errors else None)
    forecasts = [{'ticker': r['ticker'], 'at': r['at'], 'predicted_net_pct': p,
                  'actual_net_pct': r['net_y'], 'probability_profit': probs[i] if probs is not None else None,
                  'selected_regression': p > .10, 'selected_joint': p > .10 and probs[i] >= .55 if probs is not None else None}
                 for i, (r, p) in enumerate(zip(rows, net))]
    return {'report': report, 'predictions': forecasts}


def old_control(prior, fold, rows):
    if [f['id'] for f in prior['folds']] != [f['id'] for f in temporal.FOLDS]:
        raise ValueError('Prior fold set differs')
    variant = next(f for f in prior['folds'] if f['id'] == fold['id'])['variants'][0]
    if variant['id'] != 'ridge_control':
        raise ValueError('Expected the original anchor Ridge control')
    previous = variant['primary']
    saved = {(r['ticker'], temporal._time(r['at'])): r['predicted_change_pct'] for r in previous['predictions']}
    expected = {(r['ticker'], temporal._time(r['at'])) for r in rows}
    if len(saved) != len(previous['predictions']) or set(saved) != expected:
        raise ValueError('Old control eligible population differs')
    predictions = [saved[(r['ticker'], temporal._time(r['at']))] for r in rows]
    replay = paper.paper_predictions(rows, predictions)
    stress = paper.paper_predictions(rows, predictions, multiplier=2)
    for key in ('status', 'trades', 'selected_candidates', 'selected_dates', 'unknown_trades'):
        if replay[key] != previous['report']['paper'][key] or stress[key] != previous['report']['stress'][key]:
            raise ValueError('Old control decisions or completeness differ')
    for key, actual in (('net_return_pct', replay['net_return_pct']), ('stress_return_pct', stress['net_return_pct'])):
        value = previous['report']['paper'][key]
        if not (value == actual or value is not None and actual is not None and math.isclose(value, actual, abs_tol=1e-9)):
            raise ValueError('Old control monetary replay mismatch')
    return {'paper': replay, 'stress': stress, 'matched': True, 'target': 'old17:45anchor->morningchange'}


def run():
    import joblib
    from threadpoolctl import threadpool_limits
    from .overnight_decision_metrics import always_buy
    for name, version in old.PACKAGES.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f'Requires {name}=={version}')
    protocol = freeze()
    dataset = old.checked(ROOT/'data'/'nonlinear-morning-inputs-v1.json')
    prior = old.checked(ROOT/'dashboard'/'ai_models'/'nonlinear-morning-v1-20261024-082555-671799.json')
    if (dataset['source_sha256'] != old.source_hashes() or prior['source_sha256'] != old.source_hashes()
            or prior['input_artifact_sha256'] != dataset['artifact_sha256']):
        raise ValueError('Prior data/code provenance changed')
    rows = enrich_rows(dataset['rows'])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder = ROOT/'data'/f'overnight-decision-models-{stamp}'; folder.mkdir()
    folds = []
    with threadpool_limits(limits=2):
        for fold in temporal.FOLDS:
            groups = temporal.group_rows(rows, fold['periods'])
            variants = []
            for spec in VARIANTS:
                print(f'{fold["id"]} / {spec["id"]}: train then calibrate then test', flush=True)
                bundle = fit_variant(spec, groups['train'], groups['calibration'])
                scored = score(bundle, groups['test'])
                path = folder/f'{fold["id"]}-{spec["id"]}.joblib'
                joblib.dump(bundle, path, compress=3)
                # Own locally generated file only; no external pickle deserialization.
                reloaded = predict(joblib.load(path), groups['test'])
                original = predict(bundle, groups['test'])
                if reloaded != original:
                    raise ValueError('Saved weights changed predictions')
                variants.append({**spec, 'trained_rows': bundle['trained_rows'], 'calibration_rows': bundle['calibration_rows'],
                    'train_net_mean': bundle['train_net_mean'], 'train_net_median': bundle['train_net_median'],
                    'train_profit_prior': bundle['train_profit_prior'],
                    'calibration_profit_prior': bundle['calibration_profit_prior'], **scored,
                    'weights_local_file': path.relative_to(ROOT).as_posix(),
                    'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                print(f'  netMAE={scored["report"]["regression"]["mae_pct"]:.6f}% policies={scored["report"]["policies"]}', flush=True)
            folds.append({**fold, 'variants': variants, 'old_control': old_control(prior, fold, groups['test']),
                          'always_buy': always_buy(groups['test'])})
    if source_hashes() != old.checked(ROOT/'data'/'overnight-decision-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Research code changed during execution')
    output = ROOT/'dashboard'/'ai_models'/f'overnight-decision-v1-{stamp}.json'
    old.immutable(output, old.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'entry_allowed': False, 'rules': RULES, 'protocol_sha256': protocol, 'source_sha256': source_hashes(),
        'packages': old.PACKAGES, 'input_artifact_sha256': dataset['artifact_sha256'],
        'prior_artifact_sha256': prior['artifact_sha256'], 'folds': folds,
        'eligible_input_rows': len(rows), 'known_net_rows': sum(r['net_y'] is not None for r in rows),
        'peer_counts': {str(n): sum(round(r['x_peer'][-1]*6) == n for r in rows) for n in range(7)}}))
    print(f'Saved {output}', flush=True)
    return output


if __name__ == '__main__':
    run()
