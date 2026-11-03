"""Fixed hindsight concentration diagnostics on the already examined real-price archive.

No fitting, fetching, threshold selection, live signals or order submission.
Register every diagnostic before evaluating saved outcomes; never a new holdout.
"""
import hashlib
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_decision_metrics as metrics
from . import overnight_ensemble_study as ensemble
from . import temporal_validation as temporal


ROOT = storage.ROOT
VERSION = 'overnight_robustness_review_v1'
SAVED = 'overnight-ensemble-v1-20261026-072149-981644.json'
SAVED_SHA = '1fe620843c5031586c2c28a90536035bad900db224c92e817f9d12a2afa650c4'
RULES = {
    'policy': 'fixed median of saved three net forecasts >.10pct, same selected population, no fitting or tuning',
    'cash': '100000RUB initial cash, wholelots, max25pct perstock, no shorts/leverage',
    'costs': 'fixed multipliers1and2 of commission.05pct andslippage.05pct eachperside',
    'controls': 'baseline status/counts/return/drawdown must reproduce published median normal/stress accounts',
    'trace': 'separate daily cash/quantity/PnL reconstruction must reproduce simulator baseline',
    'concentration': 'remove best1and3 positive-PnL origin dates IN HINDSIGHT, all stocks together, rerun remaining account',
    'quarters': 'four separate100000RUB quarterly accounts by Moscow origin quarter; plus omit each quarter in whole-year replay',
    'missing': 'selectedunknown poisons account; incomplete baseline gets NO concentration or quarter summaries',
    'limits': 'same alreadyexamined2024/25/partial2026 prices; no independent validation, dividends, taxes or true execution',
    'interpretation': 'hindsight sensitivity only; removed dates/quarters are NEVER an implementable new entry rule; no confidence/profit guarantee',
    'entry_allowed': False,
}


def source_hashes():
    return {**ensemble.source_hashes(), 'overnight_robustness_review.py': hashlib.sha256(
        (ROOT/'dashboard/overnight_robustness_review.py').read_bytes()).hexdigest()}


def freeze():
    spec = {'version': VERSION, 'rules': RULES, 'source_sha256': source_hashes(),
            'saved_artifact_sha256': SAVED_SHA}
    path = ROOT/'data/overnight-robustness-protocol-v1.json'
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Registered robustness diagnostics changed; use a new version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': spec}))
    return decision.m.digest(spec)


def day_of(row):
    return datetime.fromisoformat(row['at']).astimezone(decision.m.MSK).date()


def _aligned(rows, selected):
    rows, selected = list(rows), list(selected)
    if len(rows) != len(selected) or any(type(v) is not bool for v in selected):
        raise ValueError('Exactly one fixed boolean decision per row is required')
    return rows, selected


def replay(rows, selected, multiplier):
    """Reconstruct completed accounts and verify against the frozen simulator.

    Unknown/open outcomes suppress the entire trace, including known subsets.
    Daily drawdown observes morning cash only, never overnight price extremes.
    """
    rows, selected = _aligned(rows, selected)
    report = metrics.paper_predictions(rows, [float(v) for v in selected],
                                       multiplier=multiplier, threshold=.5)
    if report['status'] != 'ok' or not report['trades']:
        return {'report': report, 'daily': [], 'ending_cash': None}
    grouped = defaultdict(list)
    for row, choose in zip(rows, selected):
        if choose:
            grouped[day_of(row)].append(row)
    cash = peak = 100000.
    drawdown, trades, daily = 0., 0, []
    fee = slip = .0005 * multiplier
    for day, chosen in sorted(grouped.items()):
        opening = cash
        budget = min(cash/4, cash/len(chosen))
        day_pnl, positions = 0., []
        for row in chosen:
            purchase = row['entry_price'] * (1+slip) * (1+fee)
            lots = math.floor(budget/(purchase*row['lot']))
            if not lots:
                continue
            quantity = lots*row['lot']
            pnl = quantity*(row['actual_price']*(1-slip)*(1-fee)-purchase)
            day_pnl += pnl
            positions.append({'ticker': row['ticker'], 'quantity': quantity, 'pnl_rub': pnl})
            trades += 1
        cash += day_pnl
        peak = max(peak, cash)
        drawdown = max(drawdown, 100*(peak-cash)/peak)
        daily.append({'date': day.isoformat(), 'opening_cash': opening, 'ending_cash': cash,
                      'pnl_rub': day_pnl, 'positions': positions})
    if (trades != report['trades']
            or not math.isclose(100*(cash/100000-1), report['net_return_pct'], abs_tol=1e-9)
            or not math.isclose(drawdown, report['max_drawdown_pct'], abs_tol=1e-9)):
        raise ValueError('Independent daily cash trace does not reproduce simulator')
    return {'report': report, 'daily': daily, 'ending_cash': cash}


