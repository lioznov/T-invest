"""Fixed quarterly 24/48-month overnight retraining; research only.

python -m dashboard.quarterly_overnight_study
Uses signed existing examples, no broker calls, search, promotion or orders.
"""
import hashlib
import importlib.metadata
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from . import morning_model as m
from . import temporal_validation as temporal
from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_decision_metrics as metrics


ROOT = storage.ROOT
VERSION = 'quarterly_overnight_v1'
WINDOWS = ({'id': 'recent_24m', 'months': 24}, {'id': 'recent_48m', 'months': 48})
MODEL_SPEC = {'id': 'net_boost', 'kind': 'hist', 'features': 'base'}
INPUT_SHA = '990ccf99e5cd378da2505b7309b68115ffdf5201cb93f4c088de57f9416b897b'
PRIOR_NAME = 'overnight-decision-v1-20261025-081206-225930.json'
PRIOR_SHA = '2b9c00af9768dbe5719e38728150596028be4e93e5c7763ed8c2fc5ec7d5d097'
ANCHOR_NAME = 'nonlinear-morning-v1-20261024-082555-671799.json'
ANCHOR_SHA = 'd74e09a4ad188445747267561d7e318c229e18f00f39a66a6a70c2ea9dbe9c0f'
PROTOCOL_NAME = 'quarterly-overnight-protocol-v1.json'
RULES = {
    'task': 'unchanged17:45base18 -> modeled18:00entry/nextcalendar10:00exit netreturn andP(net>0)',
    'windows': 'exactly24or48calendar months ending3months before eachquarterorigin; preceding3months calibrate',
    'origins': 'Jan1/Apr1/Jul1/Oct1 in2024,2025,2026; quarterly models may use earlier test-year outcomes alreadyknown',
    'purging': 'train/cal label_end strictlybefore theirend; forecasttime partitions outertest rows, no extra quarter-end purge',
    'population': 'exact previous temporalFOLDS testpopulation; same rows for bothwindows andfixedcontrols; unknownsretained',
    'fit_budget': '24bundles=48HGBbasefits+24chronologicalsigmoidfits; no search/earlystop/extra retries or winnerselection',
    'model': 'frozen overnight_decision_study.fit_variant net_boost; original18features; no peerfeatures added',
    'policies': 'net>.10percent; joint net>.10percent AND calibratedprobability>=.55; fixed normalcost target',
    'paper': 'unchanged100000RUBwhole-lotcashaccount,max25percent/stock;fees/slip .05percent eachside;stressx2same decisions',
    'unknowns': 'missing entry/exit stays unknown; selected unknown poisons account subsequently; no inventedzero returns',
    'aggregate_baselines': 'FIRST quarter trainmean/trainmedian andtrain/calprofitprior fixed for entiretestyear; knownJan1',
    'adaptive_baselines': 'also pairedagainst eachrow quarterknown trainmean/median andtrain/calprofitprior, no extra fitting',
    'quarter_baselines': 'eachquarter uses itsown past train/cal statistics; quarteraccounts diagnostic andNOT summed',
    'aggregate_account': 'single full-year account from allquarterforecasts, noreset atquarterboundaries; eachyear starts100000',
    'interval': 'eachrow retains itsownquarter80percentcalibratedabsoluteerrorradius; no singleconstant annualradius',
    'comparison': 'fixed oldnet_boost forecasts checked by key,label,weightsinference,fullreport replay; oldanchorcontrol samepopulation',
    'reuse': '2024-2026 already inspected; updating withinyear iscausal butnotnewindependentholdout; reserveunopened',
    'limits': 'samecurrentstocks/UID/lots, no dividends/taxes/book/news/verifiedfills/overnightworstdrawdown',
    'entry_allowed': False,
}


def source_hashes():
    return {**decision.source_hashes(),
            'quarterly_overnight_study.py': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            '../QUARTERLY_OVERNIGHT_PROTOCOL_2026-10-25.md': hashlib.sha256(
                (ROOT/'QUARTERLY_OVERNIGHT_PROTOCOL_2026-10-25.md').read_bytes()).hexdigest()}


def freeze():
    spec = decision.jsonable({'version': VERSION, 'windows': WINDOWS, 'model': MODEL_SPEC,
        'folds': temporal.FOLDS, 'rules': RULES, 'packages': storage.PACKAGES,
        'input_sha256': INPUT_SHA, 'prior_sha256': PRIOR_SHA, 'anchor_sha256': ANCHOR_SHA,
        'source_sha256': source_hashes()})
    path = ROOT/'data'/PROTOCOL_NAME
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Quarterly protocol changed; create a new version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(),
                                               'spec': spec, 'entry_allowed': False}))
    return m.digest(spec)


