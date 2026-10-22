"""Portable, read-only evidence for the separate channel experiment."""
import json
from math import isfinite
from pathlib import Path
from django.conf import settings
from .technical_context import VERSION
from .neural_service import load_model
from .management.commands.train_market_ai import digest
from .management.commands.study_technical_channels import source_hashes,RULES,PERIODS,VARIANTS


def load_report():
    unavailable={'status':'unavailable','entry_allowed':False,'message':'Историческая проверка коридоров ещё не завершена или отчёт не прошёл проверку.'}
    try:
        root=(Path(settings.BASE_DIR)/'dashboard'/'ai_models').resolve()
        paths=sorted(root.glob('technical-channels-study-v1-*.json'),reverse=True)
        if not paths:
            return unavailable
        path=paths[0]
        if path.resolve().parent!=root or path.stat().st_size>8_000_000:
            return unavailable
        report=json.loads(path.read_text(encoding='utf-8'))
        model=load_model()
        if (report['artifact_sha256']!=digest({k:v for k,v in report.items() if k!='artifact_sha256'})
                or report['source_sha256']!=source_hashes() or report['version']!=VERSION
                or report['rules']!=RULES or report['periods']!=json.loads(json.dumps(PERIODS))
                or report['entry_allowed'] is not False or report['independent_prospective'] is not False
                or model is None or report['neural_model_id']!=model['artifact_sha256']):
            return unavailable
        results={}
        for p in PERIODS:
            results[p]={}
            for key in VARIANTS:
                result={}
                for cost in ('base','double_cost'):
                    item=report['results'][p][key][cost]
                    if (type(item['trades']) is not int or item['trades']<0
                            or type(item['unknown_trades']) is not int or item['unknown_trades']<0
                            or (item['return_pct'] is not None and (type(item['return_pct']) not in (int,float) or not isfinite(item['return_pct'])))):
                        return unavailable
                    result[cost]={k:item[k] for k in ('trades','unknown_trades','max_realised_drawdown_pct')}
                    result[cost]['return_pct']=item['return_pct'] if item['trades'] and not item['unknown_trades'] else None
                results[p][key]=result
        return {'status':'ok','entry_allowed':False,'at':report['at'],'variants':VARIANTS,'results':results,
                'diagnostics':{p:report['diagnostics'][p]['all'] for p in PERIODS},
                'note':'Проверена только свечная часть двух фиксированных правил: пробой, растущая средняя и объём ≥1,3 обычного. Общий условный счёт 100 000 ₽; стоп, удержание до часа, расходы с обеих сторон. Даты уже изучались. Прошлый стакан не входит в этот тест: пользу совместного подтверждения он не доказывает.'}
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):
        return unavailable
