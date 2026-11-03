"""Independent basket features, diagonal-GMM inference, ranking and cash audit.

Loads only checksum-verified locally created model files. No fit or network.
"""
import argparse
import hashlib
import math
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path

from . import regime_relative_study as study


def recreate_features(raw):
    import numpy as np
    groups=defaultdict(list)
    for r in study.independent.recreate_features(raw):groups[study.instant(r)].append(r)
    out=[]
    for group in groups.values():
        first=np.asarray([r['z'][0] for r in group])
        market=[float(np.median([r['z'][i] for r in group])) for i in (0,1,2,3)]+[
            float(first.std()),float(np.mean(first>0))]
        out.extend({**r,'market':market,'u':r['z']+market} for r in group)
    return sorted(out,key=lambda r:(study.instant(r),r['ticker']))


def gate_probabilities(gate,rows):
    """Normalise diagonal Gaussian log densities directly from saved parameters."""
    import numpy as np
    from scipy.special import logsumexp
    raw=np.asarray([r['market'] for r in rows])
    x=np.clip((raw-gate['scaler'].mean_)/gate['scaler'].scale_,-8,8)
    model=gate['model'];logs=[]
    for weight,center,variance in zip(model.weights_,model.means_,model.covariances_):
        assert weight>0 and (variance>0).all()
        logs.append(math.log(weight)-.5*(6*math.log(2*math.pi)+np.log(variance).sum()+((x-center)**2/variance).sum(axis=1)))
    logs=np.asarray(logs).T
    return np.exp(logs-logsumexp(logs,axis=1,keepdims=True))


def inference(bundle,rows):
    import numpy as np
    raw=np.asarray([r['u'] for r in rows])
    x=np.clip((raw-bundle['scaler'].mean_)/bundle['scaler'].scale_,-8,8)
    values=[]
    for model in bundle['estimators']:
        if bundle['spec']['kind']=='mlp':
            a=x
            for index,(weights,bias) in enumerate(zip(model.coefs_,model.intercepts_)):
                a=a@weights+bias
                if index<len(model.coefs_)-1:a=np.tanh(a)
            values.append(a.ravel())
        elif bundle['spec']['kind']=='ridge':values.append(x@model.coef_+model.intercept_)
        else:values.append(model.predict(x))
    if bundle['spec']['experts']==2:
        output=(np.asarray(values).T*gate_probabilities(bundle['gate'],rows)).sum(axis=1)
    else:output=np.median(np.asarray(values),axis=0)
    return (output*np.asarray([r['risk_scale'] for r in rows])).tolist()


def selections(name,rows,values=None):
    if name=='all':return [v>.1 for v in values]
    counts=Counter(study.instant(r) for r in rows);ranked=defaultdict(list)
    for index,r in enumerate(rows):
        if name=='top2':
            if values[index]>.1:ranked[study.instant(r)].append((-values[index],r['ticker'],index))
            continue
        if counts[study.instant(r)]<4 or r['market'][2]<=0 or r['market'][5]<.5:continue
        if name=='relative_momentum' and r['z'][1]>0 and r['x'][2]>0:
            ranked[study.instant(r)].append((-r['z'][1],r['ticker'],index))
        if name=='relative_pullback' and r['z'][0]<=-1 and r['x'][2]>0 and r['x'][9]>0:
            ranked[study.instant(r)].append((r['z'][0],r['ticker'],index))
    chosen={v[2] for group in ranked.values() for v in sorted(group)[:2]}
    return [i in chosen for i in range(len(rows))]