def shift_months(at, months):
    """Calendar-month arithmetic for first-of-month boundaries only."""
    if at.day != 1 or at.hour or at.minute or at.second or at.microsecond or at.tzinfo is None:
        raise ValueError('Expected aware first-of-month midnight')
    year, month = divmod(at.year * 12 + at.month - 1 + months, 12)
    return at.replace(year=year, month=month+1)


def quarters(period, months):
    if months not in (24, 48):
        raise ValueError('Unregistered lookback')
    begin, end = temporal._bounds(period)
    if begin.month != 1 or begin.day != 1 or end > begin.replace(year=begin.year+1):
        raise ValueError('Expected one original calendar-year test period')
    result = []
    origin = begin
    while origin < end:
        calibration_start = shift_months(origin, -3)
        until = min(shift_months(origin, 3), end)
        result.append({'id': f'{origin.year}Q{(origin.month-1)//3+1}', 'origin': origin.isoformat(),
            'train': {'start': shift_months(calibration_start, -months).date().isoformat(),
                      'end_exclusive': calibration_start.date().isoformat()},
            'calibration': {'start': calibration_start.date().isoformat(), 'end_exclusive': origin.date().isoformat()},
            'test': {'start': origin.date().isoformat(), 'end_exclusive': until.date().isoformat()}})
        origin = shift_months(origin, 3)
    return result


def training_groups(rows, quarter):
    groups = {name: temporal.select_rows(rows, quarter[name]) for name in ('train', 'calibration')}
    origin = temporal._time(quarter['origin'])
    cal_start, cal_end = temporal._bounds(quarter['calibration'])
    _, train_end = temporal._bounds(quarter['train'])
    if train_end != cal_start or cal_end != origin:
        raise ValueError('Train/calibration must precede and meet the quarter origin')
    if (any(temporal._time(r['label_end']) >= cal_start for r in groups['train'])
            or any(temporal._time(r['label_end']) >= origin for r in groups['calibration'])):
        raise ValueError('Future outcome entered a fitting partition')
    return groups


def quarter_rows(outer_rows, quarter):
    """Do not purge next-quarter labels from already selected outer test rows."""
    begin, end = temporal._bounds(quarter['test'])
    return [r for r in outer_rows if begin <= temporal._time(r['at']) < end]


def key(row):
    return row['ticker'], temporal._time(row['at']).astimezone(timezone.utc)


def baselines(bundle):
    return {name: bundle[name] for name in ('train_net_mean', 'train_net_median',
                                           'train_profit_prior', 'calibration_profit_prior')}


def evaluate(rows, net, probabilities, baseline):
    return metrics.evaluate(rows, net, probabilities, baseline['train_net_mean'],
        baseline['train_profit_prior'], baseline['calibration_profit_prior'],
        train_net_median=baseline['train_net_median'])


def aggregate(outer_rows, quarter_results):
    forecasts = [item for quarter in quarter_results for item in quarter['predictions']]
    by_key = {key(item): item for item in forecasts}
    if len(by_key) != len(forecasts) or set(by_key) != {key(row) for row in outer_rows}:
        raise ValueError('Quarterly predictions duplicate or omit original test rows')
    forecasts = [by_key[key(row)] for row in outer_rows]
    for row, item in zip(outer_rows, forecasts):
        if item['actual_net_pct'] != row['net_y']:
            raise ValueError('Saved outcome no longer matches the original test row')
    reference = quarter_results[0]
    baseline = reference['baselines']
    report = evaluate(outer_rows, [item['predicted_net_pct'] for item in forecasts],
                      [item['probability_profit'] for item in forecasts], baseline)
    known = [(row, item) for row, item in zip(outer_rows, forecasts) if row['net_y'] is not None]
    report['interval_coverage_pct'] = (100 * mean(abs(row['net_y']-item['predicted_net_pct']) <= item['interval_abs_pct']
                                                for row, item in known) if known else None)
    by_origin = {quarter['origin']: quarter['baselines'] for quarter in quarter_results}
    if len(by_origin) != len(quarter_results):
        raise ValueError('Duplicate baseline quarter origin')
    adaptive = [by_origin[item['quarter_origin']] for row, item in known]
    mean_errors = [row['net_y']-base['train_net_mean'] for (row, item), base in zip(known, adaptive)]
    median_errors = [row['net_y']-base['train_net_median'] for (row, item), base in zip(known, adaptive)]
    labels = [row['profit_y'] for row, item in known]
    train_probabilities = [base['train_profit_prior'] for base in adaptive]
    cal_probabilities = [base['calibration_profit_prior'] for base in adaptive]
    mae_differences = [abs(row['net_y']-item['predicted_net_pct'])-abs(error)
                       for (row, item), error in zip(known, mean_errors)]
    brier_differences = [(item['probability_profit']-row['profit_y'])**2-(prior-row['profit_y'])**2
                         for (row, item), prior in zip(known, cal_probabilities)]
    summarize_errors = lambda values: {'mae_pct': mean(abs(v) for v in values) if values else None,
        'rmse_pct': math.hypot(*values)/math.sqrt(len(values)) if values else None}
    report['adaptive_baselines'] = {
        'note': 'Each row uses the past-only baseline of its own quarter, not the later full-year statistic.',
        'train_mean': summarize_errors(mean_errors), 'train_median': summarize_errors(median_errors),
        'train_prior': metrics._probability_metrics(labels, train_probabilities),
        'calibration_prior': metrics._probability_metrics(labels, cal_probabilities),
        'mae_difference_bootstrap': metrics._paired_bootstrap([row for row, item in known], mae_differences, 'percentage_points'),
        'brier_difference_bootstrap': metrics._paired_bootstrap([row for row, item in known], brier_differences, 'brier'),
    }
    return {'report': report, 'predictions': forecasts,
            'baseline_reference': {'quarter': reference['id'], 'origin': reference['origin'],
                                   'train': reference['train'], 'calibration': reference['calibration'], **baseline}}


