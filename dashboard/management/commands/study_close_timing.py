"""Freeze three clocks, evaluate all on cached exchange data; no networking."""
import json
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import morning_model as m
from dashboard import close_timing_study as study
from dashboard.morning_inputs import load_history
from .train_morning_forecast import immutable


def freeze(directory):
    spec = json.loads(json.dumps({'version':study.VERSION,'variants':study.VARIANTS,'rules':study.RULES,
        'features':m.FEATURES,'periods':m.PERIODS,'tickers':m.TICKERS,'source_sha256':study.source_hashes()}))
    path = directory/'close-timing-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != spec:
            raise CommandError('Зафиксированное сравнение часов изменено; зарегистрируй новую версию, старую не перезаписывай.')
    else:
        immutable(path,{'registered_at':datetime.now(timezone.utc).isoformat(),'spec':spec,'entry_allowed':False})
    return study.digest(spec)


def report(rows, model, train_count, cal_count):
    groups = m.split(rows)
    return {'trained_rows':train_count,'calibration_rows':cal_count,
            'periods':{name:{**m.evaluate(groups[name],model),'start':m.PERIODS[name][0],
                            'end_exclusive':m.PERIODS[name][1]} for name in ('test','later')},
            'note':study.NOTE}


class Command(BaseCommand):
    help = 'Compare17:45/18:00/22:45 fixed next-morning forecasts; cached history only, no orders, downloads or Telegram.'

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/'data'; directory.mkdir(parents=True,exist_ok=True)
        protocol = freeze(directory)  # Specification registered before price inspection.
        rows = {v['id']:[] for v in study.VARIANTS}; coverage,sources = {},{}
        for ticker in m.TICKERS:
            history = load_history(directory,ticker)
            examples,coverage[ticker] = study.build_examples(history['candles'],ticker,history['instrument']['lot'])
            for key,values in examples.items(): rows[key].extend(values)
            sources[ticker] = history['sources']
        common = study.paired(rows)
        variants = []
        for variant in study.VARIANTS:
            key = variant['id']; groups = m.split(rows[key])
            model = m.fit(groups['train'],groups['calibration'])
            train_count = sum(r['y'] is not None for r in groups['train'])
            cal_count = sum(r['y'] is not None for r in groups['calibration'])
            full = report(rows[key],model,train_count,cal_count)
            paired = report(common[key],model,train_count,cal_count)
            variants.append({**variant,'model':model,'report':full,'paired_report':paired})
            def value(number): return 'не определён' if number is None else f'{number:.3f}'
            for name,period in full['periods'].items():
                paper=period['paper']
                self.stdout.write(f'{variant["cutoff"]} {name}: N={period["samples"]}, '
                    f'MAE={value(period["mae_bps"])}б.п., naive={value(period["baseline_mae_bps"])}б.п., '
                    f'направление={value(period["direction_accuracy_pct"])}%, '
                    f'LONG={paper["trades"]}, счёт={value(paper["net_return_pct"])}%, '
                    f'стресс={value(paper["stress_return_pct"])}%, неизвестных={paper["unknown_trades"]}.')
        artifact = {'version':study.VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
                    'entry_allowed':False,'protocol_sha256':protocol,'source_sha256':study.source_hashes(),
                    'note':study.NOTE,'paired_note':study.PAIRED_NOTE,'rules':study.RULES,
                    'sources':sources,'coverage':coverage,'variants':variants}
        artifact['artifact_sha256'] = study.digest(artifact)
        output = Path(settings.BASE_DIR)/'dashboard'/'ai_models'; output.mkdir(parents=True,exist_ok=True)
        path = output/f'close-timing-v1-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")}.json'
        immutable(path,artifact)
        self.stdout.write(f'Сохранено {path.name}. Все3 времени показаны; модель не заменена и торговля не включена.')
