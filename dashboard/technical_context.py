"""Causal price channels and a depth-aware comparison with observed orders.

The regression fit is known only after its fitting window closes. Its historical
segment is a fit, not a sequence of previously available trading signals.
"""
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import mean, median
from .method_study import validate_candles
from .signals import features, valid_book

VERSION = 'technical_context_v1'
RULES = {'range_bars':20,'regression_bars':60,'break_atr':.1,'near_atr':.2,
         'level_zone_atr':.15,'level_zone_fraction':.00025,'book_max_age_seconds':10,
         'large_order_ratio':3.,'minimum_depth_levels':5,
         'retention_samples':3,'retention_seconds':30,'retention_min_fraction':.65,
         'history':'only closed candles, channel limits from prior bars; no fitted historical trading labels',
         'book':'consistent EXCHANGE stream, open trading, full price-zone coverage for classification; quantities in lots',
         'entry_allowed':False}


def regression(candles, index):
    n = RULES['regression_bars']
    if index < n:
        return None
    history = candles[index-n:index]
    if any(c.volume<=0 for c in history):
        return None
    center = (n-1)/2
    slope = sum((j-center)*c.close for j,c in enumerate(history))/(n*(n*n-1)/12)
    intercept = mean(c.close for c in history)-slope*center
    width = max(max(abs(c.high-(intercept+slope*j)),abs(c.low-(intercept+slope*j))) for j,c in enumerate(history))
    values = {'slope':slope,'middle':intercept+slope*n,'lower':intercept+slope*n-width,
              'upper':intercept+slope*n+width,'width':width,'start_middle':intercept,
              'start_lower':intercept-width,'start_upper':intercept+width,
              'start_time':history[0].time.isoformat(),'end_time':candles[index].time.isoformat(),
              'known_at':(history[-1].time+timedelta(minutes=15)).isoformat()}
    return values if width>0 and min(values[k] for k in ('lower','start_lower'))>0 else None


def channel_rows(candles):
    validate_candles(candles)
    rows = []
    for i,c in enumerate(candles):
        prior = candles[max(0,i-RULES['range_bars']):i]
        band = None
        if len(prior)==RULES['range_bars'] and all(b.volume>0 for b in prior):
            lower, upper = min(b.low for b in prior), max(b.high for b in prior)
            band = {'time':c.time.isoformat(),'lower':lower,'upper':upper,'middle':(lower+upper)/2,
                    'known_at':(prior[-1].time+timedelta(minutes=15)).isoformat()}
        rows.append({'range':band,'regression':regression(candles,i)})
    return rows


def checked_book(book):
    try:
        if not valid_book(book) or book.get('is_consistent') is not True or book['time'].tzinfo is None:
            return False
        bids, asks = book['bids'], book['asks']
        return (all(type(q) is int and q>0 for p,q in bids+asks)
                and all(a[0]>b[0] for a,b in zip(bids,bids[1:]))
                and all(a[0]<b[0] for a,b in zip(asks,asks[1:])))
    except (KeyError,TypeError,ValueError,AttributeError,IndexError):
        return False


def level_volume(book,level,tolerance):
    side = book['bids'] if level['kind']=='support' else book['asks']
    price = level['price']
    floor, ceiling = price-tolerance, price+tolerance
    observed = [(p,q) for p,q in side if floor<=p<=ceiling]
    if level['kind']=='support':
        overlap = ceiling>=side[-1][0] and floor<=side[0][0]
        covered = floor>=side[-1][0] and price<=side[0][0]+tolerance
    else:
        overlap = floor<=side[-1][0] and ceiling>=side[0][0]
        covered = ceiling<=side[-1][0] and price>=side[0][0]-tolerance
    return {'coverage':'full' if covered else 'partial' if overlap else 'outside',
            'lots':sum(q for p,q in observed),'largest_lots':max((q for p,q in observed),default=0),
            'largest_price':max(observed,key=lambda r:r[1])[0] if observed else None,
            'median_lots':median(q for p,q in side) if len(side)>=RULES['minimum_depth_levels'] else None}


