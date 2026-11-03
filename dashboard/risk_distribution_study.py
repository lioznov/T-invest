"""Registered quantile/loss-risk/payoff models on the old real-price cache.

Offline long-only research: no fetching, tuning, orders, Telegram or promotion.
"""
import hashlib
import importlib.metadata
import math
import warnings
from datetime import datetime,timezone
from statistics import mean,median
from . import regime_relative_study as features
from . import verify_regime_relative as prior_audit

ROOT=features.ROOT
VERSION='risk_distribution_v1'
FAMILIES=('quantiles','loss','payoff')
QUANTILES=(.2,.5,.8)
POLICIES=('q_median','q_risk','q_positive_lower','base_loss20','base_loss10',
          'q_loss20','payoff_mean','payoff_median','payoff_safe_mean','payoff_agree_safe')
REG_PARAMS={**features.vol.prior.HIST_PARAMS,'loss':'quantile'}
CLASS_PARAMS={**features.vol.prior.HIST_PARAMS,'loss':'log_loss'}
CAL_PARAMS={'C':1.,'solver':'lbfgs','max_iter':1000,'random_state':20261103}
RULES={
    'data':'same7stock cachedEXCHANGE17:45features->buy18open->exactnextcalendar10open originalnet; LONGonly; same3chronologicalfolds',
    'features':'existing29causalvolatility/peer/basketfeatures; no trainedregimegate; everyfutureunknownpeer retained before labels',
    'training':'knowntrain only; dateequalweights; train-onlyweightedStandardScaler29 clip+-8; no search/earlystop',
    'fits':'15basefits=9quantileHGB+3binarylossHGB+3threeclassHGB;6Logisticcalibrations;21fits total',
    'quantiles':'train net/risk_scale conditional.2/.5/.8; restore originalunits; no reordering; rawcrossing vetoes interval-based entries',
    'range':'calibration-only qhat=max(0,80pct finite-sample quantile of max(rawlow-y,y-rawupper)); lower=rawlow-qhat upper=rawupper+qhat; no guaranteed futurecoverage',
    'loss':'binary event ORIGINALnet<-.5pct; HGBtrain, L2sigmoid nextyearcalibration scores only; not fit on in-sample basecandidates',
    'payoff':'three ORIGINALnet classes:<-.5 /[-.5,.5]/>.5;HGBtrain; nextyearmultinomialLogisticrawscore calibration',
    'maps':'train-only classmedians and classmeans after GLOBALtrain2.5/97.5pctwinsorisation; probabilities dotmaps; test returns NEVERclipped',
    'qpolicies':'median>.10; risk adds ordered rawquantiles AND correctedlower>=-.5; positive_lower adds ordered AND correctedlower>0',
    'filters':'oldmedian3net forecast>.10 AND lossP<=.2 or<=.1; newquantilemedian>.10 AND ordered AND lossP<=.2',
    'payoffpolicies':'mean>.10; medianmap>.10; mean>.10 AND classlossP<=.2; bothmaps>.10 AND classlossP<=.2',
    'baseline':'unchanged oldmedian3net forecasts and decisions checked against published accounts; never used to train risk features',
    'metrics':'originalnetpointMAE, quantilepinball/rawandadjustedcoverage/crossings; lossBrier/AUC vs train/calprevalence; multiclassBrier/logloss vs calibrationprevalence',
    'economics':'100000RUBcash/max25pctstock/wholelots; commission.05pct+slip.05pct eachperside; stressbothx2identical decisions',
    'unknowns':'preserve everyeligibleunknown; selectedmissingentry/exit poisons full account; no convenient known-subset profits',
    'screen':'same2025AND2026 >=20dates/year, positivecompletebaseline andall1/3/5bestpositive-date omissions at BOTHcosts; separateall3yearsflag',
    'diagnostics':'allquarters/omitquarters and1/3/5bestorigin-date omissions withallstocks together; hindsight sensitivity, not tradable futuredate selection',
    'reuse':'alltestyears alreadyviewed manytimes; no untouchedholdout/searchcorrection/profitguarantee/automaticpromotion',
    'limits':'currentUID/lots/survivors; no dividendcash/tax/truefills/worstnightdrawdown/historicalbook/news/traderidentity',
    'reserve':'TATN/CHMF/PHOR unopened','entry_allowed':False,
}


