"""One frozen nonlinear challenger; v1 labels and monetary replay stay unchanged."""
from collections import defaultdict, deque
from datetime import timedelta, timezone
from math import isfinite, log
from statistics import mean
from . import market_ai as v1
from .signals import MSK

VERSION = 'market_ai_boost_v2'
FEATURES = v1.FEATURES + ('session_vwap_distance', 'rsi14', 'range_position20', 'efficiency20',
                         'clock_log_volume', 'atr_ratio', 'trend_volume', 'day_return')
NAMES = v1.NAMES + ('Цена относительно VWAP с 10:00', 'Сила покупок по движению цены (RSI)',
                   'Положение в диапазоне 20 свечей', 'Направленность движения',
                   'Объём против того же времени прошлых дней', 'Текущая волатильность против прошлой',
                   'Сочетание тренда и объёма', 'Изменение цены с 10:00')
RULES = {**v1.RULES, 'previous_trials':99, 'new_trials':1,
         'features':FEATURES, 'trees':80, 'learning_rate':.08, 'depth':2, 'bins':16,
         'min_leaf':200, 'leaf_l2':20., 'leaf_clip':1., 'clock_history':20, 'clock_min_days':5,
         'training':'fixed depth-2 histogram Newton logistic boosting; training-only quantiles; no early stopping/shuffling/parameter search',
         'observation_selection':'both later periods >=250 known rows; Brier strictly below paired v1 and AUC at least paired v1 and 0.52; no purchase authorization',
         'limitations':'reused history/current survivor universe; no learned book/news/trader identity; fixed candle outcomes, no real fills/taxes; extra features need five prior clock samples and uninterrupted 10:00 session'}


def vectors(candles):
    original, fs = v1.vectors(candles)
    rows = [None]*len(candles)
    clock = defaultdict(lambda: deque(maxlen=RULES['clock_history']))
    session_day, last, volume, weighted, opening, complete = None, None, 0., 0., None, False
    for i, c in enumerate(candles):
        local = c.time.astimezone(MSK)
        minute = local.hour*60+local.minute
        if local.date() != session_day:
            session_day, volume, weighted, opening, complete = local.date(), 0., 0., None, False
            last = None
        if minute == 600:
            volume, weighted, opening, complete = 0., 0., c.open, True
        elif minute >= 600 and (last is None or c.time-last != timedelta(minutes=15)):
            complete = False
        if minute >= 600:
            if c.volume <= 0:
                complete = False
            volume += c.volume
            weighted += (c.high+c.low+c.close)/3*c.volume
        history = clock[minute]
        if original[i] is not None and complete and volume > 0 and len(history) >= RULES['clock_min_days']:
            atr = fs[i]['atr']
            previous = candles[i-20:i]
            lo, hi = min(b.low for b in previous), max(b.high for b in previous)
            changes = [candles[j].close-candles[j-1].close for j in range(i-13, i+1)]
            gain, loss = sum(max(0, v) for v in changes), sum(max(0, -v) for v in changes)
            rsi = gain/(gain+loss) if gain+loss else .5
            path = sum(abs(candles[j].close-candles[j-1].close) for j in range(i-19, i+1))
            prior_atr = mean(f['atr'] for f in fs[i-80:i-20])
            rows[i] = original[i] + [(c.close-weighted/volume)/atr, rsi,
                (c.close-lo)/max(hi-lo, atr*.01), (c.close-candles[i-20].close)/path if path else 0.,
                log(c.volume/mean(history)), atr/max(prior_atr, atr*.01),
                original[i][4]*original[i][7], (c.close/opening-1)*100]
            if not all(isfinite(v) for v in rows[i]):
                raise ValueError('Nonfinite challenger features')
        # Today's value must never enter its own clock-volume baseline.
        if c.volume > 0 and local.weekday() < 5:
            history.append(c.volume)
        last = c.time
    return rows, fs


def examples(candles, ticker, lot):
    xs, _ = vectors(candles)
    at_to_x = {(c.time+timedelta(minutes=15)).astimezone(timezone.utc).isoformat():x for c,x in zip(candles,xs) if x is not None}
    result = []
    for row in v1.examples(candles, ticker, lot):
        key = row['at']
        if key in at_to_x:
            result.append({**row, 'x':at_to_x[key]})
    return result


def tree_value(tree, x):
    while 'feature' in tree:
        tree = tree['left'] if x[tree['feature']] <= tree['threshold'] else tree['right']
    return tree['value']


def raw(model, x):
    if len(x) != len(FEATURES) or not all(isfinite(v) for v in x):
        raise ValueError('Invalid challenger features')
    return model['bias'] + model['learning_rate']*sum(tree_value(t,x) for t in model['trees'])


