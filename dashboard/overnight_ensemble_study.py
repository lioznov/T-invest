"""Fixed combinations of saved net-return models; no new fits or promotion."""
import hashlib
import math
from datetime import datetime, timezone
from statistics import mean, median

from . import morning_model as m
from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_decision_metrics as metrics
from . import temporal_validation as temporal

ROOT = storage.ROOT
VERSION = 'overnight_ensemble_v1'
PRIOR = 'overnight-decision-v1-20261025-081206-225930.json'
PRIOR_SHA = '2b9c00af9768dbe5719e38728150596028be4e93e5c7763ed8c2fc5ec7d5d097'
RULES = {
    'models': 'saved net_ridge/net_boost/peer_boost; all same past train/cal periods and net target',
    'forecasts': 'equal arithmetic mean; median; no learned weights or new fits',
    'policies': 'mean>.10percent; median>.10percent; third separate filter requires ALL3predictions>.10percent',
    'probabilities': 'not combined or used; agreement is not calibrated confidence',
    'economics': 'unchanged100000RUBcash,25percentmax/stock,wholelots,fees/slip.05percent eachside;stressx2same decisions',
    'unknowns': 'original eligible population retained; selectedunknown poisons whole subsequent account',
    'evaluation': 'same3temporaltestfolds,allresults published; base models and these testyears already inspected',
    'search': 'exactly3fixed rules; no threshold/weight search; no selection or automatic promotion',
    'limits': 'same survivor/currentlot/UID limitations; no dividend cash,tax,real fills,book/news or worstnightdrawdown',
    'reserve': 'TATN/CHMF/PHOR unopened', 'entry_allowed': False,
}


def source_hashes():
    return {**decision.source_hashes(), 'overnight_ensemble_study.py': hashlib.sha256(
        (ROOT/'dashboard'/'overnight_ensemble_study.py').read_bytes()).hexdigest()}


def freeze():
    spec = decision.jsonable({'version': VERSION, 'rules': RULES, 'folds': temporal.FOLDS,
                             'prior_artifact_sha256': PRIOR_SHA, 'source_sha256': source_hashes()})
    path = ROOT/'data'/'overnight-ensemble-protocol-v1.json'
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Frozen ensemble changed; create a new version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(),
                                              'spec': spec, 'entry_allowed': False}))
    return m.digest(spec)


def key(row):
    return row['ticker'], temporal._time(row['at']).astimezone(timezone.utc)


def combine(rows, variants):
    """Join by ticker/instant, never by incidental array position or future result."""
    rows = metrics.validate_rows(rows)
    if [v['id'] for v in variants] != [v['id'] for v in decision.VARIANTS]:
        raise ValueError('Expected the exact registered three net models')
    baseline = {name: variants[0][name] for name in (
        'train_net_mean', 'train_net_median', 'train_profit_prior', 'calibration_profit_prior')}
    population = {key(row) for row in rows}
    members = []
    for variant in variants:
        if any(variant[name] != value for name, value in baseline.items()):
            raise ValueError('Base models were not trained/calibrated on common labels')
        mapping = {key(p): p for p in variant['predictions']}
        if len(mapping) != len(variant['predictions']) or set(mapping) != population:
            raise ValueError('Model populations do not match')
        values = []
        for row in rows:
            saved = mapping[key(row)]
            if saved['actual_net_pct'] != row['net_y']:
                raise ValueError('Saved actual net result differs')
            value = saved['predicted_net_pct']
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError('Each model requires finite predictions')
            values.append(value)
        members.append(values)
    vectors = list(zip(*members))
    point_mean = [mean(values) for values in vectors]
    point_median = [median(values) for values in vectors]
    unanimous = [all(value > .10 for value in values) for values in vectors]
    reports = {}
    for name, values in (('mean', point_mean), ('median', point_median)):
        reports[name] = metrics.evaluate(rows, values, None, baseline['train_net_mean'],
            baseline['train_profit_prior'], baseline['calibration_profit_prior'],
            train_net_median=baseline['train_net_median'])
    decisions = [1. if selected else 0. for selected in unanimous]
    unanimous_report = {'paper': metrics.paper_predictions(rows, decisions, threshold=.5),
        'stress': metrics.paper_predictions(rows, decisions, multiplier=2, threshold=.5)}
    forecasts = [{'ticker': row['ticker'], 'at': row['at'], 'actual_net_pct': row['net_y'],
        'member_predictions': list(values), 'mean_net_pct': a, 'median_net_pct': b,
        'selected_mean': a > .10, 'selected_median': b > .10, 'selected_unanimous': selected}
        for row, values, a, b, selected in zip(rows, vectors, point_mean, point_median, unanimous)]
    return {'reports': reports, 'unanimous': unanimous_report, 'predictions': forecasts}


def run():
    protocol = freeze()  # Before reading saved prices/forecasts.
    prior = storage.checked(ROOT/'dashboard'/'ai_models'/PRIOR)
    dataset = storage.checked(ROOT/'data'/'nonlinear-morning-inputs-v1.json')
    if (prior['artifact_sha256'] != PRIOR_SHA or prior['source_sha256'] != decision.source_hashes()
            or dataset['artifact_sha256'] != prior['input_artifact_sha256']
            or dataset['source_sha256'] != storage.source_hashes()
            or [f['id'] for f in prior['folds']] != [f['id'] for f in temporal.FOLDS]):
        raise ValueError('Registered source or input chain changed')
    rows = decision.enrich_rows(dataset['rows'])
    folds = []
    for fold, previous in zip(temporal.FOLDS, prior['folds']):
        test = temporal.select_rows(rows, fold['periods']['test'])
        result = combine(test, previous['variants'])
        # Monetary controls use the same signed population and frozen selections.
        controls = {v['id']: v['report']['policies'] for v in previous['variants']}
        folds.append({**fold, **result, 'controls': controls, 'anchor_control': previous['old_control']})
        print(fold['id'], {name: report['policies']['regression']['paper']['net_return_pct']
                          for name, report in result['reports'].items()},
              'unanimous', result['unanimous']['paper']['net_return_pct'], flush=True)
    if source_hashes() != storage.checked(ROOT/'data'/'overnight-ensemble-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Source changed during evaluation')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    path = ROOT/'dashboard'/'ai_models'/f'overnight-ensemble-v1-{stamp}.json'
    storage.immutable(path, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'rules': RULES, 'entry_allowed': False, 'protocol_sha256': protocol, 'source_sha256': source_hashes(),
        'prior_artifact_sha256': PRIOR_SHA, 'input_artifact_sha256': dataset['artifact_sha256'], 'folds': folds}))
    print('Saved', path.name, flush=True)
    return path


if __name__ == '__main__':
    run()
