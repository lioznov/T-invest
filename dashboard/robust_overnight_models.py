"""Registered tail-resistant overnight ML comparisons on the old real-price cache.

No fetching, parameter search, orders, Telegram, or replacement of live models.
Run only with the pinned research runtime: -m dashboard.robust_overnight_models.
"""
import hashlib
import importlib.metadata
import math
import warnings
from collections import Counter
from datetime import datetime, timezone
from statistics import mean, median

from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_decision_metrics as metrics
from . import overnight_robustness_review as robustness
from . import temporal_validation as temporal


ROOT = storage.ROOT
VERSION = 'robust_overnight_models_v1'
SEEDS = (1729, 1730, 1731)
VARIANTS = (
    {'id': 'mlp_raw', 'kind': 'mlp', 'clip_target': False},
    {'id': 'mlp_clipped', 'kind': 'mlp', 'clip_target': True},
    {'id': 'huber_linear', 'kind': 'huber', 'clip_target': False},
    {'id': 'boost_net_median', 'kind': 'hist', 'clip_target': False},
)
MLP_PARAMS = {'hidden_layer_sizes': (16, 8), 'activation': 'tanh', 'solver': 'adam',
    'alpha': .1, 'batch_size': 256, 'learning_rate_init': .001, 'max_iter': 200,
    'shuffle': False, 'early_stopping': False, 'tol': 0., 'n_iter_no_change': 201}
HUBER_PARAMS = {'epsilon': 1.35, 'alpha': 10., 'max_iter': 1000, 'tol': 1e-6}
HIST_PARAMS = {**decision.REG_PARAMS, 'loss': 'absolute_error', 'random_state': 20261103}
CONTROL = robustness.SAVED
CONTROL_SHA = robustness.SAVED_SHA
RULES = {
    'data': 'same verified cached seven-stock EXCHANGE15m 17:45 base18 features; exact18open->nextcalendar10open net target',
    'folds': 'unchanged chronological2018+train/calibration/test2024/25/partial26; purge label_end at boundaries; no shuffle split',
    'fit_count': '24 fixed base fits:18MLP (2variants x3seeds x3folds),3Huber,3medianHGB; no search or seed selection',
    'weights': 'each known training date equal total weight; weight per row=(n/dates)/stocks_that_date; fit-only',
    'preprocessing': 'train-only weighted StandardScaler, featureclip+-8; original prices/labels never enter inference',
    'mlp': '18->16->8->1 tanh,200iterations,noearlystop; median of all3 fixed seeds; no winning seed selection',
    'clip_target': 'only mlp_clipped: winsorise y to train-only unweighted2.5/97.5percentiles; keep every row; test answers remain ORIGINAL',
    'alternatives': 'Huber linear epsilon1.35 alpha10; HGB absolute-error estimates net-return median, not old anchor median',
    'calibration': 'next chronological year only80pct absolute-residual interval; no weight/threshold/offset tuning',
    'policy': 'predictednet>.10pct, identical decisions in ordinary/stress accounts; no profit probabilities or new filters',
    'economics': '100000RUB/max25pct/stock/wholelots,longonly;fee.05pct+slip.05pct eachperside;stressdouble; selectedunknown poisons entire account',
    'diagnostics': 'baseline,quarters,omittedquarters,best1/3positivePnLdates omitted IN HINDSIGHT using frozen robustness review; not a tradable filter',
    'screen': '2025AND2026 each>=20candidate_dates,complete baseline and >0 baseline/dropbest1/dropbest3 at BOTH costlevels; all3years flag additionally requires2024 same',
    'control': 'old median3net ensemble replay at BOTH costlevels must reproduce published accounts including missing outcomes/drawdown',
    'warnings': 'finite models retained with convergence/iteration warnings disclosed; no refits or dropped poor variants',
    'reuse': 'all evaluated years previously viewed, exploratory not untouched holdout; no multiple-search correction or confidence-of-profit claim',
    'limits': 'current UID/lots/survivor universe; no historicalbook/news/dividendcash/tax/realexecution/worstnightdrawdown',
    'reserve': 'TATN/CHMF/PHOR unopened', 'entry_allowed': False,
}


def source_hashes():
    return {**robustness.source_hashes(), 'robust_overnight_models.py': hashlib.sha256(
        (ROOT/'dashboard/robust_overnight_models.py').read_bytes()).hexdigest()}


def freeze():
    spec = decision.jsonable({'version': VERSION, 'variants': VARIANTS, 'seeds': SEEDS,
        'mlp': MLP_PARAMS, 'huber': HUBER_PARAMS, 'hist': HIST_PARAMS, 'rules': RULES,
        'folds': temporal.FOLDS, 'features': decision.m.FEATURES, 'packages': storage.PACKAGES,
        'control_artifact_sha256': CONTROL_SHA, 'source_sha256': source_hashes()})
    path = ROOT/'data/robust-overnight-models-protocol-v1.json'
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Registered ML experiment changed; create another version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': spec}))
    return decision.m.digest(spec)


