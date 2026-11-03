"""Independent saved-model/conditional-payoff/ordinal/cash audit; no fits or network."""
import argparse
import hashlib
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.special import expit

from . import conditional_payoff_study as study


def inference(bundle, frozen, rows):
    """Use saved coefficients, manual sigmoid/projection, and feature-only inputs."""
    x = np.clip((np.asarray([r['u'] for r in rows]) - bundle['scaler'].mean_)
                / bundle['scaler'].scale_, -8, 8)
    scales = np.asarray([r['risk_scale'] for r in rows])
    bounds = np.asarray(bundle['support_bounds'])
    ridge = np.column_stack([(x @ m.coef_ + m.intercept_) * scales for m in bundle['ridge_heads']])
    boost = np.column_stack([m.predict(x) * scales for m in bundle['boost_heads']])
    ridge = np.clip(ridge, bounds[:, 0], bounds[:, 1])
    boost = np.clip(boost, bounds[:, 0], bounds[:, 1])
    old = np.asarray([p['p_classes'] for p in study.old_audit.inference(frozen, rows)])
    cumulative = []
    for model, cal in zip(bundle['ordinal_classifiers'], bundle['ordinal_calibrators']):
        assert list(model.classes_) == list(cal.classes_) == [0, 1]
        logits = model.decision_function(x).reshape(-1, 1) @ cal.coef_.T + cal.intercept_
        cumulative.append(expit(logits[:, 0]))
    raw = np.asarray(cumulative).T
    a, b = raw[:, 0].copy(), raw[:, 1].copy()
    crossed = b > a
    a[crossed] = b[crossed] = (a[crossed] + b[crossed]) / 2
    ordinal = np.column_stack((1 - a, a - b, b))
    maps = np.asarray([bundle['stock_maps'][study.TICKERS.index(r['ticker'])] for r in rows])
    estimates = np.column_stack((np.sum(old * ridge, axis=1), np.sum(old * boost, axis=1),
        np.sum(old * maps, axis=1), ordinal @ np.asarray(bundle['global_means']),
        np.sum(ordinal * maps, axis=1), np.sum(ordinal * boost, axis=1)))
    return [{'old_probs': old[i].tolist(), 'ordinal_probs': ordinal[i].tolist(),
        'ordinal_raw': raw[i].tolist(), 'ordinal_crossed': bool(crossed[i]),
        'ridge_values': ridge[i].tolist(), 'boost_values': boost[i].tolist(),
        'stock_values': maps[i].tolist(),
        'scores': dict(zip(study.SCORES, estimates[i].tolist()))} for i in range(len(rows))]


def selections(name, points, prior):
    score, guard = name.split('/')
    assert name in study.POLICIES
    limit = {'all': 1., 'fixed': .2, 'relative': .8 * prior}[guard]
    key = 'ordinal_probs' if score.startswith('ordinal_') else 'old_probs'
    return [p['scores'][score] > .1 and p[key][0] <= limit for p in points]


