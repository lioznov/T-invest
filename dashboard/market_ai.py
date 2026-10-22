"""Trainable causal classifier of a hypothetical one-hour LONG outcome."""
from datetime import datetime, timedelta, timezone
from math import exp, isfinite, log, sin, cos, pi
from statistics import mean
from .signals import MSK, features
from .method_study import validate_candles
from .risk import position_size

VERSION = 'market_ai_logistic_v1'
FEATURES = ('return1', 'return4', 'return12', 'ema_distance', 'ema_slope', 'body', 'wick_balance',
            'log_volume', 'atr_fraction', 'prior_high_distance', 'hour_sin', 'hour_cos')
NAMES = ('Изменение последней свечи', 'Движение за четыре свечи', 'Движение за двенадцать свечей',
         'Цена относительно средней', 'Направление средней цены', 'Движение внутри свечи',
         'Соотношение теней свечи', 'Объём относительно обычного', 'Размах колебаний',
         'Расстояние до прежнего максимума', 'Время торгов: компонент 1', 'Время торгов: компонент 2')
PERIODS = {'train':('2026-03-30','2026-08-01'), 'calibration':('2026-08-01','2026-09-01'),
           'test':('2026-09-01','2026-10-03'), 'additional':('2026-10-03','2026-10-13')}
RULES = {'previous_trials':98,'new_trials':1,'features':FEATURES,'horizon_bars':4,'bar_minutes':15,
         'minimum_context_bars':100,'decision_close_msk':'10:30–17:00 weekdays',
         'label':'net result >0 of next-open LONG, stop 1.5 prior ATR/min0.3%, otherwise close after four contiguous same-day bars; unknown paths retained',
         'fees_bps_per_side':5,'slippage_bps_per_side':5,'capital_per_stock':100000,'risk_pct':.5,'allocation_pct':25,
         'training':'standardisation on training only; fixed L2 logistic Newton solver; no shuffling or parameter search',
         'l2':.05,'iterations':40,'calibration':'one-dimensional logistic on raw training-model logit using August only',
         'sample_clip':8,'candidate_threshold':.60,'minimum_train_rows':2000,'minimum_calibration_rows':500,
         'selection':'one nonoverlapping position per stock; fixed threshold; normal and doubled costs',
         'paper_readiness':'both test periods >=30 verified trades, positive returns with normal/double costs, no unknown trades; test Brier beats training-prior baseline',
         'real_deployment':'no independent prospective trading evidence yet; entry_allowed remains false in v1',
         'limitations':'previously examined history/current survivor universe; no trained historical book/news/trader identity, no taxes or real fills'}


def vectors(candles):
    validate_candles(candles)
    base, rows = features(candles), [None]*len(candles)
    for i in range(100,len(candles)):
        c, f = candles[i], base[i]
        if c.volume <= 0 or f['atr'] <= 0 or f['ema'] <= 0 or f['rvol'] <= 0:
            continue
        atr = f['atr']
        hour = c.time.astimezone(MSK).hour+c.time.astimezone(MSK).minute/60
        rows[i] = [(c.close/candles[i-n].close-1)*100 for n in (1,4,12)]+[
            (c.close-f['ema'])/atr, (f['ema']-base[i-3]['ema'])/atr,
            (c.close-c.open)/atr, ((min(c.close,c.open)-c.low)-(c.high-max(c.close,c.open)))/atr,
            log(f['rvol']), atr/c.close*100,
            (c.close-max(b.high for b in candles[i-20:i]))/atr,
            sin(2*pi*hour/24), cos(2*pi*hour/24)]
        if not all(isfinite(v) for v in rows[i]):
            raise ValueError('Invalid feature value')
    return rows,base


def examples(candles,ticker,lot):
    rows, base = vectors(candles)
    result=[]
    for i, x in enumerate(rows):
        if x is None:
            continue
        at=candles[i].time+timedelta(minutes=15)
        local=at.astimezone(MSK)
        if local.weekday()>=5 or not 630<=local.hour*60+local.minute<=1020:
            continue
        row={'ticker':ticker,'lot':lot,'at':at.astimezone(timezone.utc).isoformat(),'x':x,'atr':base[i]['atr'],'outcome':None,'y':None,'reason':'missing_future'}
        if i+4<len(candles):
            future=candles[i+1:i+5]
            valid=all(b.volume>0 and b.time.astimezone(MSK).date()==local.date()
                      and b.time-candles[i+j].time==timedelta(minutes=15)
                      and .6<=b.open/candles[i+j].close<=1.4 for j,b in enumerate(future))
            if valid:
                entry=future[0].open
                distance=max(1.5*base[i]['atr'],entry*.003)
                stop=entry-distance
                if stop>0:
                    out,reason,exit_at=future[-1].close,'hour',future[-1].time+timedelta(minutes=15)
                    for b in future:
                        if b.open<=stop:
                            out,reason,exit_at=b.open,'gap_stop',b.time
                            break
                        if b.low<=stop:
                            out,reason,exit_at=stop,'stop',b.time+timedelta(minutes=15)
                            break
                    net=out*.9995*.9995/(entry*1.0005*1.0005)-1
                    # Label interval always ends at the full 60-minute horizon,
                    # including early stops, so chronological boundaries purge it.
                    row.update(y=int(net>0),reason=reason,outcome={'entry':entry,'exit':out,'stop':stop,
                        'exit_at':exit_at.astimezone(timezone.utc).isoformat(),'label_end':(future[-1].time+timedelta(minutes=15)).astimezone(timezone.utc).isoformat(),'net_fraction':net})
            else:
                row['reason']='unverified_future'
        result.append(row)
    return result