def source_hashes():
    return {**features.source_hashes(),**{name:hashlib.sha256((ROOT/'dashboard'/name).read_bytes()).hexdigest()
        for name in ('risk_distribution_study.py','verify_regime_relative.py')}}


def freeze():
    spec=features.vol.prior.decision.jsonable({'version':VERSION,'rules':RULES,'families':FAMILIES,'policies':POLICIES,
        'quantiles':QUANTILES,'regressor':REG_PARAMS,'classifier':CLASS_PARAMS,'calibration':CAL_PARAMS,
        'folds':features.vol.prior.temporal.FOLDS,'packages':features.vol.prior.storage.PACKAGES,
        'source_sha256':source_hashes(),'control_sha256':features.vol.prior.CONTROL_SHA})
    path=ROOT/'data/risk-distribution-protocol-v1.json'
    if path.exists():
        if features.vol.prior.storage.checked(path)['spec']!=spec:raise ValueError('Frozen risk study changed; use another version')
    else:features.vol.prior.storage.immutable(path,features.vol.prior.storage.signed({'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec}))
    return features.vol.prior.decision.m.digest(spec)


def class_label(value):
    if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Finite known net return required')
    return 0 if value<-.5 else 2 if value>.5 else 1


def corrected_range(raw_low,raw_high,targets):
    raw_low,raw_high,targets=list(raw_low),list(raw_high),list(targets)
    if (not targets or len(raw_low)!=len(raw_high) or len(raw_low)!=len(targets)
            or not all(type(v) in (int,float) and math.isfinite(v) for v in raw_low+raw_high+targets)):
        raise ValueError('Aligned finite known calibration ranges required')
    scores=sorted(max(low-y,y-high) for low,high,y in zip(raw_low,raw_high,targets))
    return max(0.,scores[min(len(scores),math.ceil((len(scores)+1)*.8))-1])


def predict(bundle,rows):
    import numpy as np
    rows=list(rows)
    if not rows:return []
    x=np.clip(bundle['scaler'].transform(features.matrix(rows)),-8,8)
    family=bundle['family']
    if family not in FAMILIES:raise ValueError('Unregistered prediction family')
    if family=='quantiles':
        scale=np.asarray([r['risk_scale'] for r in rows])
        values=[model.predict(x)*scale for model in bundle['estimators']]
        qhat=bundle.get('qhat',0.)
        result=[{'q20':float(a),'q50':float(b),'q80':float(c),'lower':float(a-qhat),'upper':float(c+qhat)}
                for a,b,c in zip(*values)]
    else:
        classifier,calibrator=bundle['estimators']
        if list(classifier.classes_)!=([0,1] if family=='loss' else [0,1,2]):raise ValueError('Classifier class order differs')
        raw=classifier.decision_function(x)
        raw=raw.reshape(-1,1) if family=='loss' else raw
        p=calibrator.predict_proba(raw)
        if not np.isfinite(p).all() or (p<0).any() or (p>1).any() or not np.allclose(p.sum(axis=1),1.):
            raise ValueError('Invalid calibrated probabilities')
        if family=='loss':
            if list(calibrator.classes_)!=[0,1]:raise ValueError('Loss class order differs')
            result=[{'p_loss':float(v)} for v in p[:,1]]
        else:
            if list(calibrator.classes_)!=[0,1,2]:raise ValueError('Payoff class order differs')
            means=p@np.asarray(bundle['class_means']);medians=p@np.asarray(bundle['class_medians'])
            result=[{'p_classes':v.tolist(),'mean_net':float(a),'median_net':float(b)} for v,a,b in zip(p,means,medians)]
    for point in result:
        for value in point.values():
            sequence=value if isinstance(value,list) else [value]
            if not all(math.isfinite(v) for v in sequence):raise ValueError('Nonfinite risk-model output')
    return result


def fit_family(family,train,calibration):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor,HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    if family not in FAMILIES:raise ValueError('Unregistered risk family')
    train,calibration=list(train),list(calibration)
    features.vol.prior.metrics.validate_rows(train);features.vol.prior.metrics.validate_rows(calibration)
    features.matrix(train);features.matrix(calibration)
    known=[r for r in train if r['net_y'] is not None];cal=[r for r in calibration if r['net_y'] is not None]
    if (len(known)<200 or len(cal)<50 or len({features.instant(r) for r in known})<30 or len({features.instant(r) for r in cal})<10):
        raise ValueError('Insufficient chronological training/calibration')
    if max(features.vol.prior.temporal._time(r['label_end']) for r in train)>=min(features.instant(r) for r in calibration):
        raise ValueError('Training labels overlap calibration')
    weights=features.vol.prior.date_weights(known)
    scaler=StandardScaler().fit(features.matrix(known),sample_weight=weights)
    x=np.clip(scaler.transform(features.matrix(known)),-8,8);cx=np.clip(scaler.transform(features.matrix(cal)),-8,8)
    y=np.asarray([r['net_y'] for r in known]);cy=np.asarray([r['net_y'] for r in cal])
    bundle={'family':family,'scaler':scaler,'estimators':[],'trained_rows':len(known),'calibration_rows':len(cal),
        'train_net_mean':float(y.mean()),'train_net_median':float(np.median(y)),
        'train_profit_prior':float(np.mean(y>0)),'calibration_profit_prior':float(np.mean(cy>0)),'warnings':[]}
    def fit_one(model,data,target,sample_weight=None):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            model.fit(data,target,**({'sample_weight':sample_weight} if sample_weight is not None else {}))
        bundle['warnings'].extend({'category':type(w.message).__name__,'message':str(w.message)} for w in caught)
        bundle['estimators'].append(model)
    if family=='quantiles':
        target=y/np.asarray([r['risk_scale'] for r in known])
        for q in QUANTILES:fit_one(HistGradientBoostingRegressor(**REG_PARAMS,quantile=q),x,target,weights)
        raw=predict(bundle,cal)
        bundle['qhat']=corrected_range([p['q20'] for p in raw],[p['q80'] for p in raw],cy.tolist())
        bundle['calibration_raw_crossings']=sum(not p['q20']<=p['q50']<=p['q80'] for p in raw)
        adjusted=predict(bundle,cal)
        bundle['calibration_coverage_pct']=100*mean(p['lower']<=r['net_y']<=p['upper'] for r,p in zip(cal,adjusted))
    else:
        target=(y<-.5).astype(int) if family=='loss' else np.asarray([class_label(float(v)) for v in y])
        ctarget=(cy<-.5).astype(int) if family=='loss' else np.asarray([class_label(float(v)) for v in cy])
        expected={0,1} if family=='loss' else {0,1,2}
        if set(target)!=expected or set(ctarget)!=expected:raise ValueError('All declared risk classes required in train/calibration')
        classifier=HistGradientBoostingClassifier(**CLASS_PARAMS)
        fit_one(classifier,x,target,weights)
        raw=classifier.decision_function(cx)
        fit_one(LogisticRegression(**CAL_PARAMS),raw.reshape(-1,1) if family=='loss' else raw,ctarget)
        bundle['train_class_priors']=[float(np.mean(target==c)) for c in sorted(expected)]
        bundle['calibration_class_priors']=[float(np.mean(ctarget==c)) for c in sorted(expected)]
        if family=='payoff':
            bounds=np.quantile(y,[.025,.975]);clipped=np.clip(y,*bounds)
            bundle['payoff_clip_bounds']=bounds.tolist()
            bundle['class_means']=[float(clipped[target==c].mean()) for c in (0,1,2)]
            bundle['class_medians']=[float(np.median(y[target==c])) for c in (0,1,2)]
    predict(bundle,known)
    return bundle


def decisions(name,points):
    if name not in POLICIES:raise ValueError('Unregistered risk policy')
    out=[]
    for p in points:
        for k in ('q20','q50','q80','lower','upper','p_loss','mean_net','median_net','base_net'):
            if type(p[k]) not in (int,float) or not math.isfinite(p[k]):raise ValueError('Finite aligned risk forecasts required')
        probs=p['p_classes']
        if (len(probs)!=3 or not all(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for v in probs)
                or not math.isclose(sum(probs),1.,abs_tol=1e-9) or not 0<=p['p_loss']<=1):raise ValueError('Invalid risk probabilities')
        ordered=p['q20']<=p['q50']<=p['q80'];med=p['q50']>.1;value=p['mean_net']>.1;middle=p['median_net']>.1
        if name=='q_median':choose=med
        elif name=='q_risk':choose=med and ordered and p['lower']>=-.5
        elif name=='q_positive_lower':choose=med and ordered and p['lower']>0
        elif name=='base_loss20':choose=p['base_net']>.1 and p['p_loss']<=.2
        elif name=='base_loss10':choose=p['base_net']>.1 and p['p_loss']<=.1
        elif name=='q_loss20':choose=med and ordered and p['p_loss']<=.2
        elif name=='payoff_mean':choose=value
        elif name=='payoff_median':choose=middle
        elif name=='payoff_safe_mean':choose=value and probs[0]<=.2
        else:choose=value and middle and probs[0]<=.2
        out.append(choose)
    return out


def diagnostics(bundles,rows,points):
    import numpy as np
    known=[(r,p) for r,p in zip(rows,points) if r['net_y'] is not None]
    if not known:raise ValueError('No known test outcomes for diagnostics')
    y=np.asarray([r['net_y'] for r,p in known]);loss=[int(v<-.5) for v in y]
    probs=[p['p_loss'] for r,p in known]
    fn=features.vol.prior.metrics._probability_metrics
    loss_report={**fn(loss,probs),'label':'original net < -0.5 percent','samples':len(known),
        'train_prevalence':bundles['loss']['train_class_priors'][1],'calibration_prevalence':bundles['loss']['calibration_class_priors'][1],
        'train_baseline':fn(loss,[bundles['loss']['train_class_priors'][1]]*len(known)),
        'calibration_baseline':fn(loss,[bundles['loss']['calibration_class_priors'][1]]*len(known))}
    target=np.asarray([class_label(float(v)) for v in y]);p=np.asarray([point['p_classes'] for r,point in known])
    onehot=np.eye(3)[target];cal=np.asarray(bundles['payoff']['calibration_class_priors'])
    cls={'samples':len(known),'brier':float(np.mean(np.sum((p-onehot)**2,axis=1))),
        'calibration_baseline_brier':float(np.mean(np.sum((cal-onehot)**2,axis=1))),
        'log_loss':float(-np.mean(np.log(np.maximum(p[np.arange(len(target)),target],1e-15)))),
        'calibration_baseline_log_loss':float(-np.mean(np.log(np.maximum(cal[target],1e-15))))}
    quantile={'samples':len(known),'raw_crossings':sum(not p['q20']<=p['q50']<=p['q80'] for r,p in known),
        'raw_coverage_pct':100*mean(p['q20']<=r['net_y']<=p['q80'] for r,p in known),
        'corrected_coverage_pct':100*mean(p['lower']<=r['net_y']<=p['upper'] for r,p in known),
        'corrected_crossings':sum(p['lower']>p['upper'] for r,p in known),
        'corrected_mean_signed_width_pct':mean(p['upper']-p['lower'] for r,p in known),'qhat':bundles['quantiles']['qhat'],
        'pinball':{str(q):mean(max(q*(r['net_y']-p[key]),(q-1)*(r['net_y']-p[key])) for r,p in known)
                   for q,key in zip(QUANTILES,('q20','q50','q80'))}}
    point_reports={}
    for key in ('q50','mean_net','median_net'):
        b=bundles['quantiles'] if key=='q50' else bundles['payoff']
        point_reports[key]=features.vol.prior.metrics.evaluate(rows,[p[key] for p in points],None,
            b['train_net_mean'],b['train_profit_prior'],b['calibration_profit_prior'],train_net_median=b['train_net_median'])
    return {'quantiles':quantile,'loss':loss_report,'payoff':cls,'point_reports':point_reports}


def run():
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    for name,version in features.vol.prior.storage.PACKAGES.items():
        if importlib.metadata.version(name)!=version:raise ValueError('Requires '+name+'=='+version)
    protocol=freeze()
    data=features.vol.prior.storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    old=features.vol.prior.storage.checked(ROOT/'dashboard/ai_models'/features.vol.prior.CONTROL)
    if (data['source_sha256']!=features.vol.prior.storage.source_hashes() or old['artifact_sha256']!=features.vol.prior.CONTROL_SHA
            or data['artifact_sha256']!=old['input_artifact_sha256'] or old['source_sha256']!=features.vol.prior.robustness.ensemble.source_hashes()):
        raise ValueError('Old input/control provenance differs')
    rows=features.enrich(data['rows']);stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder=ROOT/'data'/('risk-distribution-models-'+stamp);folder.mkdir()
    folds=[];accounts=0
    with threadpool_limits(limits=2):
        for fold in features.vol.prior.temporal.FOLDS:
            groups=features.vol.prior.temporal.group_rows(rows,fold['periods']);test=groups['test']
            previous=next(f for f in old['folds'] if f['id']==fold['id'])
            control=features.vol.prior.verify_control(test,previous)
            base={(p['ticker'],p['at']):p for p in previous['predictions']}
            old_mask=[base[(r['ticker'],r['at'])]['selected_median'] for r in test]
            control=[features.vol.add_best_five(level,test,old_mask) for level in control]
            accounts+=features.independent.check_levels(test,old_mask,control)
            bundles={};outputs={};reloaded={};saved=[]
            inference_rows=[{k:r[k] for k in ('u','market','risk_scale')} for r in test]
            for family in FAMILIES:
                print(fold['id'],family,'fit train and separate calibration',flush=True)
                bundle=fit_family(family,groups['train'],groups['calibration']);bundles[family]=bundle
                path=folder/(fold['id']+'-'+family+'.joblib');joblib.dump(bundle,path,compress=3)
                outputs[family]=predict(bundle,inference_rows);reloaded[family]=predict(joblib.load(path),inference_rows)
                for a,b in zip(outputs[family],reloaded[family]):
                    if a.keys()!=b.keys() or any(not np.allclose(a[k],b[k],rtol=0.,atol=1e-12) for k in a):raise ValueError('Reloaded risk-model output differs')
                saved.append({k:v for k,v in bundle.items() if k not in ('scaler','estimators')} |
                    {'weights_local_file':path.relative_to(ROOT).as_posix(),'weights_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            points=[];again=[]
            for i,row in enumerate(test):
                common={'ticker':row['ticker'],'at':row['at'],'actual_net_pct':row['net_y'],'base_net':base[(row['ticker'],row['at'])]['median_net_pct']}
                points.append({**common,**outputs['quantiles'][i],**outputs['loss'][i],**outputs['payoff'][i]})
                again.append({**common,**reloaded['quantiles'][i],**reloaded['loss'][i],**reloaded['payoff'][i]})
            policies={}
            for name in POLICIES:
                mask=decisions(name,points)
                if mask!=decisions(name,again):raise ValueError('Reloaded risk decisions differ')
                levels=features.vol.checks(test,mask);accounts+=features.independent.check_levels(test,mask,levels)
                policies[name]={'robustness':levels,'year_screen_passed':features.vol.passes(levels)}
                for p,choose in zip(points,mask):p['selected_'+name]=choose
                print(fold['id'],name,'returns',[v['baseline']['report']['net_return_pct'] for v in levels],
                    'drop5',[v['drop_best_dates'][-1]['report']['net_return_pct'] if v['drop_best_dates'] else None for v in levels],flush=True)
            folds.append({**fold,'models':saved,'predictions':points,'policies':policies,
                'control_robustness':control,'diagnostics':diagnostics(bundles,test,points)})
    summary=[]
    for name in POLICIES+('old_median_control',):
        states={f['id']:features.vol.passes(f['control_robustness'] if name=='old_median_control' else f['policies'][name]['robustness']) for f in folds}
        summary.append({'id':name,'year_screen_passes':states,'screen_2025_2026_passed':all(states[k] for k in ('test_2025','test_2026')),
            'all_three_years_passed':all(states.values()),'promoted_to_live':False})
    if source_hashes()!=features.vol.prior.storage.checked(ROOT/'data/risk-distribution-protocol-v1.json')['spec']['source_sha256']:
        raise ValueError('Registered code changed during study')
    path=ROOT/'dashboard/ai_models'/('risk-distribution-v1-'+stamp+'.json')
    features.vol.prior.storage.immutable(path,features.vol.prior.storage.signed({'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
        'entry_allowed':False,'historical':True,'prospective':False,'new_market_data':False,'rules':RULES,
        'source_sha256':source_hashes(),'protocol_sha256':protocol,'packages':features.vol.prior.storage.PACKAGES,
        'input_artifact_sha256':data['artifact_sha256'],'control_artifact_sha256':old['artifact_sha256'],
        'folds':folds,'summary':summary,'independent_cash_replays':accounts}))
    print('Saved',path.name,'independent cash replays',accounts,flush=True)
    print(summary,flush=True)
    return path


if __name__=='__main__':run()
