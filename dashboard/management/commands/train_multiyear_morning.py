"""Train four fixed experiments from cached multi-year inputs. No network."""
import json
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import morning_model as m
from dashboard import multiyear_model as study
from dashboard import multiyear_daily as daily
from dashboard.multiyear_inputs import load_history
from dashboard.close_timing_study import VARIANTS
from .train_morning_forecast import immutable


def freeze(directory):
    spec=json.loads(json.dumps({'version':study.VERSION,'rules':study.RULES,'periods':study.PERIODS,
        'intraday_features':m.FEATURES,'daily_features':daily.FEATURES,'source_sha256':study.source_hashes()}))
    path=directory/'multiyear-morning-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec']!=spec:
            raise CommandError('Многолетний протокол изменён. Старые результаты не перезаписываются; нужна новая версия.')
    else:
        immutable(path,{'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec,'entry_allowed':False})
    return study.digest(spec)


def make_report(rows, evaluator):
    groups=study.split(rows)
    model=m.fit(groups['train'],groups['calibration'])
    report={'trained_rows':sum(r['y'] is not None for r in groups['train']),
            'calibration_rows':sum(r['y'] is not None for r in groups['calibration']),
            'periods':{name:{**evaluator(groups[name],model),'start':study.PERIODS[name][0],
                            'end_exclusive':study.PERIODS[name][1]} for name in ('test','later')},
            'note':study.NOTE}
    return model,report


class Command(BaseCommand):
    help='Train separate multiyear close/open and preclose forecasts from verified cached history; no orders or downloads.'

    def handle(self,*args,**options):
        root=Path(settings.BASE_DIR)/'data'; root.mkdir(parents=True,exist_ok=True)
        protocol=freeze(root)
        intraday={v['id']:[] for v in VARIANTS}; daily_rows=[]
        coverage,sources,feature_coverage={},{},{}
        for ticker in m.TICKERS:
            day=load_history(root/'multiyear',ticker,'day')
            intra=load_history(root/'multiyear',ticker,'15m')
            if any(day['instrument'][k]!=intra['instrument'][k] for k in ('uid','ticker','lot')):
                raise CommandError('Дневной и внутридневной инструменты не совпадают.')
            rows,feature_coverage[ticker]=study.build_intraday(intra['candles'],ticker,intra['instrument']['lot'])
            for key,values in rows.items(): intraday[key].extend(values)
            drows,dquality=daily.build_examples(day['candles'],ticker,day['instrument']['lot'])
            daily_rows.extend(drows); feature_coverage[ticker]['daily_full']=dquality
            coverage[ticker]={'day':day['coverage'],'intraday':intra['coverage']}
            sources[ticker]={'day':day['sources'],'intraday':intra['sources']}
            self.stdout.write(f'{ticker}: дневных свечей {len(day["candles"])},15минутных {len(intra["candles"])}.',ending='\n')
        variants=[]
        model,report=make_report(daily_rows,daily.evaluate)
        variants.append({'id':'daily_full','kind':'daily_observational','label':'Завершённый день → следующее дневное открытие',
                         'features':list(daily.FEATURES),'model':model,'report':report})
        for variant in VARIANTS:
            model,report=make_report(intraday[variant['id']],m.evaluate)
            variants.append({**variant,'kind':'intraday','features':list(m.FEATURES),'model':model,'report':report})
        artifact={'version':study.VERSION,'created_at':datetime.now(timezone.utc).isoformat(),'entry_allowed':False,
                  'source_sha256':study.source_hashes(),'protocol_sha256':protocol,'rules':study.RULES,
                  'coverage':coverage,'feature_coverage':feature_coverage,'sources':sources,'variants':variants,
                  'note':study.NOTE,'publication_note':'Старая модель не заменена. Это отдельный отчёт обучения, '
                  'не автоматически сохранённые вечерние прогнозы. Варианты по этим результатам не выбираются задним числом.'}
        artifact['artifact_sha256']=study.digest(artifact)
        output=Path(settings.BASE_DIR)/'dashboard'/'ai_models';output.mkdir(parents=True,exist_ok=True)
        path=output/f'multiyear-morning-v1-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")}.json'
        immutable(path,artifact)
        def value(x): return 'не определён' if x is None else f'{x:.3f}'
        for variant in variants:
            for name,p in variant['report']['periods'].items():
                self.stdout.write(f'{variant["id"]} {name}:N={p["samples"]},MAE={value(p["mae_bps"])}б.п., '
                    f'naive={value(p["baseline_mae_bps"])}б.п., направление={value(p["direction_accuracy_pct"])}%, '
                    f'сделок={p["paper"]["trades"]}, результат={value(p["paper"]["net_return_pct"])}%.')
        self.stdout.write(f'Сохранено {path.name}. Доходность не гарантирована, торговые заявки не включены.')