def review(rows, selected, multiplier=1):
    rows, selected = _aligned(rows, selected)
    baseline = replay(rows, selected, multiplier)
    result = {'cost_multiplier': multiplier, 'baseline': baseline,
              'status': baseline['report']['status'], 'concentration': None,
              'drop_best_dates': [], 'quarters': [], 'omit_quarters': []}
    if baseline['report']['status'] != 'ok' or not baseline['report']['trades']:
        return result
    positive = sorted((d for d in baseline['daily'] if d['pnl_rub'] > 0),
                      key=lambda d: (-d['pnl_rub'], d['date']))
    positive_pnl = math.fsum(d['pnl_rub'] for d in positive)
    net_pnl = baseline['ending_cash']-100000.
    top3 = math.fsum(d['pnl_rub'] for d in positive[:3])
    result['concentration'] = {
        'profitable_dates': len(positive),
        'losing_dates': sum(d['pnl_rub'] < 0 for d in baseline['daily']),
        'positive_pnl_rub': positive_pnl, 'net_pnl_rub': net_pnl,
        'top3_positive_pnl_rub': top3,
        'top3_share_of_positive_pnl_pct': 100*top3/positive_pnl if positive_pnl else None,
        'top3_share_of_net_pnl_pct': 100*top3/net_pnl if net_pnl > 0 else None,
    }
    for count in (1, 3):
        omitted = {d['date'] for d in positive[:count]}
        mask = [choose and day_of(row).isoformat() not in omitted for row, choose in zip(rows, selected)]
        result['drop_best_dates'].append({'requested_best_dates': count,
            'omitted_dates': sorted(omitted),
            'hindsight_only': True, 'report': replay(rows, mask, multiplier)['report']})
    for quarter in range(1, 5):
        in_quarter = [(day_of(row).month-1)//3+1 == quarter for row in rows]
        q_rows = [r for r, take in zip(rows, in_quarter) if take]
        q_mask = [s for s, take in zip(selected, in_quarter) if take]
        result['quarters'].append({'quarter': quarter, 'independent_start_cash': 100000.,
            'eligible_rows': len(q_rows), 'report': replay(q_rows, q_mask, multiplier)['report']})
        remaining = [choose and not take for choose, take in zip(selected, in_quarter)]
        result['omit_quarters'].append({'omitted_quarter': quarter, 'hindsight_only': True,
            'report': replay(rows, remaining, multiplier)['report']})
    return result


def run():
    protocol = freeze()
    saved = storage.checked(ROOT/'dashboard/ai_models'/SAVED)
    data = storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    if (saved['artifact_sha256'] != SAVED_SHA or saved['source_sha256'] != ensemble.source_hashes()
            or saved['input_artifact_sha256'] != data['artifact_sha256']
            or data['source_sha256'] != storage.source_hashes()):
        raise ValueError('Saved inputs or source provenance changed')
    rows = decision.enrich_rows(data['rows'])
    folds = []
    for fold in saved['folds']:
        test = temporal.select_rows(rows, fold['periods']['test'])
        lookup = {(p['ticker'], p['at']): p for p in fold['predictions']}
        if len(lookup) != len(fold['predictions']) or set(lookup) != {(r['ticker'], r['at']) for r in test}:
            raise ValueError('Saved forecast population changed')
        points = [lookup[(r['ticker'], r['at'])] for r in test]
        if any(p['actual_net_pct'] != r['net_y'] or type(p['selected_median']) is not bool
               or p['selected_median'] != (p['median_net_pct'] > .1) for p, r in zip(points, test)):
            raise ValueError('Saved outcomes/decisions differ')
        mask = [p['selected_median'] for p in points]
        levels = []
        for multiplier, name in ((1, 'paper'), (2, 'stress')):
            item = review(test, mask, multiplier)
            original = fold['reports']['median']['policies']['regression'][name]
            actual = item['baseline']['report']
            for key in ('status','trades','selected_candidates','selected_dates','unknown_trades',
                        'net_return_pct','max_drawdown_pct'):
                a, b = actual[key], original[key]
                if not (a == b or isinstance(a, float) and isinstance(b, float)
                        and math.isclose(a, b, abs_tol=1e-9)):
                    raise ValueError('Published baseline did not reproduce: '+key)
            levels.append(item)
        folds.append({'id': fold['id'], 'period': fold['periods']['test'], 'levels': levels})
        print(fold['id'], 'completed diagnostics' if levels[0]['concentration'] else 'incomplete; no partial-profit summaries', flush=True)
    if source_hashes() != storage.checked(ROOT/'data/overnight-robustness-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Registered source changed during review')
    path = ROOT/'dashboard/ai_models'/('overnight-robustness-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    storage.immutable(path, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'rules': RULES, 'historical': True, 'prospective': False, 'entry_allowed': False, 'new_market_data': False,
        'source_sha256': source_hashes(), 'protocol_sha256': protocol,
        'input_artifact_sha256': data['artifact_sha256'], 'ensemble_artifact_sha256': SAVED_SHA, 'folds': folds}))
    print('Saved', path.name, flush=True)
    return path


if __name__ == '__main__':
    run()
