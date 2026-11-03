"""Fixed basket-regime mixtures and relative-stock selection; offline research only.

No fetching, parameter search, orders, Telegram or live-model promotion.
Register all choices before reading the old verified real-price example cache.
"""
import hashlib
import importlib.metadata
import math
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median, pstdev

from . import volatility_overnight_study_v2 as vol
from . import verify_volatility_overnight as independent

ROOT=vol.ROOT
VERSION='regime_relative_v1'
MARKET_NAMES=('median_normalised_1d','median_normalised_5d','median_normalised_20d',
              'median_normalised_day_body','dispersion_normalised_1d','positive_1d_fraction')
VARIANTS=(
    {'id':'basket_ridge','kind':'ridge','experts':1},
    {'id':'regime_ridge','kind':'ridge','experts':2},
    {'id':'regime_boost','kind':'hist','experts':2},
    {'id':'basket_mlp','kind':'mlp','experts':1},
)
POLICIES=('all','top2')
HEURISTICS=('relative_momentum','relative_pullback')
GATE_PARAMS={'n_components':2,'covariance_type':'diag','tol':1e-3,'reg_covar':1e-3,
             'max_iter':200,'n_init':1,'init_params':'kmeans','random_state':20261103,'warm_start':False}
BOOST_PARAMS={**vol.prior.HIST_PARAMS,'loss':'squared_error'}
RULES={
    'data':'same7stocks cachedEXCHANGE prices; unchanged17:45X/18open->exactnextcalendar10open netreturn; LONGonly',
    'features':'previous23causalvolatility/peerfeatures +6sameinstant basketstatistics; 29total; basket!=fullmarketindex',
    'market':'median normalised1/5/20day returns/daybody, populationstdevnormalised1dayreturns, positive1dayfraction; all eligible peers including futureunknowns',
    'gate':'one2component diagonalGMM pertrain, trainedonONE X-only aggregate pereligibleUTCinstant, including unknownpast outcomes; trainonlyStandardScaler; no calibration/testfit',
    'experts':'regimeRidge andregimeHGB each2models; knowntrain weights=dateweight*trainGMMresponsibility; allfinitepositiveweightmodels retained; soft mixture inference, never futureprofitregime labels',
    'fits':'27mainfits:3GMMgates+3pooledRidge+6expertRidge+6expertHGB+9MLP; no search/refits/seedselection',
    'neural':'29->16->8->1,median3seeds1729/1730/1731; same200iterations; train-onlynormalisedtarget2.5/97.5pctclip',
    'preprocessing':'train-onlyweightedStandardScaler29,clip+-8; target=originalnet/knownrisk_scale; inference restores originalunits',
    'calibration':'nextyear80pct absoluteoriginalnetresidual interval only; no bias/threshold/modelweight tuning',
    'modelpolicies':'all predictednet>.10pct; top2 same candidates ranked by predictednet descending/tickerascending perUTCinstant; no futureprice/label availability filtering',
    'momentum':'min4eligiblecurrentstocks; positivebasketmedian20d andbasketbreadth>=.5; top2positive normalised5d withown20d>0',
    'pullback':'min4eligiblecurrentstocks; samebasketguard; bottom2normalised1d<=-1 withown20d>0 AND distanceMA20>0',
    'scope':'8modelpolicies+2fixedrelative rules, all reported; no numeric price forecasts for heuristicrules',
    'economics':'100000RUBcash/max25pctperstock/wholelots; commission.05pct+slippage.05pct eachperside; stressx2same decisions',
    'diagnostics':'frozenbest1/3/5positiveorigin-date omissions withwholeaccountreplay, allquarters/omitquarters; hindsight-only tests, not tradable selection',
    'screen':'same2025AND2026 >=20dates each, positivecompletebaseline andall1/3/5omissions BOTHcosts; additional all3years flag',
    'unknowns':'everyeligible row preserved; selectedmissingentry/exit poisons whole account; no known-subset profits',
    'control':'oldmedian3net control normal/stress results must reproduce, thenbest5diagnostic added',
    'reuse':'alltestyears previously viewed repeatedly; no untouchedholdout/multiple-search correction/profitprobability/promotion',
    'limits':'currentUID/lots/survivors; no dividends/tax/fills/worstnightdrawdown/historicalbook/news/traderidentity',
    'reserve':'TATN/CHMF/PHOR unopened','entry_allowed':False,
}


