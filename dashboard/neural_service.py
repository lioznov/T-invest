"""Validated neural artifact and a compact public research report."""
import json
import threading
from datetime import datetime
from pathlib import Path
from django.conf import settings
from . import neural_trader as nn, portfolio_replay
from .management.commands.train_neural_trader import source_hashes, paper_ready
from .management.commands.train_market_ai import digest

_lock = threading.RLock()
_cache = {}


def load_model():
    root = (Path(settings.BASE_DIR)/'dashboard'/'ai_models').resolve()
    try:
        paths = sorted(root.glob('neural-trader-v1-*.json'),reverse=True)
        if not paths:
            return None
        path = paths[0]
        stat = path.stat()
        if path.resolve().parent!=root or stat.st_size>4_000_000:
            return None
        hashes = source_hashes()
        key = (str(path),stat.st_mtime_ns,stat.st_size,digest(hashes))
        with _lock:
            if _cache.get('key')==key:
                return _cache['model']
            artifact = json.loads(path.read_text(encoding='utf-8'))
            if (artifact['artifact_sha256']!=digest({k:v for k,v in artifact.items() if k!='artifact_sha256'})
                    or artifact['version']!=nn.VERSION or artifact['source_sha256']!=hashes
                    or artifact['features']!=list(nn.FEATURES) or artifact['rules']!=nn.RULES
                    or artifact['portfolio_rules']!=portfolio_replay.RULES or artifact['entry_allowed'] is not False
                    or type(artifact['paper_ready']) is not bool or artifact['paper_ready']!=paper_ready(artifact['evaluations'])
                    or not nn.valid_model(artifact['model']) or datetime.fromisoformat(artifact['trained_at']).tzinfo is None):
                return None
            _cache.update(key=key,model=artifact)
            return artifact
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):
        return None


def model_status():
    artifact = load_model()
    if artifact is None:
        return {'status':'unavailable','entry_allowed':False,'message':'Нейросеть ещё не обучена или файл модели не прошёл проверку.'}
    def compact(result):
        return {key:value for key,value in result.items() if key not in ('trade_log','daily_pnl')}
    return {'status':'ok','version':artifact['version'],'trained_at':artifact['trained_at'],
            'trained_rows':artifact['trained_rows'],'calibration_rows':artifact['calibration_rows'],
            'features_count':len(nn.FEATURES),'layers':[20,16,8,2],'tickers':artifact['tickers'],
            'paper_ready':artifact['paper_ready'],'entry_allowed':False,'later_check':check_status(artifact),
            'tests':{p:{'classification':e['classification'],'candidates':e['candidates'],
                        'expected_return_mae_pct':e['expected_return_mae_pct'],
                        **{k:compact(e[k]) for k in ('base','double_cost')},
                        'comparison':{name:{k:compact(r[k]) for k in ('base','double_cost')}
                                      for name,r in artifact['comparison'][p].items()}}
                     for p,e in ((p,artifact['evaluations'][p]) for p in ('test','additional'))},
            'capital':portfolio_replay.RULES['capital'],'probability_threshold':nn.RULES['candidate_threshold'],
            'minimum_expected_net_pct':nn.RULES['minimum_expected_net_pct'],
            'message':'Нейросеть прошла исторический порог для отдельной бумажной проверки.' if artifact['paper_ready'] else
                      'Нейросеть обучена. Прибыльность по заданным условиям пока не подтверждена.',
            'note':'Общий условный счёт 100 000 ₽ на семь акций. Только покупки; комиссия и проскальзывание по 0,05% за сторону. Просадка по закрытым сделкам может быть меньше реальной. Даты уже изучались: нужна новая будущая проверка. Стакан проверяется при текущей оценке; его истории, новостей и личных сделок трейдеров в обучении нет.'}


def check_status(model):
    from .management.commands.verify_neural_trader import source_hashes as check_hashes, START, END
    unavailable = {'status':'unavailable','message':'Проверка более свежих дат ещё недоступна.'}
    root = (Path(settings.BASE_DIR)/'dashboard'/'ai_models').resolve()
    try:
        paths = sorted(root.glob('neural-check-v1-*.json'),reverse=True)
        if not paths:
            return unavailable
        path = paths[0]
        if path.resolve().parent!=root or path.stat().st_size>4_000_000:
            return unavailable
        report = json.loads(path.read_text(encoding='utf-8'))
        if (report['artifact_sha256']!=digest({k:v for k,v in report.items() if k!='artifact_sha256'})
                or report['model_id']!=model['artifact_sha256'] or report['source_sha256']!=check_hashes()
                or report['version']!=nn.VERSION or report['entry_allowed'] is not False
                or report['independent_prospective'] is not False
                or report['start']!=START.isoformat() or report['end_exclusive']!=END.isoformat()
                or set(report['inputs'])!=set(model['tickers'])):
            return unavailable
        result = report['result']
        return {'status':'ok','at':report['at'],'label':'13–16 октября','note':'Новый исторический участок; веса и условия входа сохранены. Это ещё не будущая бумажная торговля.',
                'result':{k:({a:b for a,b in v.items() if a not in ('trade_log','daily_pnl')} if k in ('base','double_cost') else v)
                          for k,v in result.items()}}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return unavailable


def analyze_snapshot(snapshot,mode='live',now=None):
    from .ai_service import analyze_snapshot as guarded_analysis
    return guarded_analysis(snapshot,mode,now,model=load_model(),engine=nn)
