"""Independent saved-data volatility-feature/inference/cash audit; no fits/network."""
import argparse
import hashlib
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

from . import volatility_overnight_study_v2 as study


def recreate_features(raw):
    """Independent feature formula; every simultaneous peer retained before label filtering."""
    groups=defaultdict(list)
    for row in study.prior.decision.enrich_rows(raw):
        x=row['x']; s=max(.25,x[8])
        if x[8]<0:raise ValueError('Negative past volatility')
        z=[x[0]/s,x[1]/(s*math.sqrt(5)),x[2]/(s*math.sqrt(20)),x[3]/s,x[4]/s,x[5]/s,
            x[6],x[7],x[8],x[9]/s,x[10]/s]+list(x[11:])
        groups[study.prior.temporal._time(row['at']).astimezone(timezone.utc)].append((row,z,s))
    out=[]
    for group in groups.values():
        for row,z,s in group:
            peers=[q[1][0] for q in group if q[0]['ticker']!=row['ticker']]
            extra=[mean(peers),pstdev(peers),mean(v>0 for v in peers),z[0]-mean(peers),len(peers)/6] if peers else [0.]*5
            out.append({**row,'z':z+extra,'risk_scale':s})
    return sorted(out,key=lambda r:(study.prior.temporal._time(r['at']),r['ticker']))


def inference(bundle,rows):
    import numpy as np
    x=np.asarray([r['z'] for r in rows])
    scaled=np.clip((x-bundle['scaler'].mean_)/bundle['scaler'].scale_,-8,8)
    predicted=[]
    for model in bundle['estimators']:
        kind=bundle['spec']['kind']
        if kind=='mlp':
            value=scaled
            for index,(weights,bias) in enumerate(zip(model.coefs_,model.intercepts_)):
                value=value@weights+bias
                if index<len(model.coefs_)-1:value=np.tanh(value)
            predicted.append(value.ravel())
        elif kind=='ridge':
            predicted.append(scaled@model.coef_+model.intercept_)
        else:
            if kind=='forest':assert model.n_jobs==1
            predicted.append(model.predict(scaled))
    return (np.median(np.asarray(predicted),axis=0)*np.asarray([r['risk_scale'] for r in rows])).tolist()


def mask_for(name,rows,values=None):
    if name=='plain':return [p>.1 for p in values]
    if name=='trend_guard':
        return [p>.1 and r['x'][2]>0 and r['x'][9]>0 and r['z'][20]>=.5 for r,p in zip(rows,values)]
    if name=='pullback_in_trend':
        return [r['x'][2]>0 and r['x'][9]>0 and r['z'][0]<=-1 and r['x'][7]>=0 for r in rows]
    if name=='strong_day':
        return [r['z'][3]>=1 and r['x'][6]>=.75 and r['x'][7]>=0 and r['x'][2]>0 and r['z'][20]>=.5 for r in rows]
    raise ValueError('Unknown audit rule')


def independent_days(rows,mask,multiplier):
    groups=defaultdict(list)
    for r,choose in zip(rows,mask):
        if choose:groups[study.prior.robustness.day_of(r)].append(r)
    if any(r['entry_price'] is None or r['actual_price'] is None for group in groups.values() for r in group):
        return []
    cash=100000.;fee=slip=.0005*multiplier;out=[]
    for day,group in sorted(groups.items()):
        budget=min(cash/4,cash/len(group));pnl=0.
        for r in group:
            buy=r['entry_price']*(1+slip)*(1+fee)
            quantity=math.floor(budget/(buy*r['lot']))*r['lot']
            pnl+=quantity*(r['actual_price']*(1-slip)*(1-fee)-buy)
        cash+=pnl
        out.append({'date':day.isoformat(),'pnl_rub':pnl})
    return out


def check_levels(rows,mask,levels):
    count=study.cash_verifier.check_levels(rows,mask,levels)
    for level in levels:
        expected=independent_days(rows,mask,level['cost_multiplier'])
        if level['drop_best_dates']:
            assert len(level['baseline']['daily'])==len(expected)
            positive=sorted((d for d in expected if d['pnl_rub']>0),key=lambda d:(-d['pnl_rub'],d['date']))
            for actual,wanted in zip(level['baseline']['daily'],expected):
                assert actual['date']==wanted['date']
                assert math.isclose(actual['pnl_rub'],wanted['pnl_rub'],abs_tol=1e-8)
            for omission in level['drop_best_dates']:
                assert omission['omitted_dates']==sorted(d['date'] for d in positive[:omission['requested_best_dates']])
    return count


def independent_passes(levels):
    if [v['cost_multiplier'] for v in levels]!=[1,2]:return False
    for level in levels:
        if level['baseline']['report']['selected_dates']<20:return False
        if [d['requested_best_dates'] for d in level['drop_best_dates']]!=[1,3,5]:return False
        for report in [level['baseline']['report']]+[d['report'] for d in level['drop_best_dates']]:
            value=report['net_return_pct']
            if report['status']!='ok' or value is None or not math.isfinite(value) or value<=0:return False
    return True


