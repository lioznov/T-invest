"""Frozen three-outcome improvement: conditional payoffs, stock shrinkage, ordinal probabilities.

Only cached prices; no network, parameter search, orders or live-model promotion.
"""
import hashlib
import importlib.metadata
import math
import warnings
from datetime import datetime,timezone
from statistics import mean
from . import risk_distribution_study as old
from . import verify_risk_distribution as old_audit

ROOT=old.ROOT
VERSION='conditional_payoff_v1'
PRIOR='risk-distribution-v1-20261103-210058-519766.json'
PRIOR_SHA='a896699ebb889be617f4dcf327aeeacc3b3f729d95137a18ef9a2c3e9daa41d0'
SCORES=('ridge','boost','stock','ordinal_global','ordinal_stock','ordinal_boost')
GUARDS=('all','fixed','relative')
POLICIES=tuple(s+'/'+g for s in SCORES for g in GUARDS)
TICKERS=old.features.vol.prior.decision.m.TICKERS
REG_PARAMS={**old.features.vol.prior.HIST_PARAMS,'loss':'squared_error','min_samples_leaf':40,'random_state':20261104}
CLASS_PARAMS={**old.CLASS_PARAMS,'random_state':20261104}
CAL_PARAMS={**old.CAL_PARAMS,'random_state':20261104}
SHRINKAGE=100.
RULES={
    'data':'same7stocks cachedEXCHANGE17:45X/buy18open->exactnextcalendar10open originalnet; same3chronologicalfolds; LONGonly',
    'probability_control':'reuse exact saved payoff classifier/calibrator from previousriskdistribution, SHA-checked; no refits; compare originalprobabilities/means/decisions',
    'payoffheads':'3Ridgealpha100/SVD and3HGBconditional heads/fold, trained only knowntrain rows within ORIGINALclasses:<-.5,[-.5,.5],>.5',
    'headtargets':'GLOBALtrain2.5/97.5pct clipped originalnet / knownrisk_scale; dateequaltrainweights; globaltrainStandardScaler29 clip+-8',
    'support':'restore originalunits, then cap each predictedclasspayoff to min/max of clippedTRAINtargets in that class; actualtestreturns NEVER clipped',
    'stockmaps':'mean clippedtrainnet perstock/class, shrink n/(n+100) to frozen globalclassmean; zero rows=>global mean; no future outcomes',
    'ordinal':'twoHGBbinaryevents ORIGINALnet>=-.5 and ORIGINALnet>.5; trainonlyweights; separate nextyearLogisticsigmoid perthreshold',
    'ordinalprojection':'if gainP>nonlossP average both; Pclasses=(1-nonloss,nonloss-gain,gain); crossingcount disclosed; no outcome-based reordering',
    'scores':'oldP dot Ridgeheads/HGBheads/stockmaps; ordinalP dot frozen globalmeans/stockmaps/HGBheads; 6fixedforecasts',
    'policies':'eachscore>.10pct; threeguards:all, classlossP<=.2, classlossP<=.8*previouscalibrationlossfrequency;18rules allreported',
    'fit_count':'24mainfits=9Ridgeheads+9HGBheads+6ordinalHGB;6Logisticcalibrations;30newfits, stockmapstatsnotfits;6frozenpreviousestimators reused',
    'calibration':'only ordinalprobability calibration and80pct absolute-originalnet intervals for6scores; no bias/threshold/parameter tuning',
    'controls':'oldpayoffmean,oldpayoffsafe andoldmedian3net accounts/decisions must reproduce; same populations inclunknowns',
    'economics':'100000RUBcash/max25pctstock/wholelots;commission.05pct+slip.05pct eachperside;stressbothx2same decisions',
    'unknowns':'all eligible unknowns retained; selectedmissingentry/exit poisons whole account; no known-subset P&L',
    'screen':'same2025AND2026 >=20dates/year, positivecompletebaseline andall1/3/5bestpositive-date omissions BOTHcosts; additionalall3yearsflag',
    'diagnostics':'allquarters/omitquarters and1/3/5hindsight date omissions, allstocks together; not tradable futuredate selection',
    'reuse':'alltestyears alreadyviewed repeatedly; no untouchedholdout/searchcorrection/profitguarantee/automaticpromotion',
    'limits':'currentUID/lots/survivors; no dividendcash/tax/truefills/worstnightdrawdown/historicalbook/news/traderidentity',
    'reserve':'TATN/CHMF/PHOR unopened','entry_allowed':False,
}


