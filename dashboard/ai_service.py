"""Local JSON model inference with explicit price/source and expiry guards."""
import json
import threading
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from django.conf import settings
from . import market_ai as ai
from . import market_ai_v2 as challenger
from .management.commands.train_market_ai import source_hashes, digest
from .management.commands.train_market_ai_v2 import source_hashes as challenger_hashes
from .signals import MSK, valid_book

_lock=threading.RLock()
_cache={}
_challenger_cache={}


def load_challenger():
    root=(Path(settings.BASE_DIR)/'dashboard'/'ai_models').resolve()
    try:
        paths=sorted(root.glob('market-ai-v2-*.json'),reverse=True)
        if not paths:
            return None
        path=paths[0]
        stat=path.stat()
        if path.resolve().parent!=root or stat.st_size>4_000_000:
            return None
        key=(str(path),stat.st_mtime_ns,stat.st_size,digest(challenger_hashes()))
        with _lock:
            if _challenger_cache.get('key')==key:
                return _challenger_cache['model']
            model=json.loads(path.read_text(encoding='utf-8'))
            if (model['artifact_sha256']!=digest({k:v for k,v in model.items() if k!='artifact_sha256'})
                    or model['version']!=challenger.VERSION or model['features']!=list(challenger.FEATURES)
                    or model['source_sha256']!=challenger_hashes()
                    or model['rules']!=json.loads(json.dumps(challenger.RULES)) or model['entry_allowed'] is not False
                    or type(model['observation_ready']) is not bool or type(model['paper_ready']) is not bool
                    or model['observation_ready']!=challenger.observation_ready(model['evaluations'],model['comparison_v1'])
                    or model['paper_ready']!=ai.paper_ready(model['evaluations'])):
                return None
            trained=datetime.fromisoformat(model['trained_at'])
            if trained.tzinfo is None:
                return None
            classifier=model['model']['classifier']
            n=len(challenger.FEATURES)
            finite=lambda v:type(v) in (int,float) and isfinite(v)
            if (not finite(classifier['bias']) or classifier['learning_rate']!=challenger.RULES['learning_rate']
                    or len(classifier['trees'])!=challenger.RULES['trees'] or len(classifier['median'])!=n
                    or not all(finite(v) for v in classifier['median'])):
                return None

            def valid_tree(tree,depth=0):
                if not isinstance(tree,dict):
                    return False
                if set(tree)=={'value'}:
                    return finite(tree['value']) and abs(tree['value'])<=challenger.RULES['leaf_clip']
                return (depth<challenger.RULES['depth'] and set(tree)=={'feature','threshold','left','right'}
                    and type(tree['feature']) is int and 0<=tree['feature']<n and finite(tree['threshold'])
                    and valid_tree(tree['left'],depth+1) and valid_tree(tree['right'],depth+1))

            cal=model['model']['calibrator']
            if (not all(valid_tree(t) for t in classifier['trees']) or len(cal['weights'])!=2
                    or len(cal['center'])!=1 or len(cal['scale'])!=1 or cal['scale'][0]<=0
                    or not all(finite(v) for field in ('weights','center','scale') for v in cal[field])):
                return None
            _challenger_cache.update(key=key,model=model)
            return model
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError,RecursionError):
        return None


def selected_model():
    candidate=load_challenger()
    return candidate if candidate and candidate['observation_ready'] else load_model()


def comparison_status():
    model=load_challenger()
    if model is None:
        return {'status':'unavailable','message':'Новая модель ещё не обучена или не прошла проверку файла.'}
    return {'status':'ok','version':model['version'],'trained_rows':model['trained_rows'],
        'calibration_rows':model['calibration_rows'],'trained_at':model['trained_at'],'features_count':len(challenger.FEATURES),
        'observation_ready':model['observation_ready'],'paper_ready':model['paper_ready'],'entry_allowed':False,
        'tests':{p:{'v2':{k:model['evaluations'][p][k] for k in ('classification','base','double_cost')},
                    'v1':{k:model['comparison_v1'][p][k] for k in ('classification','base','double_cost')}}
                 for p in ('test','additional')},
        'message':'Новая модель лучше различает исходы на обоих участках и подключена для наблюдения.' if model['observation_ready'] else
                  'Новая модель не доказала преимущество на обоих участках. Для текущих оценок сохранена первая версия.',
        'note':'80 деревьев решений, 20 признаков. Сравнение на одинаковых примерах; даты уже изучались ранее. Стакан не участвовал в обучении. Реальные покупки не разрешены.'}