def train_state(bundle, frozen, train, calibration):
    """Recreate train-only statistics and check the Ridge normal equations."""
    counts = Counter(study.old.features.instant(r) for r in train)
    w = np.asarray([len(train) / (len(counts) * counts[study.old.features.instant(r)]) for r in train])
    raw = np.asarray([r['u'] for r in train])
    center = np.average(raw, axis=0, weights=w)
    variance = np.average((raw - center) ** 2, axis=0, weights=w)
    np.testing.assert_allclose(bundle['scaler'].mean_, center, rtol=0., atol=1e-10)
    np.testing.assert_allclose(bundle['scaler'].var_, variance, rtol=0., atol=1e-10)
    x = np.clip((raw - bundle['scaler'].mean_) / bundle['scaler'].scale_, -8, 8)
    y = np.asarray([r['net_y'] for r in train])
    labels = np.where(y < -.5, 0, np.where(y > .5, 2, 1))
    bounds = np.quantile(y, [.025, .975])
    clipped = np.clip(y, *bounds)
    target = clipped / np.asarray([r['risk_scale'] for r in train])
    np.testing.assert_allclose(bundle['payoff_clip_bounds'], bounds, rtol=0., atol=1e-12)
    means = [clipped[labels == c].mean() for c in range(3)]
    np.testing.assert_allclose(bundle['global_means'], means, rtol=0., atol=1e-12)
    np.testing.assert_allclose(frozen['class_means'], means, rtol=0., atol=1e-12)
    support = [[clipped[labels == c].min(), clipped[labels == c].max()] for c in range(3)]
    np.testing.assert_allclose(bundle['support_bounds'], support, rtol=0., atol=1e-12)
    assert bundle['class_counts'] == [int(sum(labels == c)) for c in range(3)]
    assert bundle['trained_rows'] == frozen['trained_rows'] == len(train)
    assert bundle['calibration_rows'] == frozen['calibration_rows'] == len(calibration)
    assert math.isclose(bundle['train_net_mean'], y.mean(), abs_tol=1e-12)
    assert math.isclose(bundle['train_net_median'], np.median(y), abs_tol=1e-12)
    assert math.isclose(bundle['calibration_loss_prior'],
        np.mean([r['net_y'] < -.5 for r in calibration]), abs_tol=1e-12)
    for ti, ticker in enumerate(study.TICKERS):
        for c in range(3):
            ix = [i for i, r in enumerate(train) if r['ticker'] == ticker and labels[i] == c]
            n = len(ix)
            expected = (clipped[ix].sum() + 100 * means[c]) / (n + 100)
            assert bundle['stock_counts'][ti][c] == n
            assert math.isclose(bundle['stock_maps'][ti][c], expected, abs_tol=1e-12)
    for c, model in enumerate(bundle['ridge_heads']):
        assert model.alpha == 100. and model.solver == 'svd'
        take = labels == c
        error = x[take] @ model.coef_ + model.intercept_ - target[take]
        np.testing.assert_allclose(x[take].T @ (w[take] * error) + 100 * model.coef_, 0., atol=1e-6)
        assert abs(np.sum(w[take] * error)) < 1e-6
    for key, params in (('boost_heads', study.REG_PARAMS), ('ordinal_classifiers', study.CLASS_PARAMS),
                        ('ordinal_calibrators', study.CAL_PARAMS)):
        for model in bundle[key]:
            actual = model.get_params()
            assert all(actual[k] == v for k, v in params.items())


def check_diagnostics(bundle, frozen, rows, points, saved):
    known = [(r, p) for r, p in zip(rows, points) if r['net_y'] is not None]
    y = np.asarray([r['net_y'] for r, p in known])
    labels = np.where(y < -.5, 0, np.where(y > .5, 2, 1))
    onehot = np.eye(3)[labels]
    for key in ('old_probs', 'ordinal_probs', 'baseline'):
        probabilities = (np.tile(frozen['calibration_class_priors'], (len(y), 1)) if key == 'baseline'
                         else np.asarray([p[key] for r, p in known]))
        brier = np.mean(np.sum((probabilities - onehot) ** 2, axis=1))
        loss = -np.mean(np.log(np.maximum(probabilities[np.arange(len(y)), labels], 1e-15)))
        assert math.isclose(brier, saved[key]['brier'], abs_tol=1e-12)
        assert math.isclose(loss, saved[key]['log_loss'], abs_tol=1e-12)
    assert saved['samples'] == len(known)
    assert saved['ordinal_raw_crossings'] == sum(p['ordinal_crossed'] for r, p in known)
    for name in study.SCORES:
        error = np.asarray([p['scores'][name] for r, p in known]) - y
        expected = [np.mean(np.abs(error)), np.sqrt(np.mean(error ** 2)),
                    np.mean(np.abs(y - bundle['train_net_mean'])), bundle['interval_abs_pct'][name],
                    100 * np.mean(np.abs(error) <= bundle['interval_abs_pct'][name])]
        actual = saved['scores'][name]
        np.testing.assert_allclose(expected, [actual[k] for k in ('mae_pct', 'rmse_pct',
            'mean_baseline_mae_pct', 'interval_abs_pct', 'interval_coverage_pct')], rtol=0., atol=1e-10)


