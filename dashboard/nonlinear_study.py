"""Fixed nonlinear morning-model comparisons. No broker, orders or promotion.

Export using the existing project Python, then train in the isolated runtime:
    python -m dashboard.nonlinear_study export
    data/nonlinear-runtime/Scripts/python -m dashboard.nonlinear_study train
"""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from . import morning_model as m
from . import multiyear_model as multi
from . import temporal_validation as temporal

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'nonlinear_morning_v1'
SEED = 20261024
PACKAGES = {'numpy': '2.3.4', 'scipy': '1.16.3', 'scikit-learn': '1.7.2',
            'joblib': '1.5.2', 'threadpoolctl': '3.6.0'}
VARIANTS = (
    {'id': 'ridge_control', 'label': 'Прежний Ridge', 'kind': 'ridge', 'params': {'alpha': 10.}},
    {'id': 'boost_mean', 'label': 'Бустинг: среднее', 'kind': 'hist', 'params': {
        'loss': 'squared_error', 'learning_rate': .03, 'max_iter': 150,
        'max_leaf_nodes': 7, 'max_depth': 3, 'min_samples_leaf': 100,
        'l2_regularization': 10., 'early_stopping': False, 'random_state': SEED}},
    {'id': 'boost_median', 'label': 'Бустинг: медиана', 'kind': 'hist', 'params': {
        'loss': 'absolute_error', 'learning_rate': .03, 'max_iter': 150,
        'max_leaf_nodes': 7, 'max_depth': 3, 'min_samples_leaf': 100,
        'l2_regularization': 10., 'early_stopping': False, 'random_state': SEED}},
    {'id': 'extra_trees', 'label': 'Ансамбль деревьев', 'kind': 'extra', 'params': {
        'n_estimators': 300, 'max_depth': 6, 'min_samples_leaf': 80,
        'max_features': 1., 'bootstrap': False, 'random_state': SEED, 'n_jobs': 2}},
)
RULES = {
    'task': 'unchanged17:45 features -> exactnextcalendar10:00 target; originMonThu, entry18:00proxy',
    'data': 'cachedverifiedEXCHANGE15m sevenoldstocks; same features18 and same temporalFOLDS2024/25/26',
    'training': '12fixedfits:4variants x3folds; weights trainonly; calibrationonly80percentresidualinterval; no tuning',
    'early_stopping': 'disabled; no random train/validation split',
    'baselines': 'zerochange, trainmean and trainmedian; Ridgecontrol must reproduce previous artifact',
    'paper': 'unchangedprediction>.4percent;100000RUBcommoncash;<=25percent/stock;wholelots;'
             'fee.05percent+slip.05percentperside;stressx2;unknownselected=>incomplete',
    'bootstrap': 'pairedMAEdifference vszero; overlapping5observeddateblocks; allsame-date equitieskepttogether;'
                 '1000reps seed20261024;95percentpercentileinterval;nomultiple-testingcorrection;notprofitprobability',
    'evaluation': 'eachfoldownunseen-to-fit year and common2026; Ccommon is duplicate, not newevidence',
    'data_reuse': 'alltestyears already examined by earlier studies; currentcomparison exploratory, not untouched holdout',
    'reserve': 'TATN/CHMF/PHOR remain unopened; no selection or promotion in this experiment',
    'limits': 'currentstocks/UID/lots; no pointintimebook/news/dividendpayments/taxes/realexecution/intranightdrawdown',
    'entry_allowed': False,
}


def source_hashes():
    root = ROOT/'dashboard'
    names = ('nonlinear_study.py', 'nonlinear_metrics.py')
    return {**temporal.source_hashes(),
            **{name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names},
            '../requirements-research.txt': hashlib.sha256((ROOT/'requirements-research.txt').read_bytes()).hexdigest()}


def immutable(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)


def signed(value):
    return {**value, 'artifact_sha256': m.digest(value)}


def checked(path):
    result = json.loads(path.read_text(encoding='utf-8'))
    checksum = result.pop('artifact_sha256', None)
    if checksum != m.digest(result):
        raise ValueError('Research file checksum mismatch')
    return {**result, 'artifact_sha256': checksum}


def freeze():
    spec = json.loads(json.dumps({'version': VERSION, 'variants': VARIANTS, 'rules': RULES,
        'folds': temporal.FOLDS, 'common_period': temporal.CURRENT_YEAR,
        'features': m.FEATURES, 'packages': PACKAGES, 'source_sha256': source_hashes()}))
    path = ROOT/'data'/'nonlinear-morning-protocol-v1.json'
    if path.exists():
        if checked(path)['spec'] != spec:
            raise ValueError('Registered research changed; create another version, never overwrite the protocol')
    else:
        immutable(path, signed({'registered_at': datetime.now(timezone.utc).isoformat(),
                               'spec': spec, 'entry_allowed': False}))
    return m.digest(spec)