def predict(model, x):
    return v1.sigmoid(v1.logit(model['calibrator'], [raw(model['classifier'],x)]))


def fit(xs, ys):
    import numpy as np
    x, y = np.asarray(xs,dtype=float), np.asarray(ys,dtype=float)
    if x.ndim != 2 or x.shape[1] != len(FEATURES) or y.shape != (len(x),) or not np.isfinite(x).all() or set(y) != {0.,1.}:
        raise ValueError('Finite features and two label classes required')
    if len(x) < 2*RULES['min_leaf']:
        raise ValueError('Insufficient leaf observations')
    prior = float(y.mean())
    bias = log(prior/(1-prior))
    logits = np.full(len(x), bias)
    cuts = [np.unique(np.quantile(x[:,j],np.arange(1,RULES['bins'])/RULES['bins'])) for j in range(x.shape[1])]
    bins = [np.searchsorted(c,x[:,j],side='left') for j,c in enumerate(cuts)]
    trees = []
    all_indices = np.arange(len(x))
    for _ in range(RULES['trees']):
        p = 1/(1+np.exp(-np.clip(logits,-35,35)))
        g, h = y-p, p*(1-p)

        def grow(indices, depth):
            total_g, total_h = float(g[indices].sum()), float(h[indices].sum())
            value = max(-RULES['leaf_clip'], min(RULES['leaf_clip'], total_g/(total_h+RULES['leaf_l2'])))
            leaf = {'value':value}
            if depth >= RULES['depth'] or len(indices) < 2*RULES['min_leaf']:
                return leaf
            parent = total_g**2/(total_h+RULES['leaf_l2'])
            best, chosen = 0., None
            for j, thresholds in enumerate(cuts):
                n = len(thresholds)+1
                counts = np.cumsum(np.bincount(bins[j][indices],minlength=n))[:-1]
                gs = np.cumsum(np.bincount(bins[j][indices],weights=g[indices],minlength=n))[:-1]
                hs = np.cumsum(np.bincount(bins[j][indices],weights=h[indices],minlength=n))[:-1]
                gains = gs**2/(hs+RULES['leaf_l2'])+(total_g-gs)**2/(total_h-hs+RULES['leaf_l2'])-parent
                allowed = (counts >= RULES['min_leaf']) & (len(indices)-counts >= RULES['min_leaf'])
                gains = np.where(allowed,gains,-np.inf)
                if len(gains):
                    k = int(np.argmax(gains))
                    if gains[k] > best+1e-12:
                        best, chosen = float(gains[k]), (j,k)
            if chosen is None:
                return leaf
            j,k = chosen
            mask = bins[j][indices] <= k
            return {'feature':j,'threshold':float(cuts[j][k]),
                    'left':grow(indices[mask],depth+1),'right':grow(indices[~mask],depth+1)}

        tree = grow(all_indices,0)
        trees.append(tree)
        # Same <= boundary in fitting, calibration and production inference.
        logits += RULES['learning_rate']*np.array([tree_value(tree,row) for row in x])
    return {'trees':trees,'bias':bias,'learning_rate':RULES['learning_rate'],'prior':prior,
            'rows':len(x),'median':np.median(x,axis=0).tolist()}


def projection(model, rows):
    # Reuse v1's frozen monetary simulator and metrics, not its learned predictions.
    projected = [{**r,'x':[raw(model['classifier'],r['x'])]} for r in rows]
    identity = {'center':[0.],'scale':[100.],'weights':[0.,100.], 'prior':model['classifier']['prior']}
    return {'classifier':identity,'calibrator':model['calibrator']}, projected


def observation_ready(evaluations, comparison):
    return all(evaluations[p]['classification']['samples'] >= 250
        and evaluations[p]['classification']['brier'] < comparison[p]['classification']['brier']
        and evaluations[p]['classification']['auc'] is not None
        and evaluations[p]['classification']['auc'] >= max(.52,comparison[p]['classification']['auc'])
        for p in ('test','additional'))


def explanations(model, x):
    """Median replacement sensitivity, not causal attribution or order-flow evidence."""
    full = predict(model,x)
    differences = []
    for j, median in enumerate(model['classifier']['median']):
        changed = list(x)
        changed[j] = median
        differences.append(full-predict(model,changed))
    return [{'name':NAMES[j], 'direction':'up' if differences[j]>0 else 'down', 'value':round(x[j],4)}
            for j in sorted(range(len(x)),key=lambda j:-abs(differences[j]))[:3] if abs(differences[j])>1e-9]