def compare_book(snapshot,levels,atr,mode,now,candle_until):
    base = {'status':'unavailable','time':None,'valid_until':None,'matches':[],
            'message':'Для сверки нужен свежий согласованный биржевой стакан.'}
    books = snapshot.get('books',[])
    if mode=='demo':
        # The demo generator has no exchange consistency field. Its internally
        # ordered synthetic levels are never promoted to live evidence.
        books = [{**b,'is_consistent':b.get('is_consistent',True)} for b in books]
    if not books:
        return base
    book = books[-1]
    if not checked_book(book):
        return {**base,'message':'Стакан не согласован или уровни заявок некорректны. Сверка отложена.'}
    age = (now-book['time']).total_seconds()
    if not 0<=age<=RULES['book_max_age_seconds']:
        return {**base,'status':'stale','message':'Стакан устарел. Уровни графика остаются историческими.'}
    if now>candle_until:
        return {**base,'status':'stale','message':'Свечи устарели. Совместная текущая оценка отложена.'}
    if mode!='demo' and (snapshot.get('provider_version')!='stream_exchange_v1'
            or snapshot.get('data_quality',{}).get('action_blocked') is not False or not snapshot.get('trading')):
        return {**base,'message':'Источник EXCHANGE, полнота потока или открытые торги не подтверждены.'}
    matches = []
    for level in levels:
        tolerance = max(atr*RULES['level_zone_atr'],level['price']*RULES['level_zone_fraction'])
        current = level_volume(book,level,tolerance)
        ratio = current['largest_lots']/current['median_lots'] if current['median_lots'] else None
        previous = []
        known = datetime.fromisoformat(level['known_at'])
        for prior in books:
            if not checked_book(prior) or not known<=prior['time']<=book['time'] or not 0<=(book['time']-prior['time']).total_seconds()<=90:
                continue
            seen = level_volume(prior,level,tolerance)
            if seen['coverage']=='full':
                previous.append((prior['time'],seen['lots']))
        previous = sorted(dict(previous).items())
        retained = None
        if (current['coverage']=='full' and len(previous)>=RULES['retention_samples']
                and (previous[-1][0]-previous[0][0]).total_seconds()>=RULES['retention_seconds']):
            peak = max(q for _,q in previous)
            retained = current['lots']/peak if peak>0 else None
        dropped = retained is not None and retained<RULES['retention_min_fraction']
        label = ('За видимой глубиной стакана' if current['coverage']=='outside' else
                 'Зона видна только частично' if current['coverage']=='partial' else
                 'Объём возле уровня снизился' if dropped else
                 'Есть заметная заявка возле уровня' if ratio is not None and ratio>=RULES['large_order_ratio'] else
                 'Недостаточно уровней для сравнения объёмов' if ratio is None else
                 'Повышенного объёма возле уровня нет')
        lot = snapshot.get('instrument',{}).get('lot')
        shares = current['lots']*lot if type(lot) is int and lot>0 else None
        matches.append({**level,**current,'tolerance':tolerance,'shares':shares,'large_order_ratio':ratio,
                        'retention_fraction':retained,'observations':len(previous),
                        'state':'outside' if current['coverage']=='outside' else 'partial' if current['coverage']=='partial'
                                else 'reduced' if dropped else 'visible' if ratio is not None and ratio>=RULES['large_order_ratio'] else 'ordinary',
                        'label':label,'entry_allowed':False})
    return {**base,'status':'demo' if mode=='demo' else 'ok','time':book['time'].isoformat(),
            'valid_until':min(book['time']+timedelta(seconds=RULES['book_max_age_seconds']),candle_until).isoformat(),
            'matches':matches,'message':'Учебная сверка: заявки и цены вымышлены.' if mode=='demo' else
                      'Текущие объёмы сопоставлены с уровнями графика. Заявку могут снять или исполнить.'}


