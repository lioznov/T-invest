"""Technical v2: deterministic forest inference; all learning/trading choices unchanged.

The v1 partial run halted on bitwise comparison of parallel tree sums. Preserve
that protocol/code; v2 trains identically and pins forest prediction to one job.
"""
import hashlib
import importlib.metadata
import math
from datetime import datetime, timezone
from statistics import mean
from . import volatility_overnight_study as base

ROOT=base.ROOT
VERSION='volatility_overnight_v2'
prior=base.prior
cash_verifier=base.cash_verifier
VARIANTS=base.VARIANTS
POLICIES=base.POLICIES
HEURISTICS=base.HEURISTICS
VOL_FLOOR=base.VOL_FLOOR
RETURN_COLUMNS=base.RETURN_COLUMNS
PEER_FEATURES=base.PEER_FEATURES
FOREST_PARAMS=base.FOREST_PARAMS
RULES={**base.RULES,
    'technical_repair':'v1 partial2024 run halted on bitwise reload comparison; forest still trained n_jobs2, prediction pinned n_jobs1; no threshold/fitparameter/search changes',
    'sequence':'v2 registered after partial2024 forecasts were seen; incomplete v1 run retained; no2025/26 volatility outcomes seen at repair'}
normalised_x=base.normalised_x
enrich=base.enrich
matrix=base.matrix
predict=base.predict
decisions=base.decisions
trend_guard=base.trend_guard
add_best_five=base.add_best_five
checks=base.checks
passes=base.passes


def source_hashes():
    return {**base.source_hashes(), 'volatility_overnight_study_v2.py':hashlib.sha256(
        (ROOT/'dashboard/volatility_overnight_study_v2.py').read_bytes()).hexdigest()}


