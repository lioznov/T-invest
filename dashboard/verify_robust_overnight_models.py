"""Read-only independent checks of locally generated robust overnight models.

No fitting or network. Rebuilds neural inference from weights, train scaling,
and all money scenarios using a separate cash-account implementation.
"""
import argparse
import hashlib
import math
from pathlib import Path

from . import robust_overnight_models as study
from . import verify_overnight_experiments as independent


def check_account(rows, selected, multiplier, saved):
    actual = independent.account(rows, selected, multiplier)
    for key,value in actual.items():
        if key in ('net_return_pct','max_drawdown_pct'):
            independent.close(value,saved[key])
        else:
            assert value == saved[key], (key,value,saved[key])
    return 1


def check_levels(rows, selected, levels):
    count=0
    for level in levels:
        multiplier=level['cost_multiplier']
        count+=check_account(rows,selected,multiplier,level['baseline']['report'])
        for scenario in level['drop_best_dates']:
            omitted=set(scenario['omitted_dates'])
            mask=[choose and study.robustness.day_of(r).isoformat() not in omitted for r,choose in zip(rows,selected)]
            count+=check_account(rows,mask,multiplier,scenario['report'])
        for scenario in level['quarters']:
            take=[(study.robustness.day_of(r).month-1)//3+1 == scenario['quarter'] for r in rows]
            count+=check_account([r for r,k in zip(rows,take) if k],
                [s for s,k in zip(selected,take) if k],multiplier,scenario['report'])
        for scenario in level['omit_quarters']:
            mask=[choose and (study.robustness.day_of(r).month-1)//3+1 != scenario['omitted_quarter']
                  for r,choose in zip(rows,selected)]
            count+=check_account(rows,mask,multiplier,scenario['report'])
    return count


def verify(path):
    import joblib
    import numpy as np
    from threadpoolctl import threadpool_limits
    path=Path(path).resolve()
    assert path.parent == (study.ROOT/'dashboard/ai_models').resolve()
    result=study.storage.checked(path)
    protocol=study.storage.checked(study.ROOT/'data/robust-overnight-models-protocol-v1.json')
    assert result['source_sha256'] == study.source_hashes() == protocol['spec']['source_sha256']
    assert result['protocol_sha256'] == study.decision.m.digest(protocol['spec'])
    assert result['version'] == study.VERSION and result['entry_allowed'] is False and result['prospective'] is False
    data=study.storage.checked(study.ROOT/'data/nonlinear-morning-inputs-v1.json')
    assert data['artifact_sha256'] == result['input_artifact_sha256']
    assert data['source_sha256'] == study.storage.source_hashes()
    old=study.storage.checked(study.ROOT/'dashboard/ai_models'/study.CONTROL)
    assert old['artifact_sha256'] == result['control_artifact_sha256'] == study.CONTROL_SHA
    rows=study.decision.enrich_rows(data['rows'])
    bundles=fits=accounts=0
    with threadpool_limits(limits=2):
        for fold in result['folds']:
            groups=study.temporal.group_rows(rows,fold['periods'])
            test=groups['test']
            old_fold=next(f for f in old['folds'] if f['id'] == fold['id'])
            old_predictions={(r['ticker'],r['at']):r for r in old_fold['predictions']}
            mask=[old_predictions[(r['ticker'],r['at'])]['selected_median'] for r in test]
            accounts+=check_levels(test,mask,fold['control_robustness'])
            for item in fold['variants']:
                local=(study.ROOT/item['weights_local_file']).resolve()
                assert local.is_relative_to((study.ROOT/'data').resolve())
                assert local.name == fold['id']+'-'+item['id']+'.joblib'
                assert hashlib.sha256(local.read_bytes()).hexdigest() == item['weights_sha256']
                bundle=joblib.load(local)  # Own checksum-verified local outputs only.
                assert bundle['spec']['id'] == item['id']
                train=[r for r in groups['train'] if r['net_y'] is not None]
                x=study.matrix(train); w=study.date_weights(train)
                center=np.average(x,axis=0,weights=w)
                variance=np.average((x-center)**2,axis=0,weights=w)
                np.testing.assert_allclose(bundle['scaler'].mean_,center,atol=1e-10)
                np.testing.assert_allclose(bundle['scaler'].var_,variance,atol=1e-10)
                if item['clip_target']:
                    bounds=np.quantile([r['net_y'] for r in train],[.025,.975])
                    np.testing.assert_array_equal(bundle['target_clip_bounds'],bounds)
                else:
                    assert bundle['target_clip_bounds'] is None
                scaled=np.clip((study.matrix(test)-bundle['scaler'].mean_)/bundle['scaler'].scale_,-8,8)
                predictions=[]
                for estimator in bundle['estimators']:
                    if item['kind'] == 'mlp':
                        value=scaled
                        for index,(weights,bias) in enumerate(zip(estimator.coefs_,estimator.intercepts_)):
                            value=value@weights+bias
                            if index < len(estimator.coefs_)-1:
                                value=np.tanh(value)
                        predictions.append(value.ravel())
                    elif item['kind'] == 'huber':
                        predictions.append(scaled@estimator.coef_+estimator.intercept_)
                    else:
                        predictions.append(estimator.predict(scaled))
                predicted=np.median(np.asarray(predictions),axis=0).tolist()
                saved={(p['ticker'],p['at']):p for p in item['predictions']}
                assert len(saved) == len(test) == len(item['predictions'])
                selected=[]
                for row,value in zip(test,predicted):
                    p=saved[(row['ticker'],row['at'])]
                    assert math.isclose(value,p['predicted_net_pct'],abs_tol=1e-10)
                    assert p['actual_net_pct'] == row['net_y']
                    assert p['selected'] == (value > .1)
                    selected.append(p['selected'])
                accounts+=check_levels(test,selected,item['robustness'])
                bundles+=1; fits+=len(bundle['estimators'])
    assert bundles == 12 and fits == 24
    print(f'PASS: signed provenance, train-only scalers, {bundles} bundles/{fits} estimators, independent neural inference, {accounts} cash replays; no fits')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact')
    verify(parser.parse_args().artifact)
