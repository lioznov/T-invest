"""Fixed volatility-normalised ML and causal rules on the existing price cache.

Offline research only. All variants and diagnostics registered before fitting.
Never overwrites prior artifacts or changes live signals, orders or Telegram.
"""
import hashlib
import importlib.metadata
import math
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median, pstdev

from . import robust_overnight_models as prior
from . import verify_robust_overnight_models as cash_verifier

ROOT = prior.ROOT
VERSION = 'volatility_overnight_v1'
VOL_FLOOR = .25
RETURN_COLUMNS = {0:1., 1:math.sqrt(5), 2:math.sqrt(20), 3:1., 4:1., 5:1., 9:1., 10:1.}
PEER_FEATURES = ('peer_normalised_return_mean','peer_normalised_dispersion',
                 'peer_positive_fraction','relative_normalised_return','peer_coverage')
VARIANTS = (
    {'id':'vol_ridge','kind':'ridge'},
    {'id':'vol_mlp','kind':'mlp'},
    {'id':'vol_forest','kind':'forest'},
    {'id':'vol_median_boost','kind':'hist'},
)
POLICIES = ('plain','trend_guard')
HEURISTICS = ('pullback_in_trend','strong_day')
FOREST_PARAMS = {'n_estimators':300,'max_depth':5,'min_samples_leaf':80,
    'max_features':1.,'bootstrap':True,'random_state':20261103,'n_jobs':2}
RULES = {
    'task':'unchanged17:45 features -> exact18open->nextcalendar10open; same7stock cachedrealEXCHANGE prices',
    'normalisation':'s=max(past20day_volatility_pct,.25); eight return columns/s with sqrt5/20 horizon factors; originalvol/position/logvolume/onehot retained',
    'peers':'5 leave-self-out sameUTCinstant X-only features on normalised past1day returns; use every eligible peer even if future outcome unknown; no backfill',
    'training':'train target net_y/s; weights equal perknowntrain date; train-only weighted StandardScaler, transformedfeatures clipped+-8',
    'fits':'18 fixed fits:3Ridge+9MLP+3RandomForest+3medianHGB; same3 chronological splits, no search/refits/seedselection',
    'neural':'same16/8tanh/200iterations/3seeds as prior; train-only2.5/97.5pct clipping of NORMALISED target; medianmembers*s at inference',
    'calibration':'next chronological year80pct absolute-original-net residual interval only; no bias/threshold/weights adjustment',
    'policies':'plain predictednet>.10pct; trend_guard additionally raw20dayreturn>0 AND distanceMA20>0 AND peerpositivefraction>=.5',
    'pullback':'raw20dayreturn>0 AND distanceMA20>0 AND normalised1dayreturn<=-1 AND logrelativevolume>=0',
    'strong_day':'normaliseddaybody>=1 AND closeposition>=.75 AND logrelativevolume>=0 AND raw20dayreturn>0 AND peerpositivefraction>=.5',
    'scope':'8MLpolicies+2fixedrules, all reported; rules receive no invented point forecasts/profitprobabilities',
    'economics':'same100000RUB wholelots/max25pct/stock LONGonly; fee.05pct+slip.05pct eachperside; stressx2, unchanged decisions',
    'stress':'omit best1/3/5positivePnL origin dates IN HINDSIGHT with all stocks together and complete account rerun; fixedquarterdiagnostics retained',
    'screen':'2025AND2026 each>=20dates, baseline and all1/3/5omissions positive and complete at BOTH costs; separate all3years flag',
    'unknowns':'retain all eligible unknowns; selectedmissingprice poisons whole account; no known-subset-profit summaries',
    'control':'previous median3net model control must reproduce both published ordinary/stress accounts before adding best5 sensitivity',
    'reuse':'alltestyears previously viewed; exploratory reuse and manyprior trials; no independent proof/multiple-search correction/promotion',
    'limits':'currentUIDs/lots/survivors; no dividendcash/tax/truefills/worstnightdrawdown/historicalbook/news/traderidentity',
    'reserve':'TATN/CHMF/PHOR unopened','entry_allowed':False,
}


def source_hashes():
    names=('volatility_overnight_study.py','verify_robust_overnight_models.py')
    return {**prior.source_hashes(), **{name:hashlib.sha256((ROOT/'dashboard'/name).read_bytes()).hexdigest() for name in names}}