def run_window(rows, fold, window, save=None):
    """One four-quarter strategy; injected saver supports synthetic-only checks."""
    if window not in WINDOWS:
        raise ValueError('Unregistered window')
    outer = temporal.select_rows(rows, fold['periods']['test'])
    completed = []
    for quarter in quarters(fold['periods']['test'], window['months']):
        test = quarter_rows(outer, quarter)
        if not test:
            raise ValueError('Expected nonempty original test population in every registered quarter')
        groups = training_groups(rows, quarter)
        bundle = decision.fit_variant(MODEL_SPEC, groups['train'], groups['calibration'])
        scored = decision.score(bundle, test)
        forecasts = [{**item, 'quarter_origin': quarter['origin'], 'interval_abs_pct': bundle['interval_abs_pct']}
                     for item in scored['predictions']]
        record = {**quarter, 'trained_rows': bundle['trained_rows'], 'calibration_rows': bundle['calibration_rows'],
                  'baselines': baselines(bundle), 'report': scored['report'], 'predictions': forecasts}
        if save is not None:
            record.update(save(fold['id'], window['id'], quarter['id'], bundle, test))
        completed.append(record)
    if len(completed) != 4:
        raise ValueError('Registered test year must have four quarter origins')
    return {**window, 'quarters': completed, **aggregate(outer, completed)}


def _assert_same(actual, expected):
    if isinstance(actual, dict):
        if not isinstance(expected, dict) or not set(actual) <= set(expected):
            raise ValueError('Control report fields differ')
        for name in actual:
            _assert_same(actual[name], expected[name])
    elif isinstance(actual, list):
        if not isinstance(expected, list) or len(actual) != len(expected):
            raise ValueError('Control report lengths differ')
        for a, b in zip(actual, expected):
            _assert_same(a, b)
    elif type(actual) in (int, float) and type(expected) in (int, float):
        if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError('Control report values differ')
    elif actual != expected:
        raise ValueError('Control report values differ')


def _weight_path(record):
    path = (ROOT/record['weights_local_file']).resolve()
    if not path.is_relative_to((ROOT/'data').resolve()) or path.suffix != '.joblib':
        raise ValueError('Unexpected local generated weight path')
    if hashlib.sha256(path.read_bytes()).hexdigest() != record['weights_sha256']:
        raise ValueError('Generated weights checksum mismatch')
    return path


def fixed_control(prior, anchor, fold, outer, audit_weights=True):
    if [item['id'] for item in prior['folds']] != [item['id'] for item in temporal.FOLDS]:
        raise ValueError('Prior fold set differs')
    previous = next(item for item in prior['folds'] if item['id'] == fold['id'])
    if [item['id'] for item in previous['variants']] != [item['id'] for item in decision.VARIANTS]:
        raise ValueError('Prior variant set differs')
    model = previous['variants'][1]
    by_key = {key(item): item for item in model['predictions']}
    if len(by_key) != len(model['predictions']) or set(by_key) != {key(row) for row in outer}:
        raise ValueError('Fixed model test population differs')
    forecasts = [by_key[key(row)] for row in outer]
    for row, item in zip(outer, forecasts):
        if item['actual_net_pct'] != row['net_y']:
            raise ValueError('Fixed model actual label differs')
        if (item['selected_regression'] != (item['predicted_net_pct'] > .10)
                or item['selected_joint'] != (item['predicted_net_pct'] > .10 and item['probability_profit'] >= .55)):
            raise ValueError('Fixed selection differs from saved forecasts')
    net = [item['predicted_net_pct'] for item in forecasts]
    probabilities = [item['probability_profit'] for item in forecasts]
    if audit_weights:
        import joblib
        # This exact locally generated artifact was fixed by checksum in the protocol.
        reloaded_net, reloaded_probs = decision.predict(joblib.load(_weight_path(model)), outer)
        _assert_same(reloaded_net, net); _assert_same(reloaded_probs, probabilities)
    report = evaluate(outer, net, probabilities, baselines(model))
    _assert_same(report, model['report'])
    anchor_replay = decision.old_control(anchor, fold, outer)
    _assert_same(anchor_replay, previous['old_control'])
    return {'matched': True, 'net_boost': {'report': report, 'predictions': forecasts}, 'anchor_ridge': anchor_replay}