def verify(path):
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    path=Path(path).resolve()
    assert path.parent==(study.ROOT/'dashboard/ai_models').resolve()
    result=study.vol.prior.storage.checked(path)
    protocol=study.vol.prior.storage.checked(study.ROOT/'data/regime-relative-protocol-v1.json')
    assert result['version']==study.VERSION and result['entry_allowed'] is False and result['prospective'] is False
    assert result['source_sha256']==study.source_hashes()==protocol['spec']['source_sha256']
    assert result['protocol_sha256']==study.vol.prior.decision.m.digest(protocol['spec'])
    data=study.vol.prior.storage.checked(study.ROOT/'data/nonlinear-morning-inputs-v1.json')
    assert data['artifact_sha256']==result['input_artifact_sha256']
    assert data['source_sha256']==study.vol.prior.storage.source_hashes()
    control=study.vol.prior.storage.checked(study.ROOT/'dashboard/ai_models'/study.vol.prior.CONTROL)
    assert control['artifact_sha256']==study.vol.prior.CONTROL_SHA==result['control_artifact_sha256']
    rows=recreate_features(data['rows']);files=fits=accounts=0
    with threadpool_limits(limits=2):
        for fold in result['folds']:
            groups=study.vol.prior.temporal.group_rows(rows,fold['periods']);test=groups['test']
            previous=next(f for f in control['folds'] if f['id']==fold['id'])
            look={(p['ticker'],p['at']):p for p in previous['predictions']}
            oldmask=[look[(r['ticker'],r['at'])]['selected_median'] for r in test]
            accounts+=study.independent.check_levels(test,oldmask,fold['control_robustness'])
            gate_path=(study.ROOT/fold['gate_local_file']).resolve()
            assert gate_path.is_relative_to((study.ROOT/'data').resolve())
            assert hashlib.sha256(gate_path.read_bytes()).hexdigest()==fold['gate_sha256']
            gate=joblib.load(gate_path)  # Own local signed-output chain only.
            by_date={study.instant(r):r['market'] for r in groups['train']}
            market=np.asarray(list(by_date.values()))
            np.testing.assert_allclose(gate['scaler'].mean_,market.mean(axis=0),atol=1e-10)
            np.testing.assert_allclose(gate['scaler'].var_,market.var(axis=0),atol=1e-10)
            assert gate['dates']==len(by_date)==fold['gate']['dates']
            assert gate['model'].n_components==2 and gate['model'].covariance_type=='diag'
            gate_manual=gate_probabilities(gate,test)
            np.testing.assert_allclose(gate_manual,study.responsibilities(gate,test),atol=1e-10)
            files+=1;fits+=1
            for variant in fold['variants']:
                local=(study.ROOT/variant['weights_local_file']).resolve()
                assert local.is_relative_to((study.ROOT/'data').resolve())
                assert local.name==fold['id']+'-'+variant['id']+'.joblib'
                assert hashlib.sha256(local.read_bytes()).hexdigest()==variant['weights_sha256']
                bundle=joblib.load(local)
                assert bundle['spec']['id']==variant['id']
                train=[r for r in groups['train'] if r['net_y'] is not None]
                counts=Counter(study.instant(r) for r in train)
                weights=np.asarray([len(train)/(len(counts)*counts[study.instant(r)]) for r in train])
                x=np.asarray([r['u'] for r in train]);center=np.average(x,axis=0,weights=weights)
                variance=np.average((x-center)**2,axis=0,weights=weights)
                np.testing.assert_allclose(bundle['scaler'].mean_,center,atol=1e-10)
                np.testing.assert_allclose(bundle['scaler'].var_,variance,atol=1e-10)
                if variant['experts']==2:
                    for field in ('weights_','means_','covariances_'):
                        np.testing.assert_array_equal(getattr(gate['model'],field),getattr(bundle['gate']['model'],field))
                    mass=(weights[:,None]*gate_probabilities(gate,train)).sum(axis=0)
                    np.testing.assert_allclose(mass,variant['effective_weight_sums'],atol=1e-8)
                else:assert bundle['gate'] is None
                if variant['kind']=='mlp':
                    np.testing.assert_allclose(bundle['target_clip_bounds'],np.quantile([r['net_y']/r['risk_scale'] for r in train],[.025,.975]),atol=1e-12)
                else:assert bundle['target_clip_bounds'] is None
                values=inference(bundle,test)
                saved={(p['ticker'],p['at']):p for p in variant['predictions']}
                assert len(saved)==len(test)==len(variant['predictions'])
                for r,value in zip(test,values):
                    p=saved[(r['ticker'],r['at'])]
                    assert math.isclose(value,p['predicted_net_pct'],abs_tol=1e-10)
                    assert p['actual_net_pct']==r['net_y']
                cal=[r for r in groups['calibration'] if r['net_y'] is not None]
                errors=sorted(abs(r['net_y']-p) for r,p in zip(cal,inference(bundle,cal)))
                q=errors[min(len(errors),math.ceil((len(errors)+1)*.8))-1]
                assert math.isclose(q,bundle['interval_abs_pct'],abs_tol=1e-10)
                for name in study.POLICIES:
                    mask=selections(name,test,values)
                    assert mask==[saved[(r['ticker'],r['at'])]['selected_'+name] for r in test]
                    policy=variant['policies'][name]
                    accounts+=study.independent.check_levels(test,mask,policy['robustness'])
                    assert policy['year_screen_passed']==study.independent.independent_passes(policy['robustness'])
                actual_errors=[abs(r['net_y']-p) for r,p in zip(test,values) if r['net_y'] is not None]
                assert math.isclose(float(np.mean(actual_errors)),variant['report']['regression']['mae_pct'],abs_tol=1e-10)
                files+=1;fits+=len(bundle['estimators'])
            for name in study.HEURISTICS:
                mask=selections(name,test);saved=fold['heuristics'][name]
                assert mask==[p['selected'] for p in saved['decisions']]
                accounts+=study.independent.check_levels(test,mask,saved['robustness'])
                assert saved['year_screen_passed']==study.independent.independent_passes(saved['robustness'])
    assert files==15 and fits==27 and accounts==result['independent_cash_replays']
    for summary in result['summary']:
        states={}
        for fold in result['folds']:
            if summary['id']=='old_median_control':levels=fold['control_robustness']
            elif '/' in summary['id']:
                model,policy=summary['id'].split('/')
                levels=next(v for v in fold['variants'] if v['id']==model)['policies'][policy]['robustness']
            else:levels=fold['heuristics'][summary['id']]['robustness']
            states[fold['id']]=study.independent.independent_passes(levels)
        assert summary['year_screen_passes']==states
        assert summary['screen_2025_2026_passed']==all(states[k] for k in ('test_2025','test_2026'))
        assert summary['all_three_years_passed']==all(states.values()) and summary['promoted_to_live'] is False
    audit={'version':'regime_relative_audit_v1','created_at':datetime.now(timezone.utc).isoformat(),'status':'verified',
        'result_artifact_sha256':result['artifact_sha256'],'input_artifact_sha256':data['artifact_sha256'],
        'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'files':files,'estimators':fits,
        'cash_replays':accounts,'entry_allowed':False,
        'checks':['independent basket/peer features','train-date gate standardisation','manual diagonal Gaussian probabilities',
                  'train-only expert preprocessing/weight masses','manual neural and ridge inference',
                  'original-unit calibration and MAE','all future-independent top2/relative selections',
                  'separate cash replays and daily best1/3/5 rankings','independent robustness flags']}
    output=study.ROOT/'data'/('regime-relative-audit-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    study.vol.prior.storage.immutable(output,study.vol.prior.storage.signed(audit))
    print('PASS independent features/Gaussian gate/experts/rankings;',files,'files,',fits,'estimators,',accounts,'cash replays; no fits;',output.name)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('artifact')
    verify(parser.parse_args().artifact)
