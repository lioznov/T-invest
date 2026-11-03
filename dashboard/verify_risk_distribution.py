"""Independent range/calibration/payoff/risk-selection and cash audit; no fits/network."""
import argparse
import hashlib
import math
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
from scipy.special import expit,logsumexp
from . import risk_distribution_study as study


def inference(bundle,rows):
    raw=np.asarray([r['u'] for r in rows])
    x=np.clip((raw-bundle['scaler'].mean_)/bundle['scaler'].scale_,-8,8)
    if bundle['family']=='quantiles':
        scale=np.asarray([r['risk_scale'] for r in rows])
        arrays=[m.predict(x)*scale for m in bundle['estimators']]
        return [{'q20':float(a),'q50':float(b),'q80':float(c),'lower':float(a-bundle['qhat']),
                 'upper':float(c+bundle['qhat'])} for a,b,c in zip(*arrays)]
    model,calibrator=bundle['estimators']
    expected=[0,1] if bundle['family']=='loss' else [0,1,2]
    assert list(model.classes_)==list(calibrator.classes_)==expected
    raw=model.decision_function(x)
    raw=raw.reshape(-1,1) if bundle['family']=='loss' else raw
    logits=raw@calibrator.coef_.T+calibrator.intercept_
    if bundle['family']=='loss':return [{'p_loss':float(p)} for p in expit(logits[:,0])]
    p=np.exp(logits-logsumexp(logits,axis=1,keepdims=True))
    means=p@np.asarray(bundle['class_means']);medians=p@np.asarray(bundle['class_medians'])
    return [{'p_classes':row.tolist(),'mean_net':float(a),'median_net':float(b)} for row,a,b in zip(p,means,medians)]


def selections(name,points):
    result=[]
    for p in points:
        median=p['q50']>.1;ordered=p['q20']<=p['q50']<=p['q80'];average=p['mean_net']>.1
        robust=p['median_net']>.1;safe=p['p_classes'][0]<=.2
        tests={'q_median':median,'q_risk':median and ordered and p['lower']>=-.5,
            'q_positive_lower':median and ordered and p['lower']>0,
            'base_loss20':p['base_net']>.1 and p['p_loss']<=.2,
            'base_loss10':p['base_net']>.1 and p['p_loss']<=.1,
            'q_loss20':median and ordered and p['p_loss']<=.2,
            'payoff_mean':average,'payoff_median':robust,'payoff_safe_mean':average and safe,
            'payoff_agree_safe':average and robust and safe}
        result.append(tests[name])
    return result