def matrix(rows):
    import numpy as np
    xs = [decision._features(row, 'x', len(decision.m.FEATURES)) for row in rows]
    return np.asarray(xs, dtype=float).reshape((-1, len(decision.m.FEATURES)))


def date_weights(rows):
    import numpy as np
    counts = Counter(robustness.day_of(r) for r in rows)
    if not counts:
        raise ValueError('Nonempty known training rows required')
    return np.asarray([len(rows)/(len(counts)*counts[robustness.day_of(r)]) for r in rows])


def predict(bundle, rows):
    """Reads X only: deliberately supports rows without entry/exit/target fields."""
    import numpy as np
    rows = list(rows)
    if not rows:
        return []
    x = np.clip(bundle['scaler'].transform(matrix(rows)), -8., 8.)
    members = np.asarray([estimator.predict(x) for estimator in bundle['estimators']])
    result = np.median(members, axis=0).tolist()
    if len(result) != len(rows) or not all(math.isfinite(v) for v in result):
        raise ValueError('Invalid net-return predictions')
    return result


def fit_variant(spec, train, calibration):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import HuberRegressor
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    if spec not in VARIANTS:
        raise ValueError('Unregistered algorithm')
    train, calibration = list(train), list(calibration)
    metrics.validate_rows(train); metrics.validate_rows(calibration)
    matrix(train); matrix(calibration)
    known = [r for r in train if r['net_y'] is not None]
    cal = [r for r in calibration if r['net_y'] is not None]
    if (len(known) < 200 or len(cal) < 50 or len({robustness.day_of(r) for r in known}) < 30
            or len({robustness.day_of(r) for r in cal}) < 10):
        raise ValueError('Insufficient chronological training/calibration data')
    if max(temporal._time(r['label_end']) for r in train) >= min(temporal._time(r['at']) for r in calibration):
        raise ValueError('Training labels overlap calibration')
    x, original_y, weights = matrix(known), np.asarray([r['net_y'] for r in known]), date_weights(known)
    scaler = StandardScaler().fit(x, sample_weight=weights)
    transformed = np.clip(scaler.transform(x), -8., 8.)
    bounds = np.quantile(original_y, [.025, .975]).tolist() if spec['clip_target'] else None
    y = np.clip(original_y, *bounds) if bounds is not None else original_y.copy()
    if spec['kind'] == 'mlp':
        estimators = [MLPRegressor(**MLP_PARAMS, random_state=seed) for seed in SEEDS]
    elif spec['kind'] == 'huber':
        estimators = [HuberRegressor(**HUBER_PARAMS)]
    else:
        estimators = [HistGradientBoostingRegressor(**HIST_PARAMS)]
    messages = []
    for index, estimator in enumerate(estimators):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            estimator.fit(transformed, y, sample_weight=weights)
        messages.extend({'member': index, 'category': type(w.message).__name__, 'message': str(w.message)} for w in caught)
    bundle = {'spec': spec, 'scaler': scaler, 'estimators': estimators,
        'target_clip_bounds': bounds, 'clipped_train_targets': int(np.count_nonzero(y != original_y)),
        'trained_rows': len(known), 'calibration_rows': len(cal), 'warnings': messages,
        'fit_iterations': [int(getattr(e, 'n_iter_', 0)) for e in estimators],
        'train_net_mean': float(original_y.mean()), 'train_net_median': median(original_y.tolist()),
        'train_profit_prior': mean(r['profit_y'] for r in known),
        'calibration_profit_prior': mean(r['profit_y'] for r in cal)}
    residuals = sorted(abs(r['net_y']-p) for r,p in zip(cal,predict(bundle,cal)))
    bundle['interval_abs_pct'] = residuals[min(len(residuals), math.ceil((len(residuals)+1)*.8))-1]
    predict(bundle, known)  # All fitted outputs must be finite before saving.
    return bundle


def score(bundle, rows):
    rows = list(rows)
    values = predict(bundle, [{'x': r['x']} for r in rows])
    report = metrics.evaluate(rows, values, None, bundle['train_net_mean'], bundle['train_profit_prior'],
        bundle['calibration_profit_prior'], train_net_median=bundle['train_net_median'])
    selected = [p > .10 for p in values]
    checks = [robustness.review(rows, selected, k) for k in (1,2)]
    errors = [abs(p-r['net_y']) for r,p in zip(rows,values) if r['net_y'] is not None]
    q = bundle['interval_abs_pct']
    report.update(interval_abs_pct=q, interval_coverage_pct=100*mean(e <= q for e in errors) if errors else None)
    return {'report': report, 'robustness': checks,
        'predictions': [{'ticker': r['ticker'], 'at': r['at'], 'predicted_net_pct': p,
                         'actual_net_pct': r['net_y'], 'selected': selected[i]}
                        for i,(r,p) in enumerate(zip(rows,values))]}


def passes(levels):
    if len(levels) != 2 or [v['cost_multiplier'] for v in levels] != [1,2]:
        return False
    for level in levels:
        base = level['baseline']['report']
        reports = [base]+[v['report'] for v in level['drop_best_dates']]
        if (base['selected_dates'] < 20 or len(reports) != 3
                or any(r['status'] != 'ok' or r['net_return_pct'] is None or r['net_return_pct'] <= 0 for r in reports)):
            return False
    return True


