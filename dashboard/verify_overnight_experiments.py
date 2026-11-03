"""Read-only independent replay of the registered quarterly/ensemble results.

Loads only checksum-verified weights generated locally by these experiments.
Never fits, fetches data, places orders or edits experiment artifacts.
"""
import hashlib
import math
from collections import defaultdict
from datetime import datetime

from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import quarterly_overnight_study as quarterly
from . import overnight_ensemble_study as ensemble
from . import temporal_validation as temporal


def close(a, b):
    if a is None or b is None:
        assert a is None and b is None, (a, b)
    else:
        assert math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-11), (a, b)


def account(rows, selected, multiplier):
    """Separate cash replay, independent of the production research simulator."""
    grouped = defaultdict(list)
    for row, choose in zip(rows, selected):
        if choose:
            grouped[datetime.fromisoformat(row['at']).astimezone(decision.m.MSK).date()].append(row)
    cash = peak = 100000.
    drawdown = 0.
    trades = unknown = 0
    halted = False
    fee = slip = .0005 * multiplier
    for day in sorted(grouped):
        chosen = grouped[day]
        absent = sum(row['entry_price'] is None or row['actual_price'] is None for row in chosen)
        unknown += absent
        if absent:
            halted = True
        if halted:
            continue
        budget = min(cash / len(chosen), cash / 4)
        proceeds = 0.
        for row in chosen:
            purchase = row['entry_price'] * (1+slip) * (1+fee)
            lots = math.floor(budget / (purchase * row['lot']))
            if lots < 1:
                continue
            trades += 1
            quantity = lots * row['lot']
            proceeds += quantity * (row['actual_price'] * (1-slip) * (1-fee)-purchase)
        cash += proceeds
        peak = max(peak, cash)
        drawdown = max(drawdown, (peak-cash)/peak*100)
    return {'trades': trades, 'unknown_trades': unknown, 'selected_candidates': sum(map(len, grouped.values())),
        'selected_dates': len(grouped), 'status': 'incomplete' if halted else 'ok',
        'net_return_pct': 100*(cash/100000-1) if trades and not halted else None,
        'max_drawdown_pct': drawdown if trades and not halted else None}


def check_policy(rows, selected, saved):
    for name, multiplier in (('paper', 1), ('stress', 2)):
        result = account(rows, selected, multiplier)
        for field, value in result.items():
            if field in ('net_return_pct', 'max_drawdown_pct'):
                close(value, saved[name][field])
            else:
                assert value == saved[name][field], (field, value, saved[name][field])


def verify():
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    root = storage.ROOT
    q = storage.checked(root/'dashboard/ai_models/quarterly-overnight-v1-20261025-222844-080536.json')
    e = storage.checked(root/'dashboard/ai_models/overnight-ensemble-v1-20261026-072149-981644.json')
    prior = storage.checked(root/'dashboard/ai_models'/ensemble.PRIOR)
    assert prior['artifact_sha256'] == ensemble.PRIOR_SHA == e['prior_artifact_sha256']
    data = storage.checked(root/'data/nonlinear-morning-inputs-v1.json')
    assert q['source_sha256'] == quarterly.source_hashes()
    assert e['source_sha256'] == ensemble.source_hashes()
    for result, filename in ((q, quarterly.PROTOCOL_NAME), (e, 'overnight-ensemble-protocol-v1.json')):
        protocol = storage.checked(root/'data'/filename)
        assert decision.m.digest(protocol['spec']) == result['protocol_sha256']
        assert protocol['spec']['source_sha256'] == result['source_sha256']
        assert result['entry_allowed'] is False
        assert data['artifact_sha256'] == result['input_artifact_sha256']
    rows = decision.enrich_rows(data['rows'])
    bundles = policies = 0
    with threadpool_limits(limits=2):
        for fold in q['folds']:
            outer = temporal.select_rows(rows, fold['periods']['test'])
            for variant in fold['variants']:
                aggregate = {quarterly.key(p): p for p in variant['predictions']}
                assert len(aggregate) == len(outer)
                for record in variant['quarters']:
                    origin = temporal._time(record['origin'])
                    training = temporal.select_rows(rows, record['train'])
                    calibration = temporal.select_rows(rows, record['calibration'])
                    assert all(temporal._time(r['label_end']) < temporal._bounds(record['calibration'])[0] for r in training)
                    assert all(temporal._time(r['label_end']) < origin for r in calibration)
                    assert sum(r['net_y'] is not None for r in training) == record['trained_rows']
                    assert sum(r['net_y'] is not None for r in calibration) == record['calibration_rows']
                    test = quarterly.quarter_rows(outer, record)
                    path = (root/record['weights_local_file']).resolve()
                    assert path.is_relative_to((root/'data').resolve())
                    assert hashlib.sha256(path.read_bytes()).hexdigest() == record['weights_sha256']
                    bundle = joblib.load(path)
                    x = np.asarray([r['x'] for r in test], dtype=float)
                    net = bundle['regressor'].predict(x)
                    raw = bundle['classifier'].decision_function(x).reshape(-1, 1)
                    probability = bundle['calibrator'].predict_proba(raw)[:, 1]
                    for row, a, b in zip(test, net, probability):
                        saved = aggregate[quarterly.key(row)]
                        assert saved['quarter_origin'] == record['origin']
                        close(saved['actual_net_pct'], row['net_y'])
                        close(float(a), saved['predicted_net_pct'])
                        close(float(b), saved['probability_profit'])
                        assert saved['selected_regression'] == (a > .1)
                        assert saved['selected_joint'] == (a > .1 and b >= .55)
                    bundles += 1
                forecasts = [aggregate[quarterly.key(row)] for row in outer]
                for name in ('regression', 'joint'):
                    mask = [p['predicted_net_pct'] > .1 and (name != 'joint' or p['probability_profit'] >= .55)
                            for p in forecasts]
                    check_policy(outer, mask, variant['report']['policies'][name]); policies += 1
        for fold in e['folds']:
            outer = temporal.select_rows(rows, fold['periods']['test'])
            saved = {ensemble.key(p): p for p in fold['predictions']}
            assert len(saved) == len(outer)
            old_fold = next(f for f in prior['folds'] if f['id'] == fold['id'])
            members = [{ensemble.key(p): p for p in v['predictions']} for v in old_fold['variants']]
            computed = {'mean': [], 'median': [], 'unanimous': []}
            for row in outer:
                item = saved[ensemble.key(row)]
                values = [member[ensemble.key(row)]['predicted_net_pct'] for member in members]
                for a, b in zip(values, item['member_predictions']):
                    close(a, b)
                a, b = math.fsum(values)/3, sorted(values)[1]
                close(a, item['mean_net_pct']); close(b, item['median_net_pct'])
                close(row['net_y'], item['actual_net_pct'])
                masks = {'mean': a > .1, 'median': b > .1, 'unanimous': min(values) > .1}
                for name, choose in masks.items():
                    assert choose == item['selected_'+name]
                    computed[name].append(choose)
            for name in ('mean', 'median', 'unanimous'):
                mask = computed[name]
                report = fold['unanimous'] if name == 'unanimous' else fold['reports'][name]['policies']['regression']
                check_policy(outer, mask, report); policies += 1
    assert bundles == 24 and policies == 21
    print(f'PASS: signatures/source chains, {bundles} local weight bundles, {policies*2} independent cash replays; no fits')


if __name__ == '__main__':
    verify()