def source_hashes():
    return {**old.source_hashes(),**{name:hashlib.sha256((ROOT/'dashboard'/name).read_bytes()).hexdigest()
        for name in ('conditional_payoff_study.py','verify_risk_distribution.py')}}


def freeze():
    spec=old.features.vol.prior.decision.jsonable({'version':VERSION,'rules':RULES,'scores':SCORES,'guards':GUARDS,
        'policies':POLICIES,'regressor':REG_PARAMS,'ridge':{'alpha':100.,'solver':'svd'},'classifier':CLASS_PARAMS,
        'calibration':CAL_PARAMS,'shrinkage':SHRINKAGE,'prior_sha256':PRIOR_SHA,'source_sha256':source_hashes(),
        'folds':old.features.vol.prior.temporal.FOLDS,'packages':old.features.vol.prior.storage.PACKAGES})
    path=ROOT/'data/conditional-payoff-protocol-v1.json'
    if path.exists():
        if old.features.vol.prior.storage.checked(path)['spec']!=spec:raise ValueError('Registered conditional model changed; use a new version')
    else:old.features.vol.prior.storage.immutable(path,old.features.vol.prior.storage.signed({'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec}))
    return old.features.vol.prior.decision.m.digest(spec)


def ordinal_labels(values):
    if not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise ValueError('Finite original net labels required')
    return [[int(v>=-.5) for v in values],[int(v>.5) for v in values]]


def ordinal_probabilities(nonloss,gain):
    import numpy as np
    a,b=np.asarray(nonloss,dtype=float),np.asarray(gain,dtype=float)
    if a.shape!=b.shape or a.ndim!=1 or not np.isfinite(a).all() or not np.isfinite(b).all() or ((a<0)|(a>1)|(b<0)|(b>1)).any():
        raise ValueError('Aligned ordinal probabilities between zero and one required')
    crossed=b>a;mid=(a+b)/2
    a=np.where(crossed,mid,a);b=np.where(crossed,mid,b)
    return np.column_stack((1-a,a-b,b)),crossed


def stock_maps(rows,clipped,labels,global_means):
    import numpy as np
    maps=[];counts=[]
    for ticker in TICKERS:
        values=[];sizes=[]
        for c in range(3):
            indices=[i for i,r in enumerate(rows) if r['ticker']==ticker and labels[i]==c]
            n=len(indices);weight=n/(n+SHRINKAGE)
            values.append(float(weight*np.mean(clipped[indices])+(1-weight)*global_means[c]) if n else float(global_means[c]))
            sizes.append(n)
        maps.append(values);counts.append(sizes)
    return maps,counts


def predict(bundle,frozen,rows):
    import numpy as np
    rows=list(rows)
    if not rows:return []
    if any(r.get('ticker') not in TICKERS for r in rows):raise ValueError('Known ticker required for stock-specific payoff')
    x=np.clip(bundle['scaler'].transform(old.features.matrix(rows)),-8,8)
    scale=np.asarray([r['risk_scale'] for r in rows]);bounds=np.asarray(bundle['support_bounds'])
    ridge=np.column_stack([m.predict(x)*scale for m in bundle['ridge_heads']])
    boost=np.column_stack([m.predict(x)*scale for m in bundle['boost_heads']])
    ridge=np.clip(ridge,bounds[:,0],bounds[:,1]);boost=np.clip(boost,bounds[:,0],bounds[:,1])
    pold=np.asarray([p['p_classes'] for p in old.predict(frozen,rows)])
    raw=[]
    for classifier,calibrator in zip(bundle['ordinal_classifiers'],bundle['ordinal_calibrators']):
        if list(classifier.classes_)!=[0,1] or list(calibrator.classes_)!=[0,1]:raise ValueError('Ordinal class order differs')
        raw.append(calibrator.predict_proba(classifier.decision_function(x).reshape(-1,1))[:,1])
    pord,crossed=ordinal_probabilities(raw[0],raw[1])
    maps=np.asarray([bundle['stock_maps'][TICKERS.index(r['ticker'])] for r in rows])
    values={'ridge':np.sum(pold*ridge,axis=1),'boost':np.sum(pold*boost,axis=1),'stock':np.sum(pold*maps,axis=1),
        'ordinal_global':pord@np.asarray(bundle['global_means']),'ordinal_stock':np.sum(pord*maps,axis=1),
        'ordinal_boost':np.sum(pord*boost,axis=1)}
    result=[{'old_probs':pold[i].tolist(),'ordinal_probs':pord[i].tolist(),'ordinal_raw':[float(raw[0][i]),float(raw[1][i])],
        'ordinal_crossed':bool(crossed[i]),'ridge_values':ridge[i].tolist(),'boost_values':boost[i].tolist(),
        'stock_values':maps[i].tolist(),'scores':{name:float(values[name][i]) for name in SCORES}} for i in range(len(rows))]
    if not np.isfinite(numeric_outputs(result)).all():raise ValueError('Nonfinite conditional payoff forecast')
    return result


def numeric_outputs(points):
    return [[*p['old_probs'],*p['ordinal_probs'],*p['ordinal_raw'],*p['ridge_values'],*p['boost_values'],
             *p['stock_values'],*[p['scores'][name] for name in SCORES]] for p in points]


def fit_bundle(train,calibration,frozen):
    import numpy as np
    from sklearn.linear_model import Ridge,LogisticRegression
    from sklearn.ensemble import HistGradientBoostingRegressor,HistGradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    train,calibration=list(train),list(calibration)
    old.features.vol.prior.metrics.validate_rows(train);old.features.vol.prior.metrics.validate_rows(calibration)
    old.features.matrix(train);old.features.matrix(calibration)
    known=[r for r in train if r['net_y'] is not None];cal=[r for r in calibration if r['net_y'] is not None]
    if len(known)<200 or len(cal)<50 or len({old.features.instant(r) for r in known})<30 or len({old.features.instant(r) for r in cal})<10:
        raise ValueError('Insufficient chronological train/calibration')
    if max(old.features.vol.prior.temporal._time(r['label_end']) for r in train)>=min(old.features.instant(r) for r in calibration):
        raise ValueError('Training labels overlap calibration')
    if frozen['family']!='payoff' or frozen['trained_rows']!=len(known) or frozen['calibration_rows']!=len(cal):raise ValueError('Frozen classifier population differs')
    y=np.asarray([r['net_y'] for r in known]);cy=np.asarray([r['net_y'] for r in cal])
    labels=np.asarray([old.class_label(float(v)) for v in y]);bounds=np.quantile(y,[.025,.975]);clipped=np.clip(y,*bounds)
    if any(sum(labels==c)<40 for c in range(3)):raise ValueError('At least40 known training rows perclass required')
    global_means=[float(clipped[labels==c].mean()) for c in range(3)]
    if not np.allclose(global_means,frozen['class_means'],rtol=0.,atol=1e-12):raise ValueError('Frozen classpayoff maps differ')
    weights=old.features.vol.prior.date_weights(known)
    scaler=StandardScaler().fit(old.features.matrix(known),sample_weight=weights)
    x=np.clip(scaler.transform(old.features.matrix(known)),-8,8);cx=np.clip(scaler.transform(old.features.matrix(cal)),-8,8)
    scale=np.asarray([r['risk_scale'] for r in known]);target=clipped/scale
    maps,counts=stock_maps(known,clipped,labels,global_means)
    bundle={'scaler':scaler,'ridge_heads':[],'boost_heads':[],'ordinal_classifiers':[],'ordinal_calibrators':[],
        'stock_maps':maps,'stock_counts':counts,'global_means':global_means,'payoff_clip_bounds':bounds.tolist(),
        'support_bounds':[[float(clipped[labels==c].min()),float(clipped[labels==c].max())] for c in range(3)],
        'class_counts':[int(sum(labels==c)) for c in range(3)],'trained_rows':len(known),'calibration_rows':len(cal),
        'calibration_loss_prior':float(np.mean(cy<-.5)),'train_net_mean':float(y.mean()),
        'train_net_median':float(np.median(y)),'warnings':[]}
    def fitted(model,data,target,w=None):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always');model.fit(data,target,**({'sample_weight':w} if w is not None else {}))
        bundle['warnings'].extend({'category':type(v.message).__name__,'message':str(v.message)} for v in caught)
        return model
    for c in range(3):
        take=labels==c
        bundle['ridge_heads'].append(fitted(Ridge(alpha=100.,solver='svd'),x[take],target[take],weights[take]))
        bundle['boost_heads'].append(fitted(HistGradientBoostingRegressor(**REG_PARAMS),x[take],target[take],weights[take]))
    for target_bin,cal_bin in zip(ordinal_labels(y.tolist()),ordinal_labels(cy.tolist())):
        if set(target_bin)!={0,1} or set(cal_bin)!={0,1}:raise ValueError('Both ordinal classes required in train/calibration')
        classifier=fitted(HistGradientBoostingClassifier(**CLASS_PARAMS),x,target_bin,weights)
        calibrator=fitted(LogisticRegression(**CAL_PARAMS),classifier.decision_function(cx).reshape(-1,1),cal_bin)
        bundle['ordinal_classifiers'].append(classifier);bundle['ordinal_calibrators'].append(calibrator)
    values=predict(bundle,frozen,cal)
    intervals={}
    for name in SCORES:
        errors=sorted(abs(r['net_y']-p['scores'][name]) for r,p in zip(cal,values))
        intervals[name]=errors[min(len(errors),math.ceil((len(errors)+1)*.8))-1]
    bundle['interval_abs_pct']=intervals
    predict(bundle,frozen,known)
    return bundle


def decisions(name,points,calibration_loss_prior):
    if name not in POLICIES:raise ValueError('Unregistered conditional rule')
    if type(calibration_loss_prior) not in (int,float) or not math.isfinite(calibration_loss_prior) or not 0<=calibration_loss_prior<=1:
        raise ValueError('Finite past calibration loss frequency required')
    score,guard=name.split('/');out=[]
    for p in points:
        value=p['scores'][score]
        probs=p['ordinal_probs'] if score.startswith('ordinal_') else p['old_probs']
        if (type(value) not in (int,float) or not math.isfinite(value) or len(probs)!=3
                or not all(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for v in probs)
                or not math.isclose(sum(probs),1.,abs_tol=1e-9)):raise ValueError('Valid finite payoff and classprobabilities required')
        limit=.2 if guard=='fixed' else .8*calibration_loss_prior
        out.append(value>.1 and (guard=='all' or probs[0]<=limit))
    return out


def diagnostics(bundle,frozen,rows,points):
    import numpy as np
    known=[(r,p) for r,p in zip(rows,points) if r['net_y'] is not None]
    y=np.asarray([r['net_y'] for r,p in known]);labels=np.asarray([old.class_label(float(v)) for v in y]);onehot=np.eye(3)[labels]
    prior=np.asarray(frozen['calibration_class_priors']);stats={}
    for kind in ('old_probs','ordinal_probs'):
        p=np.asarray([point[kind] for r,point in known])
        stats[kind]={'brier':float(np.mean(np.sum((p-onehot)**2,axis=1))),
            'log_loss':float(-np.mean(np.log(np.maximum(p[np.arange(len(labels)),labels],1e-15))))}
    stats['baseline']={'brier':float(np.mean(np.sum((prior-onehot)**2,axis=1))),
        'log_loss':float(-np.mean(np.log(np.maximum(prior[labels],1e-15))))}
    stats['ordinal_raw_crossings']=sum(p['ordinal_crossed'] for r,p in known)
    stats['samples']=len(known);stats['scores']={}
    for name in SCORES:
        predicted=np.asarray([p['scores'][name] for r,p in known]);error=predicted-y
        q=bundle['interval_abs_pct'][name]
        stats['scores'][name]={'mae_pct':float(np.mean(np.abs(error))),'rmse_pct':float(np.sqrt(np.mean(error**2))),
            'mean_baseline_mae_pct':float(np.mean(np.abs(y-bundle['train_net_mean']))),
            'interval_abs_pct':q,'interval_coverage_pct':float(100*np.mean(np.abs(error)<=q))}
    return stats


def run():
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    storage=old.features.vol.prior.storage
    for name,version in storage.PACKAGES.items():
        if importlib.metadata.version(name)!=version:raise ValueError('Requires '+name+'=='+version)
    protocol=freeze();previous=storage.checked(ROOT/'dashboard/ai_models'/PRIOR)
    data=storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    if (previous['artifact_sha256']!=PRIOR_SHA or previous['source_sha256']!=old.source_hashes()
            or data['artifact_sha256']!=previous['input_artifact_sha256'] or data['source_sha256']!=storage.source_hashes()):
        raise ValueError('Frozen input/classification source differs')
    rows=old.features.enrich(data['rows']);stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder=ROOT/'data'/('conditional-payoff-models-'+stamp);folder.mkdir()
    folds=[];accounts=0
    with threadpool_limits(limits=2):
        for fold in old.features.vol.prior.temporal.FOLDS:
            groups=old.features.vol.prior.temporal.group_rows(rows,fold['periods']);test=groups['test']
            before=next(f for f in previous['folds'] if f['id']==fold['id'])
            ref=next(m for m in before['models'] if m['family']=='payoff')
            frozen_path=(ROOT/ref['weights_local_file']).resolve()
            if not frozen_path.is_relative_to((ROOT/'data').resolve()) or hashlib.sha256(frozen_path.read_bytes()).hexdigest()!=ref['weights_sha256']:
                raise ValueError('Frozen local classmodel hash/path differs')
            frozen=joblib.load(frozen_path)
            lookup={(p['ticker'],p['at']):p for p in before['predictions']}
            if len(lookup)!=len(test) or len(lookup)!=len(before['predictions']):raise ValueError('Frozen forecast population differs')
            original=old.predict(frozen,test)
            for r,p in zip(test,original):
                saved=lookup[(r['ticker'],r['at'])]
                if saved['actual_net_pct']!=r['net_y'] or any(not np.allclose(p[k],saved[k],rtol=0.,atol=1e-12) for k in p):
                    raise ValueError('Frozen probabilities/payoffs differ')
            controls={'old_payoff_mean':before['policies']['payoff_mean']['robustness'],
                      'old_payoff_safe':before['policies']['payoff_safe_mean']['robustness'],
                      'old_median_control':before['control_robustness']}
            for name in controls:
                key={'old_payoff_mean':'selected_payoff_mean','old_payoff_safe':'selected_payoff_safe_mean'}
                mask=[lookup[(r['ticker'],r['at'])][key[name]] if name in key else lookup[(r['ticker'],r['at'])]['base_net']>.1 for r in test]
                accounts+=old.features.independent.check_levels(test,mask,controls[name])
            print(fold['id'],'fit sixconditionalheads andtwoordinalclassifiers/calibrators',flush=True)
            bundle=fit_bundle(groups['train'],groups['calibration'],frozen)
            inference_rows=[{k:r[k] for k in ('u','market','risk_scale','ticker')} for r in test]
            points=predict(bundle,frozen,inference_rows)
            path=folder/(fold['id']+'.joblib');joblib.dump(bundle,path,compress=3)
            repeated=predict(joblib.load(path),frozen,inference_rows)
            if not np.allclose(numeric_outputs(points),numeric_outputs(repeated),rtol=0.,atol=1e-12):raise ValueError('Reloaded conditional forecasts differ')
            policies={}
            for name in POLICIES:
                mask=decisions(name,points,bundle['calibration_loss_prior'])
                if mask!=decisions(name,repeated,bundle['calibration_loss_prior']):raise ValueError('Reloaded conditional decisions differ')
                levels=old.features.vol.checks(test,mask);accounts+=old.features.independent.check_levels(test,mask,levels)
                policies[name]={'robustness':levels,'year_screen_passed':old.features.vol.passes(levels)}
                for p,choose in zip(points,mask):p['selected_'+name]=choose
                print(' ',name,'returns',[l['baseline']['report']['net_return_pct'] for l in levels],
                    'drop5',[l['drop_best_dates'][-1]['report']['net_return_pct'] if l['drop_best_dates'] else None for l in levels],flush=True)
            for r,p in zip(test,points):p.update(ticker=r['ticker'],at=r['at'],actual_net_pct=r['net_y'])
            metadata={k:v for k,v in bundle.items() if k not in ('scaler','ridge_heads','boost_heads','ordinal_classifiers','ordinal_calibrators')}
            folds.append({**fold,'metadata':metadata,'policies':policies,'controls':controls,'predictions':points,
                'weights_local_file':path.relative_to(ROOT).as_posix(),'weights_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'frozen_model_local_file':ref['weights_local_file'],'frozen_model_sha256':ref['weights_sha256'],
                'diagnostics':diagnostics(bundle,frozen,test,points)})
    summary=[]
    for name in POLICIES+('old_payoff_mean','old_payoff_safe','old_median_control'):
        states={f['id']:old.features.vol.passes(f['policies'][name]['robustness'] if name in POLICIES else f['controls'][name]) for f in folds}
        summary.append({'id':name,'year_screen_passes':states,'screen_2025_2026_passed':all(states[k] for k in ('test_2025','test_2026')),
            'all_three_years_passed':all(states.values()),'promoted_to_live':False})
    if source_hashes()!=storage.checked(ROOT/'data/conditional-payoff-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Registered source changed during execution')
    path=ROOT/'dashboard/ai_models'/('conditional-payoff-v1-'+stamp+'.json')
    storage.immutable(path,storage.signed({'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),'rules':RULES,
        'entry_allowed':False,'historical':True,'prospective':False,'new_market_data':False,'source_sha256':source_hashes(),
        'protocol_sha256':protocol,'packages':storage.PACKAGES,'input_artifact_sha256':data['artifact_sha256'],
        'prior_artifact_sha256':previous['artifact_sha256'],'folds':folds,'summary':summary,'independent_cash_replays':accounts}))
    print('Saved',path.name,'independent cash replays',accounts,flush=True)
    print(summary,flush=True)
    return path


if __name__=='__main__':run()