def verify(path):
    import joblib
    from threadpoolctl import threadpool_limits
    storage = study.old.features.vol.prior.storage
    path = Path(path).resolve()
    assert path.parent == (study.ROOT / 'dashboard/ai_models').resolve()
    result = storage.checked(path)
    protocol = storage.checked(study.ROOT / 'data/conditional-payoff-protocol-v1.json')
    assert result['version'] == study.VERSION and result['entry_allowed'] is False
    assert result['prospective'] is False and result['new_market_data'] is False
    assert result['source_sha256'] == study.source_hashes() == protocol['spec']['source_sha256']
    assert result['protocol_sha256'] == storage.m.digest(protocol['spec'])
    data = storage.checked(study.ROOT / 'data/nonlinear-morning-inputs-v1.json')
    previous = storage.checked(study.ROOT / 'dashboard/ai_models' / study.PRIOR)
    assert data['artifact_sha256'] == result['input_artifact_sha256'] == previous['input_artifact_sha256']
    assert data['source_sha256'] == storage.source_hashes()
    assert previous['artifact_sha256'] == result['prior_artifact_sha256'] == study.PRIOR_SHA
    rows = study.old.prior_audit.recreate_features(data['rows'])
    files = estimators = reused = accounts = 0
    with threadpool_limits(limits=2):
        for fold in result['folds']:
            groups = study.old.features.vol.prior.temporal.group_rows(rows, fold['periods'])
            train = [r for r in groups['train'] if r['net_y'] is not None]
            cal = [r for r in groups['calibration'] if r['net_y'] is not None]
            test = groups['test']
            loaded = []
            for name in ('weights', 'frozen_model'):
                local = (study.ROOT / fold[name + '_local_file']).resolve()
                assert local.is_relative_to((study.ROOT / 'data').resolve())
                assert hashlib.sha256(local.read_bytes()).hexdigest() == fold[name + '_sha256']
                loaded.append(joblib.load(local))  # Own checksum-verified local outputs only.
                files += 1
            bundle, frozen = loaded
            assert Path(fold['weights_local_file']).name == fold['id'] + '.joblib'
            before = next(f for f in previous['folds'] if f['id'] == fold['id'])
            ref = next(m for m in before['models'] if m['family'] == 'payoff')
            assert (ref['weights_local_file'], ref['weights_sha256']) == (
                fold['frozen_model_local_file'], fold['frozen_model_sha256'])
            assert frozen['family'] == 'payoff'
            assert [len(bundle[k]) for k in ('ridge_heads', 'boost_heads', 'ordinal_classifiers',
                                            'ordinal_calibrators')] == [3, 3, 2, 2]
            estimators += 10
            reused += len(frozen['estimators'])
            train_state(bundle, frozen, train, cal)
            metadata = {k: v for k, v in bundle.items() if k not in ('scaler', 'ridge_heads', 'boost_heads',
                                                                 'ordinal_classifiers', 'ordinal_calibrators')}
            assert storage.m.digest(metadata) == storage.m.digest(fold['metadata'])
            cal_points = inference(bundle, frozen, cal)
            for name in study.SCORES:
                residuals = sorted(abs(r['net_y'] - p['scores'][name]) for r, p in zip(cal, cal_points))
                q = residuals[min(len(residuals), math.ceil((len(residuals) + 1) * .8)) - 1]
                assert math.isclose(q, bundle['interval_abs_pct'][name], abs_tol=1e-10)
            feature_rows = [{k: r[k] for k in ('u', 'market', 'risk_scale', 'ticker')} for r in test]
            points = inference(bundle, frozen, feature_rows)
            saved = {(p['ticker'], p['at']): p for p in fold['predictions']}
            old_saved = {(p['ticker'], p['at']): p for p in before['predictions']}
            assert len(points) == len(saved) == len(test) == len(fold['predictions']) == len(old_saved)
            for r, p in zip(test, points):
                s = saved[(r['ticker'], r['at'])]
                for key in ('old_probs', 'ordinal_probs', 'ordinal_raw', 'ridge_values', 'boost_values', 'stock_values'):
                    np.testing.assert_allclose(p[key], s[key], rtol=0., atol=1e-10)
                assert p['ordinal_crossed'] == s['ordinal_crossed']
                for name in study.SCORES:assert math.isclose(p['scores'][name], s['scores'][name], abs_tol=1e-10)
                assert s['actual_net_pct'] == r['net_y']
                o = old_saved[(r['ticker'], r['at'])]
                np.testing.assert_allclose(p['old_probs'], o['p_classes'], rtol=0., atol=1e-10)
                assert math.isclose(np.dot(p['old_probs'], bundle['global_means']), o['mean_net'], abs_tol=1e-10)
            for name in study.POLICIES:
                mask = selections(name, points, bundle['calibration_loss_prior'])
                assert mask == [saved[(r['ticker'], r['at'])]['selected_' + name] for r in test]
                policy = fold['policies'][name]
                accounts += study.old.features.independent.check_levels(test, mask, policy['robustness'])
                assert policy['year_screen_passed'] == study.old.features.independent.independent_passes(policy['robustness'])
            for name, field in (('old_payoff_mean', 'selected_payoff_mean'),
                                ('old_payoff_safe', 'selected_payoff_safe_mean'), ('old_median_control', 'base_net')):
                mask = [old_saved[(r['ticker'], r['at'])][field] if field != 'base_net'
                        else old_saved[(r['ticker'], r['at'])][field] > .1 for r in test]
                accounts += study.old.features.independent.check_levels(test, mask, fold['controls'][name])
            check_diagnostics(bundle, frozen, test, points, fold['diagnostics'])
    assert (files, estimators, reused, accounts) == (6, 30, 6, result['independent_cash_replays'])
    for s in result['summary']:
        states = {f['id']: study.old.features.independent.independent_passes(
            f['policies'][s['id']]['robustness'] if s['id'] in study.POLICIES else f['controls'][s['id']])
            for f in result['folds']}
        assert s['year_screen_passes'] == states
        assert s['screen_2025_2026_passed'] == all(states[y] for y in ('test_2025', 'test_2026'))
        assert s['all_three_years_passed'] == all(states.values()) and s['promoted_to_live'] is False
    audit = {'version': 'conditional_payoff_audit_v1', 'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'verified', 'result_artifact_sha256': result['artifact_sha256'],
        'input_artifact_sha256': data['artifact_sha256'], 'files': files, 'new_estimators': estimators,
        'reused_estimators': reused, 'cash_replays': accounts, 'entry_allowed': False,
        'verifier_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'checks': ['independent basket features/train scaler', 'train class support and stock shrinkage',
            'Ridge normal equations', 'manual Ridge/sigmoid/ordinal projection/expected payoffs',
            'calibration residual ranges and past loss prior', 'all18 selections incl unknown outcomes',
            'original MAE/RMSE/Brier/logloss/coverage', 'separate cash and best1/3/5 rankings',
            'three unchanged controls', 'independent robustness flags']}
    output = study.ROOT / 'data' / ('conditional-payoff-audit-v1-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.json')
    storage.immutable(output, storage.signed(audit))
    print('PASS', files, 'files;', estimators, 'new estimators,', reused, 'reused;', accounts,
          'cash replays; no fits;', output.name)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact')
    verify(parser.parse_args().artifact)