def verify(path):
    import joblib
    from sklearn.metrics import roc_auc_score
    from threadpoolctl import threadpool_limits
    source=study.features.vol.prior.storage
    path=Path(path).resolve();assert path.parent==(study.ROOT/'dashboard/ai_models').resolve()
    result=source.checked(path);protocol=source.checked(study.ROOT/'data/risk-distribution-protocol-v1.json')
    assert result['version']==study.VERSION and result['entry_allowed'] is False and result['prospective'] is False
    assert result['source_sha256']==study.source_hashes()==protocol['spec']['source_sha256']
    assert result['protocol_sha256']==source.m.digest(protocol['spec'])
    data=source.checked(study.ROOT/'data/nonlinear-morning-inputs-v1.json')
    assert data['artifact_sha256']==result['input_artifact_sha256'] and data['source_sha256']==source.source_hashes()
    control=source.checked(study.ROOT/'dashboard/ai_models'/study.features.vol.prior.CONTROL)
    assert control['artifact_sha256']==study.features.vol.prior.CONTROL_SHA==result['control_artifact_sha256']
    rows=study.prior_audit.recreate_features(data['rows']);files=fits=accounts=0
    with threadpool_limits(limits=2):
        for fold in result['folds']:
            groups=study.features.vol.prior.temporal.group_rows(rows,fold['periods']);test=groups['test']
            train=[r for r in groups['train'] if r['net_y'] is not None]
            cal=[r for r in groups['calibration'] if r['net_y'] is not None]
            counts=Counter(study.features.instant(r) for r in train)
            weights=np.asarray([len(train)/(len(counts)*counts[study.features.instant(r)]) for r in train])
            x=np.asarray([r['u'] for r in train]);center=np.average(x,axis=0,weights=weights)
            variance=np.average((x-center)**2,axis=0,weights=weights)
            y=np.asarray([r['net_y'] for r in train]);cy=np.asarray([r['net_y'] for r in cal])
            outputs={};bundles={}
            for item in fold['models']:
                local=(study.ROOT/item['weights_local_file']).resolve()
                assert local.is_relative_to((study.ROOT/'data').resolve())
                assert local.name==fold['id']+'-'+item['family']+'.joblib'
                assert hashlib.sha256(local.read_bytes()).hexdigest()==item['weights_sha256']
                bundle=joblib.load(local)  # Only own checksum-verified local model outputs.
                assert bundle['family']==item['family']
                np.testing.assert_allclose(bundle['scaler'].mean_,center,atol=1e-10)
                np.testing.assert_allclose(bundle['scaler'].var_,variance,atol=1e-10)
                if bundle['family']=='quantiles':
                    assert [m.quantile for m in bundle['estimators']]==[.2,.5,.8]
                    predicted=inference(bundle,cal)
                    scores=sorted(max(p['q20']-v,v-p['q80']) for p,v in zip(predicted,cy))
                    qhat=max(0.,scores[min(len(scores),math.ceil((len(scores)+1)*.8))-1])
                    assert math.isclose(qhat,bundle['qhat'],abs_tol=1e-10)
                    coverage=100*np.mean([p['lower']<=v<=p['upper'] for p,v in zip(predicted,cy)])
                    assert math.isclose(coverage,bundle['calibration_coverage_pct'],abs_tol=1e-10)
                else:
                    labels=(y<-.5).astype(int) if bundle['family']=='loss' else np.where(y<-.5,0,np.where(y>.5,2,1))
                    clabels=(cy<-.5).astype(int) if bundle['family']=='loss' else np.where(cy<-.5,0,np.where(cy>.5,2,1))
                    states=range(2 if bundle['family']=='loss' else 3)
                    np.testing.assert_allclose(bundle['train_class_priors'],[np.mean(labels==c) for c in states])
                    np.testing.assert_allclose(bundle['calibration_class_priors'],[np.mean(clabels==c) for c in states])
                    if bundle['family']=='payoff':
                        bounds=np.quantile(y,[.025,.975]);clipped=np.clip(y,*bounds)
                        np.testing.assert_allclose(bundle['payoff_clip_bounds'],bounds)
                        np.testing.assert_allclose(bundle['class_means'],[clipped[labels==c].mean() for c in range(3)])
                        np.testing.assert_allclose(bundle['class_medians'],[np.median(y[labels==c]) for c in range(3)])
                outputs[bundle['family']]=inference(bundle,test);bundles[bundle['family']]=bundle
                files+=1;fits+=len(bundle['estimators'])
            previous=next(f for f in control['folds'] if f['id']==fold['id'])
            lookup={(p['ticker'],p['at']):p for p in previous['predictions']}
            saved={(p['ticker'],p['at']):p for p in fold['predictions']}
            assert len(saved)==len(test)==len(fold['predictions'])
            points=[]
            for index,row in enumerate(test):
                key=(row['ticker'],row['at'])
                p={**outputs['quantiles'][index],**outputs['loss'][index],**outputs['payoff'][index],
                   'base_net':lookup[key]['median_net_pct']}
                for name,value in p.items():np.testing.assert_allclose(value,saved[key][name],rtol=0.,atol=1e-10)
                assert row['net_y']==saved[key]['actual_net_pct']
                points.append(p)
            oldmask=[lookup[(r['ticker'],r['at'])]['selected_median'] for r in test]
            accounts+=study.features.independent.check_levels(test,oldmask,fold['control_robustness'])
            for name in study.POLICIES:
                mask=selections(name,points)
                assert mask==[saved[(r['ticker'],r['at'])]['selected_'+name] for r in test]
                policy=fold['policies'][name]
                accounts+=study.features.independent.check_levels(test,mask,policy['robustness'])
                assert policy['year_screen_passed']==study.features.independent.independent_passes(policy['robustness'])
            known=[(r,p) for r,p in zip(test,points) if r['net_y'] is not None]
            actual=np.asarray([r['net_y'] for r,p in known]);loss=(actual<-.5).astype(int)
            prob=np.asarray([p['p_loss'] for r,p in known]);d=fold['diagnostics']
            assert math.isclose(np.mean((loss-prob)**2),d['loss']['brier'],abs_tol=1e-12)
            if len(set(loss))==2:assert math.isclose(roc_auc_score(loss,prob),d['loss']['auc'],abs_tol=1e-12)
            cprior=bundles['loss']['calibration_class_priors'][1]
            assert math.isclose(np.mean((loss-cprior)**2),d['loss']['calibration_baseline']['brier'],abs_tol=1e-12)
            labels=np.where(actual<-.5,0,np.where(actual>.5,2,1));onehot=np.eye(3)[labels]
            probs=np.asarray([p['p_classes'] for r,p in known]);cprior=np.asarray(bundles['payoff']['calibration_class_priors'])
            assert math.isclose(np.mean(np.sum((probs-onehot)**2,axis=1)),d['payoff']['brier'],abs_tol=1e-12)
            assert math.isclose(np.mean(np.sum((cprior-onehot)**2,axis=1)),d['payoff']['calibration_baseline_brier'],abs_tol=1e-12)
            assert math.isclose(-np.mean(np.log(np.maximum(probs[np.arange(len(labels)),labels],1e-15))),d['payoff']['log_loss'],abs_tol=1e-12)
            for q,key in zip((.2,.5,.8),('q20','q50','q80')):
                error=actual-np.asarray([p[key] for r,p in known])
                assert math.isclose(np.mean(np.maximum(q*error,(q-1)*error)),d['quantiles']['pinball'][str(q)],abs_tol=1e-12)
            coverage=100*np.mean([p['lower']<=r['net_y']<=p['upper'] for r,p in known])
            assert math.isclose(coverage,d['quantiles']['corrected_coverage_pct'],abs_tol=1e-10)
            for key in ('q50','mean_net','median_net'):
                mae=np.mean([abs(r['net_y']-p[key]) for r,p in known])
                assert math.isclose(mae,d['point_reports'][key]['regression']['mae_pct'],abs_tol=1e-10)
    assert files==9 and fits==21 and accounts==result['independent_cash_replays']
    for summary in result['summary']:
        states={f['id']:study.features.independent.independent_passes(f['control_robustness'] if summary['id']=='old_median_control'
                else f['policies'][summary['id']]['robustness']) for f in result['folds']}
        assert summary['year_screen_passes']==states
        assert summary['screen_2025_2026_passed']==all(states[k] for k in ('test_2025','test_2026'))
        assert summary['all_three_years_passed']==all(states.values()) and summary['promoted_to_live'] is False
    audit={'version':'risk_distribution_audit_v1','created_at':datetime.now(timezone.utc).isoformat(),'status':'verified',
        'result_artifact_sha256':result['artifact_sha256'],'input_artifact_sha256':data['artifact_sha256'],
        'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'files':files,'estimators':fits,
        'cash_replays':accounts,'entry_allowed':False,'checks':['independent basket/peer features andtrain-onlyscaling',
            'manual sigmoid/multinomial calibration from savedcoefficients','train-onlypayoffmaps',
            'calibration-onlyrangecorrection','all ten riskselections','original MAE/Brier/AUC/pinball/coverage',
            'separate cash replays andbest1/3/5rankings','independent robustnessflags']}
    output=study.ROOT/'data'/('risk-distribution-audit-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    source.immutable(output,source.signed(audit))
    print('PASS independent probabilities/ranges/payoffs/rules;',files,'files,',fits,'estimators,',accounts,'cash replays; no fits;',output.name)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('artifact');verify(parser.parse_args().artifact)