def panel(snapshot,structure=None,mode='live',now=None):
    now = now or datetime.now(timezone.utc)
    base = {'version':VERSION,'status':'insufficient','entry_allowed':False,'source_confirmed':False,
            'mode':mode,'evaluated_at':None,'valid_until':None,'range_points':[],'regression':None,
            'levels':[],'book':{'status':'unavailable','matches':[]},'state':None,
            'message':'Для коридоров нужны минимум 61 закрытая свеча.'}
    try:
        bars = [c for c in snapshot.get('candles',[]) if c.time+timedelta(minutes=15)<=now]
        if len(bars)<61:
            return base
        history = channel_rows(bars)
        band, reg = history[-1]['range'], history[-1]['regression']
        if band is None or reg is None or bars[-1].volume<=0:
            return {**base,'message':'Для коридоров нужны свечи с подтверждённым положительным объёмом.'}
        fs = features(bars)
        atr = fs[-2]['atr']
        c = bars[-1]
        until = c.time+timedelta(minutes=32)
        source = mode=='live' and snapshot.get('provider_version')=='stream_exchange_v1'
        direction = 'up' if reg['slope']*60>atr else 'down' if reg['slope']*60<-atr else 'flat'
        state = 'above' if c.close>band['upper']+.1*atr else 'below' if c.close<band['lower']-.1*atr else 'near_support' if c.close<=band['lower']+.2*atr else 'near_resistance' if c.close>=band['upper']-.2*atr else 'inside'
        names = {'above':'Закрытие вышло выше коридора 20 свечей','below':'Закрытие вышло ниже коридора 20 свечей',
                 'near_support':'Цена около нижней границы коридора','near_resistance':'Цена около верхней границы коридора','inside':'Цена внутри коридора 20 свечей'}
        levels = [{'key':'range_support','kind':'resistance' if state=='below' else 'support','price':band['lower'],
                   'known_at':band['known_at'],'name':'Нижняя граница 20'+(' · пройдена' if state=='below' else '')},
                  {'key':'range_resistance','kind':'support' if state=='above' else 'resistance','price':band['upper'],
                   'known_at':band['known_at'],'name':'Верхняя граница 20'+(' · пройдена' if state=='above' else '')}]
        if structure and structure.get('version')=='market_structure_v1' and structure.get('entry_allowed') is False:
            for kind in ('support','resistance'):
                choices = [l for l in structure.get('levels',[]) if l.get('kind')==kind and not l.get('broken')
                           and isfinite(l.get('price',float('nan'))) and l['price']>0
                           and (l['price']<=c.close if kind=='support' else l['price']>=c.close)]
                if choices:
                    l = min(choices,key=lambda l:abs(l['price']-c.close))
                    if all(abs(l['price']-a['price'])>.1*atr for a in levels):
                        levels.append({'key':'pivot_'+kind,'kind':kind,'price':l['price'],'known_at':l['known_at'],
                                       'name':'Подтверждённая поддержка' if kind=='support' else 'Подтверждённое сопротивление'})
        current = now<=until
        return {**base,'status':'ok' if current else 'stale','source_confirmed':source,'levels':levels,
                'range_points':[r['range'] for r in history[-90:] if r['range'] is not None],
                'regression':reg,'direction':direction,'state':state if current else None,
                'evaluated_at':(c.time+timedelta(minutes=15)).isoformat(),'valid_until':until.isoformat(),
                'message':names[state] if current else 'Исторические коридоры: свежая закрытая свеча ещё не получена.',
                'book':compare_book(snapshot,levels,atr,mode,now,until),
                'note':'Ступенчатый коридор — максимум/минимум предыдущих 20 свечей. Наклонный — прямая по 60 предыдущим закрытиям и параллельные границы по наибольшему отклонению. Историческая часть наклонного коридора — подгонка прямой по этому окну, а не прежние сигналы. Объём стакана не гарантирует отскок; по снимкам снятие и исполнение не различить.'}
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,OverflowError):
        return {**base,'status':'invalid','message':'Не удалось проверить данные для коридоров и стакана.'}


def expire_payload(payload,now=None):
    now = now or datetime.now(timezone.utc)
    a = payload.get('analysis',{})
    p = a.get('technical')
    if not p or p.get('version')!=VERSION:
        return payload
    updated = p
    try:
        if p.get('status')=='ok' and now>datetime.fromisoformat(p['valid_until']):
            updated = {**updated,'status':'stale','state':None,'message':'Исторические коридоры: текущие данные устарели.'}
        book = updated.get('book',{})
        if book.get('status') in ('ok','demo') and now>datetime.fromisoformat(book['valid_until']):
            updated = {**updated,'book':{**book,'status':'stale','matches':[],
                                      'message':'Сверка стакана устарела. Ждём новые заявки.'}}
    except (ValueError,TypeError,KeyError):
        updated = {**updated,'status':'stale','state':None,'book':{'status':'unavailable','matches':[],'message':'Время сверки не подтверждено.'}}
    return {**payload,'analysis':{**a,'technical':updated}} if updated is not p else payload