def freeze():
    spec=prior.decision.jsonable({'version':VERSION,'rules':RULES,'variants':VARIANTS,'policies':POLICIES,
        'heuristics':HEURISTICS,'vol_floor':VOL_FLOOR,'return_columns':RETURN_COLUMNS,'peer_features':PEER_FEATURES,
        'folds':prior.temporal.FOLDS,'packages':prior.storage.PACKAGES,'mlp':prior.MLP_PARAMS,'seeds':prior.SEEDS,
        'ridge':{'alpha':10.,'solver':'svd'},'forest':FOREST_PARAMS,'hist':prior.HIST_PARAMS,
        'source_sha256':source_hashes(),'control_artifact_sha256':prior.CONTROL_SHA})
    path=ROOT/'data/volatility-overnight-protocol-v1.json'
    if path.exists():
        if prior.storage.checked(path)['spec'] != spec:
            raise ValueError('Registered volatility experiment changed; use another version')
    else:
        prior.storage.immutable(path,prior.storage.signed({'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec}))
    return prior.decision.m.digest(spec)


def normalised_x(row):
    x=list(prior.decision._features(row,'x',len(prior.decision.m.FEATURES)))
    if x[8] < 0:
        raise ValueError('Past volatility must be nonnegative')
    scale=max(VOL_FLOOR,x[8])
    z=list(x)
    for index,factor in RETURN_COLUMNS.items():
        z[index]/=scale*factor
    return z,scale


def enrich(rows):
    """Peer information depends on simultaneous historical X, never labels/fills."""
    rows=prior.decision.enrich_rows(rows)
    groups=defaultdict(list)
    for row in rows:
        z,scale=normalised_x(row)
        groups[prior.temporal._time(row['at']).astimezone(timezone.utc)].append((row,z,scale))
    result=[]
    for group in groups.values():
        for row,z,scale in group:
            values=[other[1][0] for other in group if other[0]['ticker'] != row['ticker']]
            extra=[mean(values),pstdev(values),mean(v>0 for v in values),z[0]-mean(values),len(values)/6] if values else [0.]*5
            result.append({**row,'z':z+extra,'risk_scale':scale})
    return sorted(result,key=lambda r:(prior.temporal._time(r['at']),r['ticker']))


def matrix(rows):
    import numpy as np
    xs=[]
    for row in rows:
        z=row['z']; scale=row['risk_scale']
        if (len(z) != 23 or not all(type(v) in (int,float) and math.isfinite(v) for v in z)
                or type(scale) not in (int,float) or not math.isfinite(scale) or scale < VOL_FLOOR):
            raise ValueError('Invalid finite normalised features or risk scale')
        xs.append(z)
    return np.asarray(xs,dtype=float).reshape((-1,23))


def predict(bundle, rows):
    import numpy as np
    rows=list(rows)
    if not rows:
        return []
    transformed=np.clip(bundle['scaler'].transform(matrix(rows)),-8.,8.)
    values=np.median(np.asarray([model.predict(transformed) for model in bundle['estimators']]),axis=0)
    result=(values*np.asarray([r['risk_scale'] for r in rows])).tolist()
    if not all(math.isfinite(v) for v in result):
        raise ValueError('Nonfinite original-net predictions')
    return result


def fit_variant(spec,train,calibration):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    if spec not in VARIANTS:
        raise ValueError('Unregistered volatility algorithm')
    train,calibration=list(train),list(calibration)
    prior.metrics.validate_rows(train); prior.metrics.validate_rows(calibration)
    matrix(train); matrix(calibration)
    known=[r for r in train if r['net_y'] is not None]
    cal=[r for r in calibration if r['net_y'] is not None]
    if (len(known)<200 or len(cal)<50 or len({prior.robustness.day_of(r) for r in known})<30
            or len({prior.robustness.day_of(r) for r in cal})<10):
        raise ValueError('Insufficient train/calibration history')
    if max(prior.temporal._time(r['label_end']) for r in train) >= min(prior.temporal._time(r['at']) for r in calibration):
        raise ValueError('Training labels overlap calibration')
    weights=prior.date_weights(known)
    x=matrix(known); original=np.asarray([r['net_y'] for r in known])
    target=original/np.asarray([r['risk_scale'] for r in known])
    scaler=StandardScaler().fit(x,sample_weight=weights)
    transformed=np.clip(scaler.transform(x),-8.,8.)
    bounds=np.quantile(target,[.025,.975]).tolist() if spec['kind']=='mlp' else None
    y=np.clip(target,*bounds) if bounds is not None else target.copy()
    if spec['kind']=='ridge':
        models=[Ridge(alpha=10.,solver='svd')]
    elif spec['kind']=='mlp':
        models=[MLPRegressor(**prior.MLP_PARAMS,random_state=seed) for seed in prior.SEEDS]
    elif spec['kind']=='forest':
        models=[RandomForestRegressor(**FOREST_PARAMS)]
    else:
        models=[HistGradientBoostingRegressor(**prior.HIST_PARAMS)]
    messages=[]
    for index,model in enumerate(models):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            model.fit(transformed,y,sample_weight=weights)
        messages.extend({'member':index,'category':type(w.message).__name__,'message':str(w.message)} for w in caught)
    bundle={'spec':spec,'scaler':scaler,'estimators':models,'target_clip_bounds':bounds,
        'clipped_train_targets':int(np.count_nonzero(y!=target)),'trained_rows':len(known),'calibration_rows':len(cal),
        'train_net_mean':float(original.mean()),'train_net_median':median(original.tolist()),
        'train_profit_prior':mean(r['profit_y'] for r in known),'calibration_profit_prior':mean(r['profit_y'] for r in cal),
        'warnings':messages,'fit_iterations':[int(getattr(m,'n_iter_',0) or 0) for m in models]}
    residuals=sorted(abs(r['net_y']-p) for r,p in zip(cal,predict(bundle,cal)))
    bundle['interval_abs_pct']=residuals[min(len(residuals),math.ceil((len(residuals)+1)*.8))-1]
    predict(bundle,known)
    return bundle


def trend_guard(row):
    return row['x'][2]>0 and row['x'][9]>0 and row['z'][-3]>=.5


def decisions(name,rows,predictions=None):
    rows=list(rows)
    matrix(rows)
    if name in POLICIES:
        predictions=list(predictions) if predictions is not None else []
        if len(predictions)!=len(rows) or not all(type(p) in (int,float) and math.isfinite(p) for p in predictions):
            raise ValueError('Exactly one finite prediction per eligible row required')
        return [p>.1 and (name=='plain' or trend_guard(r)) for r,p in zip(rows,predictions)]
    if name=='pullback_in_trend':
        return [r['x'][2]>0 and r['x'][9]>0 and r['z'][0]<=-1 and r['x'][7]>=0 for r in rows]
    if name=='strong_day':
        return [r['z'][3]>=1 and r['x'][6]>=.75 and r['x'][7]>=0 and r['x'][2]>0 and r['z'][-3]>=.5 for r in rows]
    raise ValueError('Unregistered decision rule')


def add_best_five(level,rows,mask):
    if level['baseline']['report']['status']!='ok' or not level['baseline']['report']['trades']:
        return level
    positive=sorted((d for d in level['baseline']['daily'] if d['pnl_rub']>0),key=lambda d:(-d['pnl_rub'],d['date']))
    omitted={d['date'] for d in positive[:5]}
    selected=[choose and prior.robustness.day_of(r).isoformat() not in omitted for r,choose in zip(rows,mask)]
    level['drop_best_dates'].append({'requested_best_dates':5,'omitted_dates':sorted(omitted),'hindsight_only':True,
        'report':prior.robustness.replay(rows,selected,level['cost_multiplier'])['report']})
    return level


def checks(rows,mask):
    return [add_best_five(prior.robustness.review(rows,mask,k),rows,mask) for k in (1,2)]


def passes(levels):
    if len(levels)!=2 or [v['cost_multiplier'] for v in levels]!=[1,2]:
        return False
    for level in levels:
        base=level['baseline']['report']
        drops=level['drop_best_dates']
        reports=[base]+[d['report'] for d in drops]
        if (base['selected_dates']<20 or [d['requested_best_dates'] for d in drops]!=[1,3,5]
                or any(r['status']!='ok' or type(r['net_return_pct']) not in (int,float)
                       or not math.isfinite(r['net_return_pct']) or r['net_return_pct']<=0 for r in reports)):
            return False
    return True


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
    folder=ROOT/'data'/('volatility-overnight-models-'+stamp); folder.mkdir()
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
    if source_hashes()!=prior.storage.checked(ROOT/'data/volatility-overnight-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Registered code changed during evaluation')
    path=ROOT/'dashboard/ai_models'/('volatility-overnight-v1-'+stamp+'.json')
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