def load_inputs():
    dataset = storage.checked(ROOT/'data'/'nonlinear-morning-inputs-v1.json')
    prior = storage.checked(ROOT/'dashboard'/'ai_models'/PRIOR_NAME)
    anchor = storage.checked(ROOT/'dashboard'/'ai_models'/ANCHOR_NAME)
    for obj, expected in ((dataset, INPUT_SHA), (prior, PRIOR_SHA), (anchor, ANCHOR_SHA)):
        if obj['artifact_sha256'] != expected:
            raise ValueError('Registered input or previous result changed')
    if (dataset['source_sha256'] != storage.source_hashes() or anchor['source_sha256'] != storage.source_hashes()
            or prior['source_sha256'] != decision.source_hashes()
            or prior['input_artifact_sha256'] != INPUT_SHA or anchor['input_artifact_sha256'] != INPUT_SHA
            or prior['prior_artifact_sha256'] != ANCHOR_SHA):
        raise ValueError('Frozen source or artifact chain changed')
    for artifact, protocol_name in ((prior, 'overnight-decision-protocol-v1.json'),
                                    (anchor, 'nonlinear-morning-protocol-v1.json')):
        registered = storage.checked(ROOT/'data'/protocol_name)
        if (m.digest(registered['spec']) != artifact['protocol_sha256']
                or registered['spec']['source_sha256'] != artifact['source_sha256']):
            raise ValueError('Previous protocol signature or source chain differs')
    return dataset, prior, anchor


def run():
    import joblib
    from threadpoolctl import threadpool_limits
    for name, version in storage.PACKAGES.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f'Requires {name}=={version}')
    protocol = freeze()  # Before opening examples, old predictions or weights.
    dataset, prior, anchor = load_inputs()
    rows = decision.enrich_rows(dataset['rows'])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    directory = ROOT/'data'/f'quarterly-overnight-models-{stamp}'
    directory.mkdir()
    saved = []

    def save(fold_id, window_id, quarter_id, bundle, test):
        path = directory/f'{fold_id}-{window_id}-{quarter_id}.joblib'
        joblib.dump(bundle, path, compress=3)
        reloaded = decision.predict(joblib.load(path), test)
        expected = decision.predict(bundle, test)
        _assert_same(reloaded[0], expected[0]); _assert_same(reloaded[1], expected[1])
        saved.append(path)
        print(f'{fold_id}/{window_id}/{quarter_id}: train={bundle["trained_rows"]}, cal={bundle["calibration_rows"]}, test={len(test)}', flush=True)
        return {'weights_local_file': path.relative_to(ROOT).as_posix(),
                'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    folds = []
    with threadpool_limits(limits=2):
        for fold in temporal.FOLDS:
            outer = temporal.select_rows(rows, fold['periods']['test'])
            control = fixed_control(prior, anchor, fold, outer)
            variants = [run_window(rows, fold, window, save) for window in WINDOWS]
            folds.append({**fold, 'variants': variants, 'fixed_control': control, 'always_buy': metrics.always_buy(outer)})
    if len(saved) != 24 or sum(len(v['quarters']) for fold in folds for v in fold['variants']) != 24:
        raise ValueError('Registered 24-bundle fitting budget not fulfilled')
    if source_hashes() != storage.checked(ROOT/'data'/PROTOCOL_NAME)['spec']['source_sha256']:
        raise ValueError('Research sources changed during execution')
    output = ROOT/'dashboard'/'ai_models'/f'quarterly-overnight-v1-{stamp}.json'
    storage.immutable(output, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'entry_allowed': False, 'rules': RULES, 'protocol_sha256': protocol, 'source_sha256': source_hashes(),
        'packages': storage.PACKAGES, 'input_artifact_sha256': INPUT_SHA, 'prior_artifact_sha256': PRIOR_SHA,
        'anchor_artifact_sha256': ANCHOR_SHA, 'fit_counts': {'bundles': 24, 'hgb_models': 48, 'sigmoid_calibrations': 24},
        'folds': folds, 'eligible_input_rows': len(rows)}))
    print(f'Saved {output.name}', flush=True)
    return output


if __name__ == '__main__':
    run()