def export_inputs():
    protocol = freeze()  # Code and choices registered before input reading.
    path = ROOT/'data'/'nonlinear-morning-inputs-v1.json'
    if path.exists():
        cached = checked(path)
        if cached['protocol_sha256'] != protocol or cached['source_sha256'] != source_hashes():
            raise ValueError('Input cache no longer matches protocol')
        print('Verified existing input cache', flush=True)
        return path
    from .multiyear_inputs import load_history
    rows, coverage, sources = [], {}, {}
    for ticker in m.TICKERS:
        source = load_history(ROOT/'data'/'multiyear', ticker, '15m')
        examples, detail = multi.build_intraday(source['candles'], ticker, source['instrument']['lot'])
        rows.extend(examples['control'])
        coverage[ticker] = detail['control']; sources[ticker] = source['sources']
        print(f'{ticker}: {len(examples["control"])} examples including unknowns', flush=True)
    immutable(path, signed({'version': VERSION, 'protocol_sha256': protocol,
        'created_at': datetime.now(timezone.utc).isoformat(), 'source_sha256': source_hashes(),
        'entry_allowed': False, 'rows': rows, 'coverage': coverage, 'sources': sources}))
    return path


def train_variant(spec, train, calibration):
    """Calibrate residual range after weights are fit using train exclusively."""
    import numpy as np
    known = [r for r in train if r['y'] is not None]
    cal = [r for r in calibration if r['y'] is not None]
    if (len(known) < 200 or len(cal) < 50
            or len({temporal._time(r['at']).astimezone(m.MSK).date() for r in known}) < 30
            or len({temporal._time(r['at']).astimezone(m.MSK).date() for r in cal}) < 10):
        raise ValueError('Insufficient training/calibration observations')
    if max(temporal._time(r['label_end']) for r in known) >= min(temporal._time(r['at']) for r in cal):
        raise ValueError('Training labels overlap calibration')
    for row in train+calibration:
        if len(row['x']) != len(m.FEATURES) or not all(type(v) in (float,int) and math.isfinite(v) for v in row['x']):
            raise ValueError('Invalid features')
        if row['y'] is not None and (type(row['y']) not in (float,int) or not math.isfinite(row['y'])):
            raise ValueError('Invalid training target')
    x = np.asarray([r['x'] for r in known], dtype=float)
    y = np.asarray([r['y'] for r in known], dtype=float)
    if spec['kind'] == 'ridge':
        estimator = m.fit(train, calibration)
        prediction = lambda batch: [m.predict(estimator, r['x']) for r in batch]
    else:
        from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
        factory = HistGradientBoostingRegressor if spec['kind'] == 'hist' else ExtraTreesRegressor
        estimator = factory(**spec['params'])
        estimator.fit(x, y)
        prediction = lambda batch: estimator.predict(np.asarray([r['x'] for r in batch], dtype=float)).tolist() if batch else []
    def predict_checked(batch):
        values = prediction(batch)
        if len(values) != len(batch) or not all(math.isfinite(p) for p in values):
            raise ValueError('Invalid model predictions')
        return values
    errors = sorted(abs(r['y']-p) for r, p in zip(cal, predict_checked(cal)))
    rank = min(len(errors), math.ceil((len(errors)+1)*.8))
    return {'estimator': estimator, 'predict': predict_checked, 'interval_abs_pct': errors[rank-1],
            'train_mean': float(y.mean()), 'train_median': median(y.tolist()),
            'train_n': len(known), 'calibration_n': len(cal)}


def score(fitted, rows):
    from .nonlinear_metrics import evaluate_predictions
    predictions = fitted['predict'](rows)
    report = evaluate_predictions(rows, predictions, fitted['train_mean'], fitted['train_median'])
    errors = [abs(p-r['y']) for r,p in zip(rows,predictions) if r['y'] is not None]
    q = fitted['interval_abs_pct']
    report.update(interval_abs_pct=q, coverage_pct=100*sum(e <= q for e in errors)/len(errors) if errors else None)
    forecasts = [{'ticker': r['ticker'], 'at': r['at'], 'predicted_change_pct': p, 'actual_change_pct': r['y']}
                 for r,p in zip(rows,predictions)]
    return {'report': report, 'predictions': forecasts}