def load_model():
    root=(Path(settings.BASE_DIR)/'dashboard'/'ai_models').resolve()
    try:
        paths=sorted(root.glob('market-ai-v1-*.json'),reverse=True)
        if not paths:
            return None
        path=paths[0]
        stat=path.stat()
        if path.resolve().parent!=root or stat.st_size>4_000_000:
            return None
        key=(str(path),stat.st_mtime_ns,stat.st_size,digest(source_hashes()))
        with _lock:
            if _cache.get('key')==key:
                return _cache['model']
            model=json.loads(path.read_text(encoding='utf-8'))
            content={k:v for k,v in model.items() if k!='artifact_sha256'}
            if (model['artifact_sha256']!=digest(content) or model['version']!=ai.VERSION
                    or model['features']!=list(ai.FEATURES) or model['source_sha256']!=source_hashes()
                    or model['rules']!=json.loads(json.dumps(ai.RULES)) or model['entry_allowed'] is not False):
                return None
            for name,n in (('classifier',len(ai.FEATURES)),('calibrator',1)):
                part=model['model'][name]
                if (len(part['weights'])!=n+1 or len(part['center'])!=n or len(part['scale'])!=n
                        or any(type(v) not in (int,float) or not isfinite(v) for field in ('weights','center','scale') for v in part[field])
                        or any(s<=0 for s in part['scale'])):
                    return None
            datetime.fromisoformat(model['trained_at']).astimezone(timezone.utc)
            _cache.update(key=key,model=model)
            return model
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):
        return None


def model_status():
    from .structure_research import load_structure_research
    model=selected_model()
    if model is None:
        return {'status':'unavailable','entry_allowed':False,'message':'ИИ ещё не обучен или сохранённая модель не прошла проверку.'}
    return {'status':'ok','version':model['version'],'trained_at':model['trained_at'], 'comparison':comparison_status(),
            'structure_research':load_structure_research(),
            'trained_rows':model['trained_rows'],'calibration_rows':model['calibration_rows'],
            'tickers':model['tickers'],'paper_ready':model['paper_ready'],'entry_allowed':False,
            'threshold_score':round(ai.RULES['candidate_threshold']*100),'prospective_start':model['prospective_start'],
            'tests':{p:{'classification':e['classification'],'base':e['base'],'double_cost':e['double_cost']}
                     for p,e in model['evaluations'].items() if p in ('test','additional')},
            'message':'Локальная модель обучена на истории Т-Банка. Оценка относится к условной покупке на час с защитным стопом.',
            'readiness_message':'Можно начинать отдельную бумажную проверку.' if model['paper_ready'] else
                                'Проверка доходности пока не пройдена. ИИ используется для наблюдения.',
            'note':'Оценка модели не гарантирует рост акции. Обучение: март–июль; калибровка: август; проверки: сентябрь и 3–12 октября 2026. Стакан проверяется отдельно, его история не участвовала в обучении.'}


