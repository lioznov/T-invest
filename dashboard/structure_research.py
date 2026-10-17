"""Read-only portable results for deterministic structure, separate from ML."""
import json
from datetime import datetime, timezone
from pathlib import Path
from math import isfinite
from django.conf import settings
from . import market_structure as structure, market_ai as ai
from .management.commands.study_structure import source_hashes
from .management.commands.train_market_ai import digest


def load_structure_research():
    base={'status':'unavailable','entry_allowed':False,'variants':[],
          'message':'Проверка уровней на истории ещё не выполнена или сохранённый отчёт не прошёл проверку.'}
    try:
        root=(Path(settings.BASE_DIR)/'dashboard'/'ai_models').resolve()
        paths=sorted(root.glob('market-structure-study-v1-*.json'),reverse=True)
        if not paths:
            return base
        path=paths[0]
        if path.resolve().parent!=root or path.stat().st_size>8_000_000:
            return base
        saved=json.loads(path.read_text(encoding='utf-8'))
        if (saved['artifact_sha256']!=digest({k:v for k,v in saved.items() if k!='artifact_sha256'})
                or saved['version']!=structure.VERSION or saved['source_sha256']!=source_hashes()
                or saved['rules']!=json.loads(json.dumps(structure.RULES))
                or saved['entry_allowed'] is not False or saved['periods']!=json.loads(json.dumps(ai.PERIODS))):
            return base
        variants=[]
        for key,label in structure.VARIANTS.items():
            periods={}
            for p in ai.PERIODS:
                result=saved['results'][key][p]
                public={}
                for cost in ('base','double_cost'):
                    item=result[cost]
                    if (type(item['trades']) is not int or item['trades']<0
                        or type(item['unknown_trades']) is not int or item['unknown_trades']<0
                        or type(item['accounts']) is not int or item['accounts']!=7
                        or type(item['positive_accounts']) is not int or not 0<=item['positive_accounts']<=7
                        or type(item['return_pct']) not in (int,float) or not isfinite(item['return_pct'])):
                        return base
                    public[cost]={k:item[k] for k in ('trades','unknown_trades','accounts','positive_accounts')}
                    public[cost]['return_pct']=item['return_pct'] if item['trades'] and not item['unknown_trades'] else None
                periods[p]=public
            variants.append({'key':key,'name':label,'periods':periods})
        return {**base,'status':'ok','version':structure.VERSION,'at':saved['at'],'variants':variants,
            'message':'Три отдельные проверки уровней. Это правила разметки и входа, не переобучение ИИ.',
            'note':'Итоги — среднее семи отдельных условных счетов по 100 000 ₽. Следующее открытие, стоп и выход в пределах часа; расходы с обеих сторон. Даты уже изучались ранее. История стакана не моделировалась. Правила не разрешают покупку.'}
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError,RecursionError):
        return base


def expire_structure_payload(payload,now=None):
    now=now or datetime.now(timezone.utc)
    analysis=payload.get('analysis',{})
    current=analysis.get('market_structure')
    if not current or current.get('status')!='ok':
        return payload
    try:
        fresh=now<=datetime.fromisoformat(current['valid_until'])
    except (ValueError,TypeError,KeyError):
        fresh=False
    if fresh:
        return payload
    return {**payload,'analysis':{**analysis,'market_structure':{**current,'status':'stale',
        'entry_allowed':False,'latest_event':None,'message':'Историческая разметка: данные устарели. Текущий пробой не подтверждается.'}}}