def compare_old_control(folds):
    path = ROOT/'dashboard'/'ai_models'/'morning-temporal-v1-20261023-161258-732738.json'
    prior = checked(path)
    expected_ids = [fold['id'] for fold in temporal.FOLDS]
    if ([fold['id'] for fold in folds] != expected_ids
            or [fold['id'] for fold in prior['folds']] != expected_ids
            or prior['source_sha256'] != temporal.source_hashes()):
        raise ValueError('Old control source or complete fold set does not match')
    comparisons = []
    for fold, old in zip(folds, prior['folds']):
        if [variant['id'] for variant in fold['variants']] != [variant['id'] for variant in VARIANTS]:
            raise ValueError('Variant set differs from registered protocol')
        for name, old_name in (('primary', 'report'), ('common_2026', 'current_year_report')):
            new = fold['variants'][0][name]['report']; expected = old[old_name]
            for key in ('samples','eligible_forecasts','unknown_targets','days'):
                if new[key] != expected[key]:
                    raise ValueError(f'Ridge coverage differs: {fold["id"]}/{name}/{key}')
            for key in ('status','selected_candidates','trades','unknown_trades','stress_trades','stress_unknown_trades'):
                if new['paper'][key] != expected['paper'][key]:
                    raise ValueError(f'Ridge paper differs: {fold["id"]}/{name}/{key}')
            pairs = [('mae', new['mae_pct'], expected['mae_bps']/100),
                     ('rmse', new['rmse_pct'], expected['rmse_bps']/100),
                     ('paper', new['paper']['net_return_pct'], expected['paper']['net_return_pct']),
                     ('stress', new['paper']['stress_return_pct'], expected['paper']['stress_return_pct'])]
            for metric,a,b in pairs:
                okay = a is None and b is None or a is not None and b is not None and math.isclose(a,b,rel_tol=1e-7,abs_tol=1e-7)
                if not okay:
                    raise ValueError(f'Ridge control differs: {fold["id"]}/{name}/{metric}')
        comparisons.append({'fold': fold['id'], 'matched': True})
    return comparisons


def run_training():
    import importlib.metadata
    import joblib
    from threadpoolctl import threadpool_limits
    for name, required in PACKAGES.items():
        if importlib.metadata.version(name) != required:
            raise ValueError(f'Expected research dependency {name}=={required}')
    protocol = freeze()
    dataset = checked(ROOT/'data'/'nonlinear-morning-inputs-v1.json')
    if dataset['protocol_sha256'] != protocol or dataset['source_sha256'] != source_hashes():
        raise ValueError('Input dataset does not match registered code')
    rows = dataset['rows']
    current = temporal.select_rows(rows, temporal.CURRENT_YEAR)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    weights = ROOT/'data'/f'nonlinear-models-{stamp}'; weights.mkdir()
    folds = []
    with threadpool_limits(limits=2):
        for fold in temporal.FOLDS:
            groups = temporal.group_rows(rows, fold['periods'])
            variants = []
            for spec in VARIANTS:
                print(f'{fold["id"]} / {spec["id"]}: fit train, calibrate, score', flush=True)
                fitted = train_variant(spec, groups['train'], groups['calibration'])
                primary = score(fitted, groups['test'])
                common = primary if fold['id'] == 'test_2026' else score(fitted, current)
                path = weights/f'{fold["id"]}-{spec["id"]}.joblib'
                joblib.dump(fitted['estimator'], path, compress=3)
                variants.append({**spec, 'trained_rows': fitted['train_n'], 'calibration_rows': fitted['calibration_n'],
                    'primary': primary, 'common_2026': common, 'weights_local_file': path.relative_to(ROOT).as_posix(),
                    'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                result = primary['report']; paper = result['paper']
                print(f'  MAE={result["mae_pct"]:.6f}% zero={result["baseline_mae_pct"]:.6f}% '
                      f'paper={paper["net_return_pct"]} stress={paper["stress_return_pct"]} trades={paper["trades"]}', flush=True)
            folds.append({**fold, 'variants': variants})
    control = compare_old_control(folds)
    output = ROOT/'dashboard'/'ai_models'/f'nonlinear-morning-v1-{stamp}.json'
    immutable(output, signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'entry_allowed': False, 'rules': RULES, 'protocol_sha256': protocol, 'source_sha256': source_hashes(),
        'input_artifact_sha256': dataset['artifact_sha256'], 'packages': PACKAGES,
        'folds': folds, 'old_control_comparison': control, 'coverage': dataset['coverage'],
        'note': 'Все варианты и годы опубликованы. Это повторное исследование изученных данных; '
                'улучшение ошибки само по себе не разрешает реальные покупки.'}))
    print(f'Saved: {output.name}. Old Ridge control matches all three years.', flush=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export','train'))
    args = parser.parse_args()
    if args.action == 'export':
        print(export_inputs())
    else:
        run_training()