def source_hashes():
    return {**vol.source_hashes(), **{name:hashlib.sha256((ROOT/'dashboard'/name).read_bytes()).hexdigest()
        for name in ('regime_relative_study.py','verify_volatility_overnight.py')}}


def freeze():
    spec=vol.prior.decision.jsonable({'version':VERSION,'rules':RULES,'variants':VARIANTS,'policies':POLICIES,
        'heuristics':HEURISTICS,'gate':GATE_PARAMS,'market_features':MARKET_NAMES,'ridge':{'alpha':10.,'solver':'svd'},
        'boost':BOOST_PARAMS,'mlp':vol.prior.MLP_PARAMS,'seeds':vol.prior.SEEDS,'folds':vol.prior.temporal.FOLDS,
        'packages':vol.prior.storage.PACKAGES,'source_sha256':source_hashes(),'control_sha256':vol.prior.CONTROL_SHA})
    path=ROOT/'data/regime-relative-protocol-v1.json'
    if path.exists():
        if vol.prior.storage.checked(path)['spec']!=spec:raise ValueError('Registered regime study changed; use a new version')
    else:
        vol.prior.storage.immutable(path,vol.prior.storage.signed({'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec}))
    return vol.prior.decision.m.digest(spec)


def instant(row):
    return vol.prior.temporal._time(row['at']).astimezone(timezone.utc)


def enrich(raw):
    groups=defaultdict(list)
    for row in vol.enrich(raw):groups[instant(row)].append(row)
    rows=[]
    for group in groups.values():
        first=[r['z'][0] for r in group]
        market=[median(r['z'][i] for r in group) for i in (0,1,2,3)]+[pstdev(first),mean(v>0 for v in first)]
        for row in group:rows.append({**row,'market':list(market),'u':row['z']+market})
    return sorted(rows,key=lambda r:(instant(r),r['ticker']))


def matrix(rows):
    import numpy as np
    values=[]
    for r in rows:
        if (len(r['u'])!=29 or len(r['market'])!=6
                or not all(type(v) in (int,float) and math.isfinite(v) for v in r['u']+r['market'])
                or r['u'][-6:]!=r['market'] or type(r['risk_scale']) not in (int,float)
                or not math.isfinite(r['risk_scale']) or r['risk_scale']<.25):raise ValueError('Invalid causal basket features')
        values.append(r['u'])
    return np.asarray(values,dtype=float).reshape((-1,29))


def fit_gate(train):
    import numpy as np
    from sklearn.mixture import GaussianMixture
    from sklearn.preprocessing import StandardScaler
    train=list(train);matrix(train)
    by_at={}
    for row in train:
        key=instant(row)
        if key in by_at and by_at[key]!=row['market']:raise ValueError('Basket features differ at same instant')
        by_at[key]=row['market']
    if len(by_at)<30:raise ValueError('Insufficient train basket dates')
    values=np.asarray([by_at[k] for k in sorted(by_at)])
    scaler=StandardScaler().fit(values)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        model=GaussianMixture(**GATE_PARAMS).fit(np.clip(scaler.transform(values),-8,8))
    return {'scaler':scaler,'model':model,'dates':len(values),'converged':bool(model.converged_),
        'iterations':int(model.n_iter_),'warnings':[{'category':type(w.message).__name__,'message':str(w.message)} for w in caught]}


def responsibilities(gate,rows):
    import numpy as np
    rows=list(rows);matrix(rows)
    if not rows:return np.empty((0,2))
    x=np.asarray([r['market'] for r in rows])
    result=gate['model'].predict_proba(np.clip(gate['scaler'].transform(x),-8,8))
    if result.shape!=(len(rows),2) or not np.isfinite(result).all() or (result<0).any() or not np.allclose(result.sum(axis=1),1.):
        raise ValueError('Invalid feature-only regime probabilities')
    return result


def predict(bundle,rows):
    import numpy as np
    rows=list(rows)
    if not rows:return []
    x=np.clip(bundle['scaler'].transform(matrix(rows)),-8,8)
    members=np.asarray([model.predict(x) for model in bundle['estimators']])
    if bundle['spec']['experts']==2:
        normalised=np.sum(members.T*responsibilities(bundle['gate'],rows),axis=1)
    else:normalised=np.median(members,axis=0)
    values=(normalised*np.asarray([r['risk_scale'] for r in rows])).tolist()
    if not all(math.isfinite(v) for v in values):raise ValueError('Nonfinite regime net forecasts')
    return values


def fit_variant(spec,train,calibration,gate):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    if spec not in VARIANTS:raise ValueError('Unregistered basket model')
    train,calibration=list(train),list(calibration)
    vol.prior.metrics.validate_rows(train);vol.prior.metrics.validate_rows(calibration)
    matrix(train);matrix(calibration)
    known=[r for r in train if r['net_y'] is not None];cal=[r for r in calibration if r['net_y'] is not None]
    if (len(known)<200 or len(cal)<50 or len({instant(r) for r in known})<30 or len({instant(r) for r in cal})<10):
        raise ValueError('Insufficient chronological training/calibration data')
    if max(vol.prior.temporal._time(r['label_end']) for r in train)>=min(instant(r) for r in calibration):
        raise ValueError('Training labels overlap calibration')
    weights=vol.prior.date_weights(known)
    scaler=StandardScaler().fit(matrix(known),sample_weight=weights)
    x=np.clip(scaler.transform(matrix(known)),-8,8)
    original=np.asarray([r['net_y'] for r in known]);target=original/np.asarray([r['risk_scale'] for r in known])
    bounds=np.quantile(target,[.025,.975]).tolist() if spec['kind']=='mlp' else None
    y=np.clip(target,*bounds) if bounds is not None else target.copy()
    if spec['kind']=='mlp':models=[MLPRegressor(**vol.prior.MLP_PARAMS,random_state=s) for s in vol.prior.SEEDS]
    elif spec['kind']=='ridge':models=[Ridge(alpha=10.,solver='svd') for _ in range(spec['experts'])]
    else:models=[HistGradientBoostingRegressor(**BOOST_PARAMS) for _ in range(spec['experts'])]
    probs=responsibilities(gate,known) if spec['experts']==2 else None
    messages=[];effective=[]
    for index,model in enumerate(models):
        w=weights*probs[:,index] if probs is not None else weights
        if not np.isfinite(w).all() or w.sum()<=0:raise ValueError('Empty regime training weights')
        effective.append(float(w.sum()))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always');model.fit(x,y,sample_weight=w)
        messages.extend({'member':index,'category':type(w.message).__name__,'message':str(w.message)} for w in caught)
    bundle={'spec':spec,'scaler':scaler,'estimators':models,'gate':gate if spec['experts']==2 else None,
        'target_clip_bounds':bounds,'clipped_train_targets':int(np.count_nonzero(y!=target)),
        'trained_rows':len(known),'calibration_rows':len(cal),'effective_weight_sums':effective,'warnings':messages,
        'train_net_mean':float(original.mean()),'train_net_median':median(original.tolist()),
        'train_profit_prior':mean(r['profit_y'] for r in known),'calibration_profit_prior':mean(r['profit_y'] for r in cal)}
    errors=sorted(abs(r['net_y']-p) for r,p in zip(cal,predict(bundle,cal)))
    bundle['interval_abs_pct']=errors[min(len(errors),math.ceil((len(errors)+1)*.8))-1]
    predict(bundle,known)
    return bundle


def decisions(name,rows,values=None):
    rows=list(rows);matrix(rows)
    keys=[(r['ticker'],instant(r)) for r in rows]
    if len(set(keys))!=len(keys):raise ValueError('Duplicate stock/instant in selection')
    groups=defaultdict(list)
    if name in POLICIES:
        values=list(values) if values is not None else []
        if len(values)!=len(rows) or not all(type(v) in (int,float) and math.isfinite(v) for v in values):
            raise ValueError('One finite net forecast per eligible row required')
        if name=='all':return [v>.1 for v in values]
        for index,(row,value) in enumerate(zip(rows,values)):
            if value>.1:groups[instant(row)].append((-value,row['ticker'],index))
    elif name in HEURISTICS:
        current=counts_by_instant(rows)
        for index,row in enumerate(rows):
            if current[instant(row)]<4 or row['market'][2]<=0 or row['market'][5]<.5:continue
            if name=='relative_momentum' and row['z'][1]>0 and row['x'][2]>0:
                groups[instant(row)].append((-row['z'][1],row['ticker'],index))
            elif name=='relative_pullback' and row['z'][0]<=-1 and row['x'][2]>0 and row['x'][9]>0:
                groups[instant(row)].append((row['z'][0],row['ticker'],index))
    else:raise ValueError('Unregistered selection rule')
    chosen={v[2] for group in groups.values() for v in sorted(group)[:2]}
    return [i in chosen for i in range(len(rows))]


def counts_by_instant(rows):
    counts=defaultdict(int)
    for row in rows:counts[instant(row)]+=1
    return counts


def run():
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    for name,version in vol.prior.storage.PACKAGES.items():
        if importlib.metadata.version(name)!=version:raise ValueError('Requires '+name+'=='+version)
    protocol=freeze()
    data=vol.prior.storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    old=vol.prior.storage.checked(ROOT/'dashboard/ai_models'/vol.prior.CONTROL)
    if (data['source_sha256']!=vol.prior.storage.source_hashes() or old['artifact_sha256']!=vol.prior.CONTROL_SHA
            or data['artifact_sha256']!=old['input_artifact_sha256']
            or old['source_sha256']!=vol.prior.robustness.ensemble.source_hashes()):raise ValueError('Input/control provenance differs')
    rows=enrich(data['rows']);stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder=ROOT/'data'/('regime-relative-models-'+stamp);folder.mkdir()
    folds=[];accounts=0
    with threadpool_limits(limits=2):
        for fold in vol.prior.temporal.FOLDS:
            groups=vol.prior.temporal.group_rows(rows,fold['periods']);test=groups['test']
            previous=next(f for f in old['folds'] if f['id']==fold['id'])
            control=vol.prior.verify_control(test,previous)
            lookup={(p['ticker'],p['at']):p for p in previous['predictions']}
            oldmask=[lookup[(r['ticker'],r['at'])]['selected_median'] for r in test]
            control=[vol.add_best_five(level,test,oldmask) for level in control]
            accounts+=independent.check_levels(test,oldmask,control)
            print(fold['id'],'fit one train-only two-state basket gate',flush=True)
            gate=fit_gate(groups['train'])
            gate_path=folder/(fold['id']+'-gate.joblib');joblib.dump(gate,gate_path,compress=3)
            variants=[]
            for spec in VARIANTS:
                print(fold['id'],spec['id'],'train, calibrate interval, score original answers',flush=True)
                bundle=fit_variant(spec,groups['train'],groups['calibration'],gate)
                features=[{k:r[k] for k in ('u','market','risk_scale')} for r in test]
                values=predict(bundle,features)
                path=folder/(fold['id']+'-'+spec['id']+'.joblib');joblib.dump(bundle,path,compress=3)
                repeated=predict(joblib.load(path),features)
                if not np.allclose(values,repeated,rtol=0.,atol=1e-12):raise ValueError('Reloaded model outputs differ')
                for policy in POLICIES:
                    if decisions(policy,test,values)!=decisions(policy,test,repeated):raise ValueError('Reloaded choices differ')
                report=vol.prior.metrics.evaluate(test,values,None,bundle['train_net_mean'],bundle['train_profit_prior'],
                    bundle['calibration_profit_prior'],train_net_median=bundle['train_net_median'])
                q=bundle['interval_abs_pct'];errors=[abs(r['net_y']-p) for r,p in zip(test,values) if r['net_y'] is not None]
                report.update(interval_abs_pct=q,interval_coverage_pct=100*mean(e<=q for e in errors) if errors else None)
                policies={}
                for name in POLICIES:
                    mask=decisions(name,test,values);levels=vol.checks(test,mask)
                    accounts+=independent.check_levels(test,mask,levels)
                    policies[name]={'robustness':levels,'year_screen_passed':vol.passes(levels)}
                    print(' ',name,'returns',[v['baseline']['report']['net_return_pct'] for v in levels],
                        'drop5',[v['drop_best_dates'][-1]['report']['net_return_pct'] if v['drop_best_dates'] else None for v in levels],flush=True)
                masks={name:decisions(name,test,values) for name in POLICIES}
                variants.append({**spec,**{k:bundle[k] for k in ('trained_rows','calibration_rows','effective_weight_sums',
                    'target_clip_bounds','clipped_train_targets','warnings','train_net_mean','train_net_median',
                    'train_profit_prior','calibration_profit_prior')},'report':report,'policies':policies,
                    'predictions':[{'ticker':r['ticker'],'at':r['at'],'actual_net_pct':r['net_y'],'predicted_net_pct':p,
                        **{'selected_'+name:masks[name][i] for name in POLICIES}} for i,(r,p) in enumerate(zip(test,values))],
                    'weights_local_file':path.relative_to(ROOT).as_posix(),'weights_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            heuristics={}
            for name in HEURISTICS:
                mask=decisions(name,test);levels=vol.checks(test,mask)
                accounts+=independent.check_levels(test,mask,levels)
                heuristics[name]={'robustness':levels,'year_screen_passed':vol.passes(levels),
                    'decisions':[{'ticker':r['ticker'],'at':r['at'],'selected':s,'actual_net_pct':r['net_y']} for r,s in zip(test,mask)]}
                print(fold['id'],name,'returns',[v['baseline']['report']['net_return_pct'] for v in levels],flush=True)
            folds.append({**fold,'variants':variants,'heuristics':heuristics,'control_robustness':control,
                'gate':{k:gate[k] for k in ('dates','converged','iterations','warnings')},
                'gate_local_file':gate_path.relative_to(ROOT).as_posix(),'gate_sha256':hashlib.sha256(gate_path.read_bytes()).hexdigest()})
    summary=[]
    names=[(v['id'],p) for v in VARIANTS for p in POLICIES]+[(n,None) for n in HEURISTICS]+[('old_median_control',None)]
    for name,policy in names:
        states={}
        for fold in folds:
            if policy:levels=next(v for v in fold['variants'] if v['id']==name)['policies'][policy]['robustness']
            elif name=='old_median_control':levels=fold['control_robustness']
            else:levels=fold['heuristics'][name]['robustness']
            states[fold['id']]=vol.passes(levels)
        summary.append({'id':name+('/'+policy if policy else ''),'year_screen_passes':states,
            'screen_2025_2026_passed':all(states[y] for y in ('test_2025','test_2026')),
            'all_three_years_passed':all(states.values()),'promoted_to_live':False})
    if source_hashes()!=vol.prior.storage.checked(ROOT/'data/regime-relative-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Registered code changed during evaluation')
    path=ROOT/'dashboard/ai_models'/('regime-relative-v1-'+stamp+'.json')
    vol.prior.storage.immutable(path,vol.prior.storage.signed({'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
        'rules':RULES,'entry_allowed':False,'historical':True,'prospective':False,'new_market_data':False,
        'source_sha256':source_hashes(),'protocol_sha256':protocol,'packages':vol.prior.storage.PACKAGES,
        'input_artifact_sha256':data['artifact_sha256'],'control_artifact_sha256':old['artifact_sha256'],
        'folds':folds,'summary':summary,'independent_cash_replays':accounts}))
    print('Saved',path.name,'independent cash replays',accounts,flush=True)
    print(summary,flush=True)
    return path


if __name__=='__main__':run()