def verify_control(rows, fold):
    lookup = {(p['ticker'],p['at']): p for p in fold['predictions']}
    if len(lookup) != len(fold['predictions']) or set(lookup) != {(r['ticker'],r['at']) for r in rows}:
        raise ValueError('Saved control forecast population differs')
    points = [lookup[(r['ticker'],r['at'])] for r in rows]
    if any(p['actual_net_pct'] != r['net_y'] or p['selected_median'] != (p['median_net_pct'] > .1)
           for r,p in zip(rows,points)):
        raise ValueError('Saved control outcomes/decisions differ')
    levels = [robustness.review(rows, [p['selected_median'] for p in points], k) for k in (1,2)]
    for level,name in zip(levels,('paper','stress')):
        actual = level['baseline']['report']; old = fold['reports']['median']['policies']['regression'][name]
        for key in ('status','trades','selected_candidates','selected_dates','unknown_trades','net_return_pct','max_drawdown_pct'):
            a,b=actual[key],old[key]
            if not (a == b or type(a) is float and type(b) is float and math.isclose(a,b,abs_tol=1e-9)):
                raise ValueError('Saved control replay differs: '+key)
    return levels


def run():
    import joblib
    from threadpoolctl import threadpool_limits
    for name, required in storage.PACKAGES.items():
        if importlib.metadata.version(name) != required:
            raise ValueError('Requires pinned research dependency '+name+'=='+required)
    protocol = freeze()  # Before price/label cache reading or fitting.
    data = storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    old = storage.checked(ROOT/'dashboard/ai_models'/CONTROL)
    if (old['artifact_sha256'] != CONTROL_SHA or old['source_sha256'] != robustness.ensemble.source_hashes()
            or data['artifact_sha256'] != old['input_artifact_sha256']
            or data['source_sha256'] != storage.source_hashes()):
        raise ValueError('Old source/data provenance differs')
    rows = decision.enrich_rows(data['rows'])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder = ROOT/'data'/('robust-overnight-models-'+stamp); folder.mkdir()
    folds = []
    with threadpool_limits(limits=2):
        for fold in temporal.FOLDS:
            groups = temporal.group_rows(rows,fold['periods'])
            previous = next(f for f in old['folds'] if f['id'] == fold['id'])
            control = verify_control(groups['test'], previous)
            variants = []
            for spec in VARIANTS:
                print(fold['id'],spec['id'],'fit fixed train, calibrate interval, test original answers',flush=True)
                bundle = fit_variant(spec,groups['train'],groups['calibration'])
                result = score(bundle,groups['test'])
                path = folder/(fold['id']+'-'+spec['id']+'.joblib')
                joblib.dump(bundle,path,compress=3)
                if predict(joblib.load(path),[{'x':r['x']} for r in groups['test']]) != predict(bundle,groups['test']):
                    raise ValueError('Reloaded local weights changed predictions')
                variants.append({**spec, **{k:bundle[k] for k in ('target_clip_bounds','clipped_train_targets',
                    'trained_rows','calibration_rows','warnings','fit_iterations','train_net_mean','train_net_median',
                    'train_profit_prior','calibration_profit_prior')}, **result,
                    'weights_local_file': path.relative_to(ROOT).as_posix(),
                    'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                print('  MAE',round(result['report']['regression']['mae_pct'],6),
                    'returns',[v['baseline']['report']['net_return_pct'] for v in result['robustness']],
                    'drop3',[v['drop_best_dates'][-1]['report']['net_return_pct'] if v['drop_best_dates'] else None for v in result['robustness']],flush=True)
            folds.append({**fold,'control_robustness':control,'variants':variants})
    summary = []
    for identifier in [v['id'] for v in VARIANTS]+['old_median_control']:
        states = {}
        for fold in folds:
            levels = (fold['control_robustness'] if identifier == 'old_median_control' else
                next(v for v in fold['variants'] if v['id'] == identifier)['robustness'])
            states[fold['id']] = passes(levels)
        summary.append({'id':identifier,'year_screen_passes':states,
            'screen_2025_2026_passed': all(states[y] for y in ('test_2025','test_2026')),
            'all_three_years_passed':all(states.values()),'promoted_to_live':False})
    if source_hashes() != storage.checked(ROOT/'data/robust-overnight-models-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Registered source changed during experiment')
    path = ROOT/'dashboard/ai_models'/('robust-overnight-models-v1-'+stamp+'.json')
    storage.immutable(path,storage.signed({'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
        'rules':RULES,'entry_allowed':False,'historical':True,'prospective':False,'new_market_data':False,
        'protocol_sha256':protocol,'source_sha256':source_hashes(),'packages':storage.PACKAGES,
        'input_artifact_sha256':data['artifact_sha256'],'control_artifact_sha256':old['artifact_sha256'],
        'folds':folds,'summary':summary}))
    print('Saved',path.name,flush=True)
    print(summary,flush=True)
    return path


if __name__ == '__main__':
    run()