def freeze():
    original=base.freeze()
    old=prior.storage.checked(ROOT/'data/volatility-overnight-protocol-v1.json')
    spec={**old['spec'],'version':VERSION,'rules':RULES,'base_protocol_sha256':original,
          'forest_prediction_jobs':1,'source_sha256':source_hashes()}
    path=ROOT/'data/volatility-overnight-protocol-v2.json'
    if path.exists():
        if prior.storage.checked(path)['spec']!=spec:
            raise ValueError('Registered technical v2 changed; use another version')
    else:
        prior.storage.immutable(path,prior.storage.signed({'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec}))
    return prior.decision.m.digest(spec)


def fit_variant(spec,train,calibration):
    train,calibration=list(train),list(calibration)
    bundle=base.fit_variant(spec,train,calibration)
    if spec['kind']=='forest':
        for model in bundle['estimators']:
            model.set_params(n_jobs=1)
        cal=[r for r in calibration if r['net_y'] is not None]
        residuals=sorted(abs(r['net_y']-p) for r,p in zip(cal,predict(bundle,cal)))
        bundle['interval_abs_pct']=residuals[min(len(residuals),math.ceil((len(residuals)+1)*.8))-1]
    return bundle


def run():
    import joblib
    from threadpoolctl import threadpool_limits
    for name,version in prior.storage.PACKAGES.items():
        if importlib.metadata.version(name)!=version:
            raise ValueError('Requires '+name+'=='+version)
    protocol=freeze()
    data=prior.storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    control=prior.storage.checked(ROOT/'dashboard/ai_models'/prior.CONTROL)
    if (data['source_sha256']!=prior.storage.source_hashes() or control['artifact_sha256']!=prior.CONTROL_SHA
            or control['source_sha256']!=prior.robustness.ensemble.source_hashes()
            or data['artifact_sha256']!=control['input_artifact_sha256']):
        raise ValueError('Registered prices or source provenance changed')
    rows=enrich(data['rows'])
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder=ROOT/'data'/('volatility-overnight-models-v2-'+stamp); folder.mkdir()
    folds=[]; accounts=0
    with threadpool_limits(limits=2):
        for fold in prior.temporal.FOLDS:
            groups=prior.temporal.group_rows(rows,fold['periods']); test=groups['test']
            old=next(f for f in control['folds'] if f['id']==fold['id'])
            old_levels=prior.verify_control(test,old)
            lookup={(p['ticker'],p['at']):p for p in old['predictions']}
            old_mask=[lookup[(r['ticker'],r['at'])]['selected_median'] for r in test]
            old_levels=[add_best_five(level,test,old_mask) for level in old_levels]
            accounts+=cash_verifier.check_levels(test,old_mask,old_levels)
            variants=[]
            for spec in VARIANTS:
                print(fold['id'],spec['id'],'fit only prior train; score original net returns',flush=True)
                bundle=fit_variant(spec,groups['train'],groups['calibration'])
                features=[{'z':r['z'],'risk_scale':r['risk_scale']} for r in test]
                values=predict(bundle,features)
                report=prior.metrics.evaluate(test,values,None,bundle['train_net_mean'],bundle['train_profit_prior'],
                    bundle['calibration_profit_prior'],train_net_median=bundle['train_net_median'])
                errors=[abs(p-r['net_y']) for r,p in zip(test,values) if r['net_y'] is not None]
                q=bundle['interval_abs_pct']
                report.update(interval_abs_pct=q,interval_coverage_pct=100*mean(e<=q for e in errors) if errors else None)
                policies={}
                for name in POLICIES:
                    mask=decisions(name,test,values); levels=checks(test,mask)
                    accounts+=cash_verifier.check_levels(test,mask,levels)
                    policies[name]={'robustness':levels,'year_screen_passed':passes(levels)}
                    print(' ',name,'returns',[v['baseline']['report']['net_return_pct'] for v in levels],
                        'drop5',[v['drop_best_dates'][-1]['report']['net_return_pct'] if v['drop_best_dates'] else None for v in levels],flush=True)
                path=folder/(fold['id']+'-'+spec['id']+'.joblib'); joblib.dump(bundle,path,compress=3)
                if predict(joblib.load(path),features)!=values:
                    raise ValueError('Locally reloaded weights changed forecasts')
                variants.append({**spec,**{k:bundle[k] for k in ('trained_rows','calibration_rows','target_clip_bounds',
                    'clipped_train_targets','train_net_mean','train_net_median','train_profit_prior','calibration_profit_prior',
                    'warnings','fit_iterations')},'report':report,'policies':policies,
                    'predictions':[{'ticker':r['ticker'],'at':r['at'],'actual_net_pct':r['net_y'],'predicted_net_pct':p,
                        **{'selected_'+name:decisions(name,[r],[p])[0] for name in POLICIES}} for r,p in zip(test,values)],
                    'weights_local_file':path.relative_to(ROOT).as_posix(),'weights_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            heuristics={}
            for name in HEURISTICS:
                mask=decisions(name,test); levels=checks(test,mask)
                accounts+=cash_verifier.check_levels(test,mask,levels)
                heuristics[name]={'robustness':levels,'year_screen_passed':passes(levels),
                    'decisions':[{'ticker':r['ticker'],'at':r['at'],'selected':s,'actual_net_pct':r['net_y']} for r,s in zip(test,mask)]}
                print(fold['id'],name,'returns',[v['baseline']['report']['net_return_pct'] for v in levels],flush=True)
            folds.append({**fold,'variants':variants,'heuristics':heuristics,'control_robustness':old_levels})
    summary=[]
    names=[(spec['id'],policy) for spec in VARIANTS for policy in POLICIES]+[(name,None) for name in HEURISTICS]+[('old_median_control',None)]
    for name,policy in names:
        states={}
        for fold in folds:
            if policy:
                levels=next(v for v in fold['variants'] if v['id']==name)['policies'][policy]['robustness']
            elif name=='old_median_control':
                levels=fold['control_robustness']
            else:
                levels=fold['heuristics'][name]['robustness']
            states[fold['id']]=passes(levels)
        summary.append({'id':name+('/'+policy if policy else ''),'year_screen_passes':states,
            'screen_2025_2026_passed':all(states[y] for y in ('test_2025','test_2026')),
            'all_three_years_passed':all(states.values()),'promoted_to_live':False})
    if source_hashes()!=prior.storage.checked(ROOT/'data/volatility-overnight-protocol-v2.json')['spec']['source_sha256']:
        raise ValueError('Registered code changed during evaluation')
    path=ROOT/'dashboard/ai_models'/('volatility-overnight-v2-'+stamp+'.json')
    prior.storage.immutable(path,prior.storage.signed({'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
        'rules':RULES,'entry_allowed':False,'historical':True,'prospective':False,'new_market_data':False,
        'source_sha256':source_hashes(),'protocol_sha256':protocol,'packages':prior.storage.PACKAGES,
        'input_artifact_sha256':data['artifact_sha256'],'control_artifact_sha256':control['artifact_sha256'],
        'folds':folds,'summary':summary,'independent_cash_replays':accounts}))
    print('Saved',path.name,'independent cash replays',accounts,flush=True)
    print(summary,flush=True)
    return path


if __name__=='__main__':
    run()
