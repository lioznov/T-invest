"""Three fixed combinations after reviewing the four robust-ML alternatives.

Sequential exploration of previously viewed history, never an untouched test.
No new fits, network requests, threshold searches or model promotion.
"""
import hashlib
import math
from datetime import datetime, timezone

from . import robust_overnight_models as study
from . import verify_robust_overnight_models as independent

ROOT = study.ROOT
VERSION = 'robust_overnight_ensemble_v1'
PRIOR = 'robust-overnight-models-v1-20261103-165310-192860.json'
PRIOR_SHA = '9d4d6b6e0a446c9f2f78d0a317e96f2fe9cad87290499b551fbc9245b0254a37'
MEMBERS = ('mlp_clipped','huber_linear','boost_net_median')
POLICIES = ('mean','median','unanimous')
RULES = {
    'members': 'three different model families:clippedMLP/Huber/medianHGB; rawMLP kept as earlier control, not a fourth family',
    'policies': 'equalmean>.10pct; median>.10pct; allthree>.10pct (unanimity filter, not separate point forecast)',
    'fixed': 'three rules only, no learned weights, selection, threshold tuning or refits',
    'alignment': 'ticker+UTCinstant, identical eligible rows and actualnetlabels; allunknown targets preserved',
    'diagnostics': 'same frozen two-costlevels, best1/3dates andquarters; independent cash replay of every reported scenario',
    'screen': 'same >=20dates/year, positive baseline/dropbest1/dropbest3 normalANDstress in2025AND2026; separate all3yearsflag',
    'sequence': 'registered AFTER four base-model results were seen; alltestyears previously viewed; exploratory, no independent evidence',
    'limits': 'same cash/wholelot/currentlots/survivors; no dividends/taxes/realexecution/worstovernightdrawdown',
    'reserve': 'TATN/CHMF/PHOR unopened', 'entry_allowed': False,
}


def source_hashes():
    return {**study.source_hashes(), **{name:hashlib.sha256((ROOT/'dashboard'/name).read_bytes()).hexdigest()
        for name in ('robust_overnight_ensemble.py','verify_robust_overnight_models.py')}}


def freeze():
    spec=study.decision.jsonable({'version':VERSION,'members':MEMBERS,'policies':POLICIES,
        'rules':RULES,'prior_artifact_sha256':PRIOR_SHA,'source_sha256':source_hashes()})
    path=ROOT/'data/robust-overnight-ensemble-protocol-v1.json'
    if path.exists():
        if study.storage.checked(path)['spec'] != spec:
            raise ValueError('Registered combinations changed; create another version')
    else:
        study.storage.immutable(path,study.storage.signed({'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec}))
    return study.decision.m.digest(spec)


def key(row):
    return (row['ticker'],study.temporal._time(row['at']).astimezone(timezone.utc))


def combine(rows, variants):
    rows=list(rows)
    if len(variants) != 3 or {v['id'] for v in variants} != set(MEMBERS):
        raise ValueError('Exactly three registered model families required')
    study.metrics.validate_rows(rows)
    expected={key(r) for r in rows}
    aligned=[]
    for name in MEMBERS:
        variant=next(v for v in variants if v['id'] == name)
        lookup={key(p):p for p in variant['predictions']}
        if len(lookup) != len(variant['predictions']) or set(lookup) != expected:
            raise ValueError('Missing, duplicate or unexpected model predictions')
        aligned.append(lookup)
    points=[]
    for row in rows:
        members=[lookup[key(row)] for lookup in aligned]
        if any(p['actual_net_pct'] != row['net_y'] for p in members):
            raise ValueError('Member outcome labels differ')
        values=[p['predicted_net_pct'] for p in members]
        if not all(type(v) in (int,float) and math.isfinite(v) for v in values):
            raise ValueError('Nonfinite model forecast')
        center=sorted(values)[1]
        average=math.fsum(values)/3
        points.append({'ticker':row['ticker'],'at':row['at'],'actual_net_pct':row['net_y'],
            'member_predictions':values,'mean_net_pct':average,'median_net_pct':center,
            'selected_mean':average > .1,'selected_median':center > .1,'selected_unanimous':min(values) > .1})
    return points


def run():
    protocol=freeze()
    prior=study.storage.checked(ROOT/'dashboard/ai_models'/PRIOR)
    data=study.storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    if (prior['artifact_sha256'] != PRIOR_SHA or prior['source_sha256'] != study.source_hashes()
            or data['source_sha256'] != study.storage.source_hashes()
            or data['artifact_sha256'] != prior['input_artifact_sha256']):
        raise ValueError('Registered source/data mismatch')
    rows=study.decision.enrich_rows(data['rows'])
    folds=[]; cash_replays=0
    for fold in prior['folds']:
        test=study.temporal.select_rows(rows,fold['periods']['test'])
        variants=[v for v in fold['variants'] if v['id'] in MEMBERS]
        points=combine(test,variants)
        policies={}
        for name in POLICIES:
            mask=[p['selected_'+name] for p in points]
            checks=[study.robustness.review(test,mask,k) for k in (1,2)]
            cash_replays+=independent.check_levels(test,mask,checks)
            policies[name]={'robustness':checks,'year_screen_passed':study.passes(checks)}
            print(fold['id'],name,'returns',[v['baseline']['report']['net_return_pct'] for v in checks],
                  'drop3',[v['drop_best_dates'][-1]['report']['net_return_pct'] if v['drop_best_dates'] else None for v in checks],flush=True)
        folds.append({'id':fold['id'],'periods':fold['periods'],'predictions':points,'policies':policies})
    summary=[]
    for name in POLICIES:
        states={f['id']:f['policies'][name]['year_screen_passed'] for f in folds}
        summary.append({'id':name,'year_screen_passes':states,
            'screen_2025_2026_passed':all(states[y] for y in ('test_2025','test_2026')),
            'all_three_years_passed':all(states.values()),'promoted_to_live':False})
    if source_hashes() != study.storage.checked(ROOT/'data/robust-overnight-ensemble-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Source changed during evaluation')
    path=ROOT/'dashboard/ai_models'/('robust-overnight-ensemble-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    study.storage.immutable(path,study.storage.signed({'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
        'rules':RULES,'entry_allowed':False,'prospective':False,'new_market_data':False,'historical':True,
        'source_sha256':source_hashes(),'protocol_sha256':protocol,'prior_artifact_sha256':PRIOR_SHA,
        'input_artifact_sha256':data['artifact_sha256'],'folds':folds,'summary':summary,
        'independent_cash_replays':cash_replays}))
    print('Saved',path.name,'independent cash checks',cash_replays,flush=True)
    print(summary,flush=True)
    return path


if __name__ == '__main__':
    run()
