"""Fixed two-head MLP: net-positive probability and net one-hour return.

Only the training partition fits preprocessing/weights; August fits calibration.
Inference uses standard Python so a saved model needs no neural runtime.
"""
from math import isfinite, log, tanh
from datetime import datetime, timedelta, timezone
from . import market_ai as v1, market_ai_v2 as v2

VERSION = 'neural_trader_v1'
FEATURES, NAMES = v2.FEATURES, v2.NAMES
RULES = {
    'previous_trials':102, 'new_trials':1, 'hidden_layers':[16, 8], 'epochs':80,
    'batch_size':512, 'learning_rate':.001, 'l2':.01, 'seed':1729,
    'target_clip_pct':5., 'feature_clip':8., 'candidate_threshold':.50,
    'minimum_expected_net_pct':.05, 'minimum_train_rows':2000,
    'minimum_calibration_rows':500,
    'selection':'probability >=0.50 AND expected net >=0.05%; fixed before prices; LONG only',
    'target':'same next-open one-hour stopped LONG as market_ai v1, ordinary costs included',
    'training':'20 causal features; 16/8 tanh neurons; probability + return heads; deterministic Adam; no shuffling, search or early stopping',
    'calibration':'August only: logistic probability calibration and mean clipped-return residual offset',
    'limitations':'previously examined dates and survivor stocks; no historical book/news/trader identity training; no independent prospective proof',
}

vectors, examples = v2.vectors, v2.examples


def replay_rows(candles, ticker, lot):
    """Keep feature time separate from future labels/fills, including unknowns."""
    indices = {(c.time+timedelta(minutes=15)).astimezone(timezone.utc).isoformat():i for i,c in enumerate(candles)}
    rows = examples(candles,ticker,lot)
    for row in rows:
        i = indices[row['at']]
        o = row['outcome']
        nxt = candles[i+1] if i+1<len(candles) else None
        entry = nxt.open if nxt and nxt.time-candles[i].time==timedelta(minutes=15) and nxt.volume>0 else None
        stop = entry-max(1.5*row['atr'],entry*.003) if entry else None
        row.update(entry=entry,stop=stop if stop and stop>0 else None,
                   label_end=(datetime.fromisoformat(row['at'])+timedelta(hours=1)).isoformat())
        if o:
            row.update(entry=o['entry'],stop=o['stop'],label_end=o['label_end'])
    return rows


def fit(xs, ys, returns):
    import numpy as np
    x, y, r = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float), np.asarray(returns, dtype=float)
    n = len(x)
    if (x.ndim != 2 or x.shape[1] != len(FEATURES) or y.shape != (n,) or r.shape != (n,)
            or n < 40 or set(y) != {0., 1.} or not all(np.isfinite(a).all() for a in (x,y,r))):
        raise ValueError('Finite training features, returns and both classes required')
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-8] = 1.
    z = np.clip((x-center)/scale, -RULES['feature_clip'], RULES['feature_clip'])
    clipped = np.clip(r, -RULES['target_clip_pct'], RULES['target_clip_pct'])
    ret_center, ret_scale = float(clipped.mean()), max(.05, float(clipped.std()))
    targets = (clipped-ret_center)/ret_scale
    rng = np.random.default_rng(RULES['seed'])
    sizes = [len(FEATURES)] + RULES['hidden_layers'] + [2]
    params = []
    for a,b in zip(sizes, sizes[1:]):
        params.extend((rng.normal(0, (2/(a+b))**.5, (a,b)), np.zeros(b)))
    prior = float(y.mean())
    params[-1][0] = log(prior/(1-prior))
    first, second = [np.zeros_like(p) for p in params], [np.zeros_like(p) for p in params]
    step = 0
    for _ in range(RULES['epochs']):
        for start in range(0, n, RULES['batch_size']):
            end = min(n, start+RULES['batch_size'])
            a0 = z[start:end]
            a1 = np.tanh(a0@params[0]+params[1])
            a2 = np.tanh(a1@params[2]+params[3])
            out = a2@params[4]+params[5]
            p = 1/(1+np.exp(-np.clip(out[:,0],-35,35)))
            delta = np.column_stack((p-y[start:end], out[:,1]-targets[start:end]))/(end-start)
            d2 = (delta@params[4].T)*(1-a2*a2)
            d1 = (d2@params[2].T)*(1-a1*a1)
            grads = [a0.T@d1, d1.sum(axis=0), a1.T@d2, d2.sum(axis=0), a2.T@delta, delta.sum(axis=0)]
            step += 1
            for j in range(len(params)):
                if j%2 == 0:
                    grads[j] += RULES['l2']*params[j]
                first[j] = .9*first[j]+.1*grads[j]
                second[j] = .999*second[j]+.001*grads[j]**2
                params[j] -= RULES['learning_rate']*(first[j]/(1-.9**step))/(np.sqrt(second[j]/(1-.999**step))+1e-8)
    if not all(np.isfinite(p).all() for p in params):
        raise ValueError('Nonfinite trained network')
    return {'center':center.tolist(), 'scale':scale.tolist(), 'median':np.median(x,axis=0).tolist(),
            'layers':[{'weights':params[j].tolist(), 'bias':params[j+1].tolist()} for j in range(0,6,2)],
            'return_center':ret_center, 'return_scale':ret_scale, 'prior':prior, 'rows':n}