def verify(path):
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    path=Path(path).resolve()
    assert path.parent==(study.ROOT/'dashboard/ai_models').resolve()
    result=study.prior.storage.checked(path)
    protocol=study.prior.storage.checked(study.ROOT/'data/volatility-overnight-protocol-v2.json')
    assert result['version']==study.VERSION and result['entry_allowed'] is False and result['prospective'] is False
    assert result['source_sha256']==study.source_hashes()==protocol['spec']['source_sha256']
    assert result['protocol_sha256']==study.prior.decision.m.digest(protocol['spec'])
    data=study.prior.storage.checked(study.ROOT/'data/nonlinear-morning-inputs-v1.json')
    assert data['artifact_sha256']==result['input_artifact_sha256']
    assert data['source_sha256']==study.prior.storage.source_hashes()
    control=study.prior.storage.checked(study.ROOT/'dashboard/ai_models'/study.prior.CONTROL)
    assert control['artifact_sha256']==study.prior.CONTROL_SHA==result['control_artifact_sha256']
    rows=recreate_features(data['rows']);bundles=fits=accounts=0
    with threadpool_limits(limits=2):
        for fold in result['folds']:
            groups=study.prior.temporal.group_rows(rows,fold['periods']);test=groups['test']
            old=next(f for f in control['folds'] if f['id']==fold['id'])
            lookup={(p['ticker'],p['at']):p for p in old['predictions']}
            mask=[lookup[(r['ticker'],r['at'])]['selected_median'] for r in test]
            accounts+=check_levels(test,mask,fold['control_robustness'])
            for variant in fold['variants']:
                local=(study.ROOT/variant['weights_local_file']).resolve()
                assert local.is_relative_to((study.ROOT/'data').resolve())
                assert local.name==fold['id']+'-'+variant['id']+'.joblib'
                assert hashlib.sha256(local.read_bytes()).hexdigest()==variant['weights_sha256']
                bundle=joblib.load(local)  # Own local, checksum-verified outputs only.
                assert bundle['spec']['id']==variant['id']
                train=[r for r in groups['train'] if r['net_y'] is not None]
                counts=Counter(study.prior.robustness.day_of(r) for r in train)
                w=np.asarray([len(train)/(len(counts)*counts[study.prior.robustness.day_of(r)]) for r in train])
                x=np.asarray([r['z'] for r in train]);center=np.average(x,axis=0,weights=w)
                variance=np.average((x-center)**2,axis=0,weights=w)
                np.testing.assert_allclose(bundle['scaler'].mean_,center,atol=1e-10)
                np.testing.assert_allclose(bundle['scaler'].var_,variance,atol=1e-10)
                if variant['kind']=='mlp':
                    bounds=np.quantile([r['net_y']/r['risk_scale'] for r in train],[.025,.975])
                    np.testing.assert_allclose(bundle['target_clip_bounds'],bounds,atol=1e-12)
                else:assert bundle['target_clip_bounds'] is None
                cal=[r for r in groups['calibration'] if r['net_y'] is not None]
                errors=sorted(abs(r['net_y']-p) for r,p in zip(cal,inference(bundle,cal)))
                q=errors[min(len(errors),math.ceil((len(errors)+1)*.8))-1]
                assert math.isclose(q,bundle['interval_abs_pct'],abs_tol=1e-10)
                values=inference(bundle,test)
                saved={(p['ticker'],p['at']):p for p in variant['predictions']}
                assert len(saved)==len(test)==len(variant['predictions'])
                for r,p in zip(test,values):
                    oldp=saved[(r['ticker'],r['at'])]
                    assert math.isclose(p,oldp['predicted_net_pct'],abs_tol=1e-10)
                    assert oldp['actual_net_pct']==r['net_y']
                for name in study.POLICIES:
                    mask=mask_for(name,test,values)
                    assert mask==[saved[(r['ticker'],r['at'])]['selected_'+name] for r in test]
                    policy=variant['policies'][name]
                    accounts+=check_levels(test,mask,policy['robustness'])
                    assert policy['year_screen_passed']==independent_passes(policy['robustness'])
                actual_errors=[abs(r['net_y']-p) for r,p in zip(test,values) if r['net_y'] is not None]
                assert math.isclose(mean(actual_errors),variant['report']['regression']['mae_pct'],abs_tol=1e-10)
                bundles+=1;fits+=len(bundle['estimators'])
            for name in study.HEURISTICS:
                mask=mask_for(name,test)
                saved=fold['heuristics'][name]
                assert mask==[p['selected'] for p in saved['decisions']]
                accounts+=check_levels(test,mask,saved['robustness'])
                assert saved['year_screen_passed']==independent_passes(saved['robustness'])
    assert bundles==12 and fits==18 and accounts==result['independent_cash_replays']
    for summary in result['summary']:
        states={}
        for fold in result['folds']:
            if summary['id']=='old_median_control':levels=fold['control_robustness']
            elif '/' in summary['id']:
                model,policy=summary['id'].split('/')
                levels=next(v for v in fold['variants'] if v['id']==model)['policies'][policy]['robustness']
            else:levels=fold['heuristics'][summary['id']]['robustness']
            states[fold['id']]=independent_passes(levels)
        assert summary['year_screen_passes']==states
        assert summary['screen_2025_2026_passed']==all(states[y] for y in ('test_2025','test_2026'))
        assert summary['all_three_years_passed']==all(states.values())
        assert summary['promoted_to_live'] is False
    audit={'version':'volatility_audit_v1','created_at':datetime.now(timezone.utc).isoformat(),
        'result_artifact_sha256':result['artifact_sha256'],'input_artifact_sha256':data['artifact_sha256'],
        'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'status':'verified','bundles':bundles,'estimators':fits,'cash_replays':accounts,'entry_allowed':False,
        'checks':['independent X-only volatility/peer features','train-only weighted scaling','normalised target bounds',
                  'independent neural/ridge inference','original-unit intervals/MAE','every saved decision',
                  'separate cash replays','independent daily ranking of1/3/5 best dates']}
    output=study.ROOT/'data'/('volatility-audit-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    study.prior.storage.immutable(output,study.prior.storage.signed(audit))
    print('PASS independent features, scaling, inference and best-day ranks;',bundles,'bundles/',fits,'estimators,',accounts,'cash replays; no fits; audit',output.name)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact')
    verify(parser.parse_args().artifact)