def split(rows):
    groups={k:[] for k in PERIODS}
    for row in rows:
        day=row['at'][:10]
        for key,(first,last) in PERIODS.items():
            if first<=day<last:
                if row['outcome'] and row['outcome']['label_end'][:10]>=last:
                    continue
                groups[key].append(row)
    return groups


def sigmoid(value):
    return 1/(1+exp(-max(-35,min(35,value))))


def fit(xs,ys):
    import numpy as np
    x,y=np.asarray(xs,dtype=float),np.asarray(ys,dtype=float)
    if x.ndim!=2 or len(y)!=len(x) or len(x)<20 or not np.isfinite(x).all() or set(y)!={0.,1.}:
        raise ValueError('Training needs finite features and two outcome classes')
    center=x.mean(axis=0)
    scale=x.std(axis=0)
    scale[scale<1e-8]=1
    z=np.column_stack((np.ones(len(x)),np.clip((x-center)/scale,-8,8)))
    weights=np.zeros(z.shape[1])
    prior=float(y.mean())
    weights[0]=log(prior/(1-prior))
    penalty=np.eye(len(weights))*RULES['l2']
    penalty[0,0]=0
    for _ in range(RULES['iterations']):
        p=1/(1+np.exp(-np.clip(z@weights,-35,35)))
        gradient=z.T@(p-y)/len(x)+penalty@weights
        hessian=(z.T*(p*(1-p)))@z/len(x)+penalty+np.eye(len(weights))*1e-9
        step=np.linalg.solve(hessian,gradient)
        weights-=step
        if float(np.max(np.abs(step)))<1e-7:
            break
    if not np.isfinite(weights).all():
        raise ValueError('Nonfinite fitted coefficients')
    return {'center':center.tolist(),'scale':scale.tolist(),'weights':weights.tolist(),'prior':prior,'rows':len(x)}


def logit(model,x):
    if len(x)!=len(model['center']) or any(not isfinite(v) for v in x):
        raise ValueError('Invalid inference features')
    return model['weights'][0]+sum(w*max(-8,min(8,(v-c)/s)) for w,v,c,s in
                                  zip(model['weights'][1:],x,model['center'],model['scale']))


def predict(model,x):
    raw=logit(model['classifier'],x)
    return sigmoid(logit(model['calibrator'],[raw]))


def metrics(model,rows):
    known=[r for r in rows if r['y'] is not None]
    pairs=[(predict(model,r['x']),r['y']) for r in known]
    if not pairs:
        return {'samples':0,'unknown':len(rows),'brier':None,'baseline_brier':None,'accuracy_pct':None,'auc':None}
    prior=model['classifier']['prior']
    # Mann-Whitney ranks with average ranks for tied predictions.
    ordered=sorted(pairs)
    positives=sum(y for _,y in pairs)
    negatives=len(pairs)-positives
    rank_sum,i=0.,0
    while i<len(ordered):
        j=i+1
        while j<len(ordered) and ordered[j][0]==ordered[i][0]:
            j+=1
        rank_sum+=sum(y for _,y in ordered[i:j])*(i+1+j)/2
        i=j
    return {'samples':len(pairs),'unknown':len(rows)-len(pairs),'positive_pct':mean(y for _,y in pairs)*100,
            'accuracy_pct':mean((p>=.5)==bool(y) for p,y in pairs)*100,
            'baseline_accuracy_pct':mean((prior>=.5)==bool(y) for _,y in pairs)*100,
            'brier':mean((p-y)**2 for p,y in pairs),'baseline_brier':mean((prior-y)**2 for _,y in pairs),
            'auc':(rank_sum-positives*(positives+1)/2)/(positives*negatives) if positives and negatives else None}


def simulate(model,rows,multiplier=1):
    cash,until,trades,unknown=100000.,'',[],0
    fee=slip=.0005*multiplier
    for row in sorted(rows,key=lambda r:r['at']):
        if row['at']<until or predict(model,row['x'])<RULES['candidate_threshold']:
            continue
        if row['outcome'] is None:
            unknown+=1
            until=(datetime.fromisoformat(row['at'])+timedelta(hours=1)).isoformat()
            continue
        o=row['outcome']
        size=position_size(cash,.5,25,round(o['entry'],9),round(o['stop'],9),round(o['entry']+2*(o['entry']-o['stop']),9),row['lot'],
                           fee_bps=5*multiplier,slippage_bps=5*multiplier)
        shares=size['shares']
        if shares:
            pnl=shares*(o['exit']*(1-fee)*(1-slip)-o['entry']*(1+fee)*(1+slip))
            cash+=pnl
            trades.append({'ticker':row['ticker'],'at':row['at'],'exit_at':o['exit_at'],'pnl':pnl,'shares':shares})
            until=o['label_end']
    return {'trades':len(trades),'unknown_trades':unknown,'return_pct':(cash/100000-1)*100,
            'win_rate_pct':mean(t['pnl']>0 for t in trades)*100 if trades else None,'trade_log':trades}


def paper_ready(evaluations):
    return all((e:=evaluations[p])['classification']['brier'] is not None
               and e['classification']['brier']<e['classification']['baseline_brier']
               and all(e[s]['trades']>=30 and e[s]['unknown_trades']==0 and e[s]['return_pct']>0 for s in ('base','double_cost'))
               for p in ('test','additional'))
