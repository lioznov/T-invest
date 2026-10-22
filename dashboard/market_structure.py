"""Causal confirmed pivots and chart annotations, not a trained predictor."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from .signals import features, MSK
from .method_study import validate_candles

VERSION = 'market_structure_v1'
RULES = {'previous_trials':100,'new_trials':3,'pivot_left':2,'pivot_right':2,'level_lifetime_bars':96,
         'break_buffer_atr':.1,'volume_breakout':1.5,'volume_bounce':1.2,'chart_cluster_atr':.2,
         'trend_filter':'close above EMA50 and EMA50 rising over three bars',
         'bounce':'low in support [-0.3,+0.15] prior ATR, green close >=support+0.3 ATR, previous close >=support+0.15 ATR',
         'pivot':'strict local high/low of five contiguous positive-volume same-MSK-day candles; available only after right two bars close',
         'events':'prior confirmed level, prior ATR, adjacent same-day candles; latest candle never creates its own prior level',
         'replay':'v1 next-open LONG, initial 1.5ATR/min0.3% stop, exit within four contiguous bars, unknown future retained',
         'costs':'v1 five bps fees and five bps slippage each side; normal and doubled costs',
         'entry_allowed':False,'limitations':'previously examined history; no trained book/news/trader identity; no real fills/taxes'}
VARIANTS = {'pivot_breakout':'Пробой подтверждённого максимума + тренд',
            'volume_breakout':'Пробой + тренд + повышенный объём',
            'support_bounce':'Отскок от подтверждённого минимума + тренд + объём'}


def scan(candles):
    validate_candles(candles)
    fs = features(candles)
    pivots, events, rows = [], [], []
    for i,c in enumerate(candles):
        active = [p for p in pivots if i-p['index']<=RULES['level_lifetime_bars']]
        prior = candles[i-1] if i else c
        atr = fs[i-1]['atr'] if i else 0.
        highs = [p for p in active if p['kind']=='high' and p['price']>=prior.close-.1*atr]
        lows = [p for p in active if p['kind']=='low' and p['price']<=prior.close+.15*atr]
        resistance = min(highs,key=lambda p:p['price']) if highs else None
        support = max(lows,key=lambda p:p['price']) if lows else None
        local = c.time.astimezone(MSK)
        adjacent = (i>0 and c.time-prior.time==timedelta(minutes=15)
                    and local.date()==prior.time.astimezone(MSK).date() and c.volume>0 and prior.volume>0)
        up = bool(adjacent and atr>0 and resistance and c.close>resistance['price']+.1*atr
                  and prior.close<=resistance['price']+.1*atr)
        down = bool(adjacent and atr>0 and support and c.close<support['price']-.1*atr
                    and prior.close>=support['price']-.1*atr)
        bounce = bool(adjacent and atr>0 and support and c.close>c.open
                      and support['price']-.3*atr<=c.low<=support['price']+.15*atr
                      and c.close>=support['price']+.3*atr and prior.close>=support['price']+.15*atr)
        trend = c.close>fs[i]['ema'] and fs[i]['rising']
        rows.append({'support':support,'resistance':resistance,'up':up,'down':down,'bounce':bounce,
                     'trend':trend,'rvol':fs[i]['rvol'],'atr':atr})
        for key,detected,level in (('breakout',up,resistance),('breakdown',down,support),('bounce',bounce,support)):
            if detected:
                events.append({'key':key,'time':c.time.isoformat(),'detected_at':(c.time+timedelta(minutes=15)).isoformat(),
                    'level':level['price'],'level_known_at':level['confirmed_at'],'price':c.close,
                    'rvol':fs[i]['rvol'],'volume_confirmed':fs[i]['rvol']>=RULES['volume_breakout'],
                    'trend_confirmed':bool(trend),'entry_allowed':False})
        # New pivot is registered AFTER the event calculation on its confirmation candle.
        if i>=4:
            block=candles[i-4:i+1]
            center=block[2]
            good=(all(b.volume>0 and b.time.astimezone(MSK).date()==local.date() for b in block)
                  and all(b.time-a.time==timedelta(minutes=15) for a,b in zip(block,block[1:])))
            if good:
                others=block[:2]+block[3:]
                for kind,price,extreme in (('high',center.high,all(center.high>b.high for b in others)),
                                           ('low',center.low,all(center.low<b.low for b in others))):
                    if extreme:
                        pivots.append({'kind':kind,'price':price,'index':i-2,'confirmed_index':i,
                            'time':center.time.isoformat(),'confirmed_at':(c.time+timedelta(minutes=15)).isoformat()})
    return {'rows':rows,'pivots':pivots,'events':events,'features':fs}


def flags(row):
    return {'pivot_breakout':row['up'] and row['trend'],
            'volume_breakout':row['up'] and row['trend'] and row['rvol']>=RULES['volume_breakout'],
            'support_bounce':row['bounce'] and row['trend'] and row['rvol']>=RULES['volume_bounce']}


def chart_panel(snapshot,mode='live',now=None):
    now = now or datetime.now(timezone.utc)
    base = {'version':VERSION,'entry_allowed':False,'timeframe':'15m','status':'insufficient',
            'source_confirmed':False,'pivots':[],'levels':[],'trend_lines':[],'events':[],'latest_event':None,
            'message':'Нужны закрытые свечи для разметки графика.','evaluated_at':None,'valid_until':None}
    try:
        bars=[c for c in snapshot.get('candles',[]) if c.time+timedelta(minutes=15)<=now]
        result=scan(bars)
        if len(bars)<21:
            return base
        latest=bars[-1]
        closed=latest.time+timedelta(minutes=15)
        fresh=timedelta(0)<=now-closed<=timedelta(minutes=17)
        source = mode=='live' and snapshot.get('provider_version')=='stream_exchange_v1'
        active=[p for p in result['pivots'] if len(bars)-1-p['index']<=RULES['level_lifetime_bars']]
        atr=result['features'][-1]['atr']
        levels,lines=[],[]
        for kind,label in (('low','support'),('high','resistance')):
            subset=[p for p in active if p['kind']==kind]
            selected=[]
            for p in sorted(subset,key=lambda p:(abs(p['price']-latest.close),-p['confirmed_index'])):
                if all(abs(p['price']-q['price'])>.2*atr for q in selected):
                    selected.append(p)
                if len(selected)==3:
                    break
            for p in selected:
                levels.append({'kind':label,'price':p['price'],'known_at':p['confirmed_at'],
                    'pivot_at':p['time'],'touches':sum(abs(q['price']-p['price'])<=.2*atr for q in subset),
                    'broken':latest.close<p['price']-.1*atr if label=='support' else latest.close>p['price']+.1*atr})
            if len(subset)>=2:
                a,b=subset[-2:]
                slope=(b['price']-a['price'])/(b['index']-a['index'])
                # Only rising lows or falling highs, shown from confirmation onward.
                if (kind=='low' and slope>0) or (kind=='high' and slope<0):
                    first=b['confirmed_index']
                    start=b['price']+slope*(first-b['index'])
                    end=b['price']+slope*(len(bars)-1-b['index'])
                    if all(isfinite(v) and v>0 for v in (start,end)):
                        lines.append({'kind':label,'start_time':bars[first].time.isoformat(),'known_at':b['confirmed_at'],
                            'start_price':start,'end_time':latest.time.isoformat(),'end_price':end,
                            'anchors':[a,b],'broken':latest.close<end-.1*atr if kind=='low' else latest.close>end+.1*atr})
        row=result['rows'][-1]
        state='Закрытие выше прежнего сопротивления' if row['up'] else 'Закрытие ниже прежней поддержки' if row['down'] else 'Отскок от поддержки' if row['bounce'] else 'Свежего пробоя или отскока нет'
        latest_events=[e for e in result['events'] if e['time']==latest.time.isoformat()]
        return {**base,'status':'ok' if fresh else 'stale','source_confirmed':source,
            'pivots':[{k:v for k,v in p.items() if k not in ('index','confirmed_index')} for p in active[-24:]],
            'levels':levels,'trend_lines':lines,'events':result['events'][-40:],
            'latest_event':latest_events[-1] if latest_events and fresh else None,
            'evaluated_at':closed.isoformat(),'valid_until':(closed+timedelta(minutes=17)).isoformat(),
            'message':state if fresh else 'Историческая разметка: закрытая свеча устарела. Текущий пробой не подтверждается.',
            'note':'Максимум/минимум подтверждается спустя две закрытые свечи. Линии — расчётные ориентиры, не обещание отскока или сигнал покупки. Это отдельные правила разметки, не новые обученные веса ИИ.'}
    except (ValueError,TypeError,AttributeError,KeyError,IndexError,OverflowError):
        return {**base,'status':'invalid','message':'Не удалось проверить историю для разметки графика.'}