def analyze_snapshot(snapshot,mode='live',now=None,*,model=None,engine=None):
    now=now or datetime.now(timezone.utc)
    base={'status':'waiting','decision':'WAIT','entry_allowed':False,'score':None,'plan':None,'drivers':[],
          'message':'Ждём свежую закрытую свечу и подтверждённый биржевой стакан.'}
    if mode!='live':
        return {**base,'status':'unavailable','message':'ИИ оценивает реальные биржевые данные. Учебные цены не используются.'}
    if engine is None:
        model=selected_model()
    if model is None:
        return {**base,'status':'unavailable','message':'Обученная ИИ-модель недоступна; прогноз не рассчитан.'}
    ticker=snapshot.get('instrument',{}).get('ticker')
    if ticker not in model['tickers']:
        return {**base,'message':'Эта акция не входила в обучение модели.'}
    try:
        age=(now-datetime.fromisoformat(model['trained_at'])).total_seconds()
        if not 0<=age<=30*86400:
            return {**base,'message':'Модель требует обновления обучения; текущая оценка не используется.'}
        candles=[c for c in snapshot.get('candles',[]) if c.time+timedelta(minutes=15)<=now]
        if len(candles)<101:
            return {**base,'message':'Для ИИ нужны минимум 101 закрытая 15-минутная свеча.'}
        closed=candles[-1].time+timedelta(minutes=15)
        closed_age=(now-closed).total_seconds()
        local=closed.astimezone(MSK)
        if not 0<=closed_age<=60:
            return {**base,'message':'Свеча закрылась больше минуты назад. Ждём следующего закрытия для оценки входа.'}
        if local.weekday()>=5 or not 630<=local.hour*60+local.minute<=1020:
            return {**base,'message':'Модель рассматривает входы по будням с 10:30 до 17:00 МСК.'}
        quality=snapshot.get('data_quality',{})
        book=snapshot.get('books',[])[-1] if snapshot.get('books') else {}
        if (snapshot.get('provider_version')!='stream_exchange_v1' or quality.get('action_blocked') is not False
                or not snapshot.get('trading') or not valid_book(book) or book.get('is_consistent') is not True):
            return {**base,'message':'Источник стакана, торговый статус или полнота потока не подтверждены. Оценка входа отложена.'}
        book_at=book['time']
        if not 0<=(now-book_at).total_seconds()<=5:
            return {**base,'message':'Стакан задержан; ждём свежих котировок.'}
        entry=book['asks'][0][0]
        spread=(entry-book['bids'][0][0])/entry
        engine=engine or (challenger if model['version']==challenger.VERSION else ai)
        xs,features=engine.vectors(candles)
        x=xs[-1]
        if x is None:
            return {**base,'message':'Не хватает проверенного контекста свечей для модели: нужны непрерывная сессия и история объёмов.'}
        if spread>.001 or abs(entry-candles[-1].close)>features[-1]['atr']:
            return {**base,'message':'Спред, скачок цены или признаки свечи не подходят для модели.'}
        distance=max(1.5*features[-1]['atr'],entry*.003)
        if distance>=entry:
            return {**base,'message':'Колебания слишком велики для корректного расчёта стопа.'}
        probability=engine.predict(model['model'],x)
        if hasattr(engine,'explanations'):
            drivers=engine.explanations(model['model'],x)
        else:
            c=model['model']['classifier']
            cal=model['model']['calibrator']
            slope=cal['weights'][1]/cal['scale'][0]
            contributions=[w*max(-8,min(8,(v-center)/scale))*slope for w,v,center,scale in zip(c['weights'][1:],x,c['center'],c['scale'])]
            indices=sorted(range(len(contributions)),key=lambda i:-abs(contributions[i]))[:3]
            drivers=[{'name':ai.NAMES[i],'direction':'up' if contributions[i]>0 else 'down','value':round(x[i],4)} for i in indices]
        candidate=engine.candidate(model['model'],x) if hasattr(engine,'candidate') else probability>=ai.RULES['candidate_threshold']
        extra={'expected_net_pct':engine.estimate(model['model'],x)['expected_net_pct']} if hasattr(engine,'estimate') else {}
        expires=min(closed+timedelta(seconds=60),book_at+timedelta(seconds=5),now+timedelta(seconds=25))
        return {**base,**extra,'status':'ok','score':round(probability*100),'decision':'WATCH' if candidate else 'WAIT',
                'model_id':model['artifact_sha256'][:16],'model_version':model['version'],'drivers':drivers,'valid_until':expires.isoformat(),
                'explanation_note':'Чувствительность оценки при замене признака обычным значением; не причинное объяснение.' if hasattr(engine,'explanations') else 'Вклад признаков линейной модели; не причинное объяснение.',
                'candle_closed_at':closed.isoformat(),'paper_ready':model['paper_ready'],
                'message':'Модель выделила ситуацию для наблюдения. Покупка ещё не подтверждена будущими тестами.' if candidate else
                          'Модель не видит достаточно сильного основания для входа на ближайший час.',
                'plan':{'entry':entry,'stop':round(entry-distance,9),
                        'exit_by':(now+timedelta(hours=1)).isoformat(),'kind':'research_only'} if candidate else None}
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        return {**base,'message':'Не удалось проверить данные для ИИ. Оценка входа отложена.'}


def expire_payload(payload,now=None):
    now=now or datetime.now(timezone.utc)
    analysis=payload.get('analysis',{})
    changed={}
    for key in ('ai','neural'):
        current=analysis.get(key)
        if not current or current.get('status')!='ok':
            continue
        try:
            fresh=now<=datetime.fromisoformat(current['valid_until'])
        except (ValueError,TypeError,KeyError):
            fresh=False
        if not fresh:
            changed[key]={**current,'status':'waiting','decision':'WAIT','score':None,'expected_net_pct':None,
                'plan':None,'drivers':[],'entry_allowed':False,'message':'Оценка ИИ устарела. Ждём новую свечу и котировки.'}
    return {**payload,'analysis':{**analysis,**changed}} if changed else payload