def raw(network, x):
    if len(x) != len(FEATURES) or not all(isfinite(v) for v in x):
        raise ValueError('Invalid neural features')
    a = [max(-8,min(8,(v-c)/s)) for v,c,s in zip(x,network['center'],network['scale'])]
    for i, layer in enumerate(network['layers']):
        a = [bias+sum(v*w[j] for v,w in zip(a,layer['weights'])) for j,bias in enumerate(layer['bias'])]
        if i < 2:
            a = [tanh(v) for v in a]
    return a[0], network['return_center']+a[1]*network['return_scale']


def estimate(model, x):
    score, net = raw(model['network'], x)
    return {'probability':v1.sigmoid(v1.logit(model['calibrator'],[score])),
            'expected_net_pct':max(-5.,min(5.,net+model['return_offset']))}


def predict(model, x):
    return estimate(model,x)['probability']


def candidate(model, x):
    e = estimate(model,x)
    return e['probability'] >= RULES['candidate_threshold'] and e['expected_net_pct'] >= RULES['minimum_expected_net_pct']


def projection(model, rows):
    projected = [{**r,'x':[raw(model['network'],r['x'])[0]]} for r in rows]
    identity = {'center':[0.], 'scale':[100.], 'weights':[0.,100.], 'prior':model['network']['prior']}
    return {'classifier':identity,'calibrator':model['calibrator']}, projected


def explanations(model, x):
    full = estimate(model,x)['expected_net_pct']
    changes = []
    for j, median in enumerate(model['network']['median']):
        changed = list(x)
        changed[j] = median
        changes.append(full-estimate(model,changed)['expected_net_pct'])
    return [{'name':NAMES[j], 'direction':'up' if changes[j]>0 else 'down','value':round(x[j],4)}
            for j in sorted(range(len(x)),key=lambda j:-abs(changes[j]))[:3] if abs(changes[j])>1e-9]


def valid_model(model):
    """Reject malformed finite-but-wrong shapes before inference."""
    try:
        finite = lambda v: type(v) in (int,float) and isfinite(v)
        net = model['network']
        n = len(FEATURES)
        for field in ('center','scale','median'):
            if len(net[field]) != n or not all(finite(v) for v in net[field]):
                return False
        if any(v <= 0 for v in net['scale']) or not finite(net['return_scale']) or net['return_scale'] <= 0:
            return False
        if not finite(net['return_center']) or not finite(net['prior']) or not 0 < net['prior'] < 1:
            return False
        sizes = [n]+RULES['hidden_layers']+[2]
        if len(net['layers']) != 3:
            return False
        for layer,a,b in zip(net['layers'],sizes,sizes[1:]):
            if len(layer['weights']) != a or len(layer['bias']) != b or not all(finite(v) for v in layer['bias']):
                return False
            if any(len(row) != b or not all(finite(v) for v in row) for row in layer['weights']):
                return False
        cal = model['calibrator']
        return (len(cal['weights'])==2 and len(cal['center'])==len(cal['scale'])==1
                and all(finite(v) for k in ('weights','center','scale') for v in cal[k])
                and cal['scale'][0]>0 and finite(model['return_offset']))
    except (KeyError,ValueError,TypeError,AttributeError):
        return False
