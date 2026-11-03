"""Cost sensitivity of the fixed median ensemble, on archived real prices.

This is a retrospective diagnostic of the same signals, not new market data,
new fitted models, threshold tuning, or prospective profit evidence.
"""
import hashlib
import math
from datetime import datetime, timezone

from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_ensemble_study as ensemble
from . import overnight_decision_metrics as metrics
from . import temporal_validation as temporal

ROOT = storage.ROOT
VERSION = 'overnight_cost_review_v1'
LEVELS = (.5, 1., 1.5, 2., 2.5, 3.)
SAVED = 'overnight-ensemble-v1-20261026-072149-981644.json'
SAVED_SHA = '1fe620843c5031586c2c28a90536035bad900db224c92e817f9d12a2afa650c4'
RULES = {
    'policy': 'saved medianensemble, unchanged decisions >.10percent net forecast; no refit/search',
    'costs': 'six fixed multipliers .5/1/1.5/2/2.5/3 of fee.05percent andslip.05percent eachperside',
    'execution': '100000RUBcash, wholelots,max25percent/stock; preserve unknownselectedoutcomes',
    'control': 'multipliers1and2 must match saved standard/stress outcomes including trades/status/counts',
    'scope': 'same alreadyexamined2024/25/26 T-Bank historical EXCHANGE candles, NOT fresh quotes or new proof',
    'limits': 'currentlots/UID/survivors; no dividends/tax/truefills/worstnightdrawdown; no exact break-even guarantee',
    'entry_allowed': False,
}


def source_hashes():
    return {**ensemble.source_hashes(), 'overnight_cost_review.py': hashlib.sha256(
        (ROOT/'dashboard/overnight_cost_review.py').read_bytes()).hexdigest()}


def freeze():
    spec = {'version': VERSION, 'levels': list(LEVELS), 'rules': RULES,
            'saved_artifact_sha256': SAVED_SHA, 'source_sha256': source_hashes()}
    path = ROOT/'data/overnight-cost-review-protocol-v1.json'
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Cost review changed; use a new version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': spec}))
    return decision.m.digest(spec)


def curve(rows, selection):
    rows, selection = list(rows), list(selection)
    if len(rows) != len(selection) or any(type(v) is not bool for v in selection):
        raise ValueError('Exactly one fixed boolean decision per row is required')
    encoded = [float(v) for v in selection]
    return [{'multiplier': k, 'fee_pct_per_side': .05*k, 'slippage_pct_per_side': .05*k,
             'approx_roundtrip_cost_pct': .2*k,
             'paper': metrics.paper_predictions(rows, encoded, multiplier=k, threshold=.5)} for k in LEVELS]


def run():
    protocol = freeze()
    saved = storage.checked(ROOT/'dashboard/ai_models'/SAVED)
    dataset = storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    if (saved['artifact_sha256'] != SAVED_SHA or saved['source_sha256'] != ensemble.source_hashes()
            or saved['input_artifact_sha256'] != dataset['artifact_sha256']
            or dataset['source_sha256'] != storage.source_hashes()):
        raise ValueError('Saved source or dataset provenance changed')
    rows = decision.enrich_rows(dataset['rows'])
    folds = []
    for fold in saved['folds']:
        test = temporal.select_rows(rows, fold['periods']['test'])
        forecasts = {(p['ticker'], p['at']): p for p in fold['predictions']}
        if len(forecasts) != len(test):
            raise ValueError('Saved prediction population differs')
        points = [forecasts[(r['ticker'], r['at'])] for r in test]
        if any(p['actual_net_pct'] != r['net_y'] or p['selected_median'] != (p['median_net_pct'] > .1)
               for r, p in zip(test, points)):
            raise ValueError('Saved outcomes or decisions changed')
        levels = curve(test, [p['selected_median'] for p in points])
        for k, name in ((1., 'paper'), (2., 'stress')):
            replay = next(p['paper'] for p in levels if p['multiplier'] == k)
            control = fold['reports']['median']['policies']['regression'][name]
            for key in ('status','trades','selected_candidates','selected_dates','unknown_trades','net_return_pct'):
                a, b = replay[key], control[key]
                if not (a == b or type(a) is float and type(b) is float and math.isclose(a, b, abs_tol=1e-9)):
                    raise ValueError('Published cost controls do not reproduce')
        folds.append({'id': fold['id'], 'period': fold['periods']['test'], 'levels': levels})
        print(fold['id'], [(p['approx_roundtrip_cost_pct'], p['paper']['net_return_pct']) for p in levels], flush=True)
    if source_hashes() != storage.checked(ROOT/'data/overnight-cost-review-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Source changed during evaluation')
    path = ROOT/'dashboard/ai_models'/('overnight-cost-review-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    storage.immutable(path, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'entry_allowed': False, 'historical': True, 'new_market_data': False, 'rules': RULES,
        'source_sha256': source_hashes(), 'protocol_sha256': protocol,
        'input_artifact_sha256': dataset['artifact_sha256'], 'ensemble_artifact_sha256': SAVED_SHA, 'folds': folds}))
    print('Saved', path.name, flush=True)
    return path


if __name__ == '__main__':
    run()
