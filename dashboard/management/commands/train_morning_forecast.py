"""Freeze one model specification, then replay cached data; no broker or bot calls."""
import json
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import morning_model as m
from dashboard.morning_inputs import load_history


def immutable(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)


def freeze(directory):
    spec = json.loads(json.dumps({'version':m.VERSION, 'rules':m.RULES, 'features':m.FEATURES,
        'periods':m.PERIODS, 'tickers':m.TICKERS, 'source_sha256':m.source_hashes()}))
    path = directory/'morning-ridge-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != spec:
            raise CommandError('Зафиксированный протокол изменён. Сохрани старую версию и зарегистрируй новую.')
    else:
        immutable(path, {'registered_at':datetime.now(timezone.utc).isoformat(), 'spec':spec,
                         'entry_allowed':False, 'prospective_note':'No future predictions collected yet.'})
    return m.digest(spec)


class Command(BaseCommand):
    help = 'Train one fixed next-morning Ridge on cached EXCHANGE candles; no orders, bot, downloads or parameter search.'

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/'data'; directory.mkdir(parents=True,exist_ok=True)
        protocol = freeze(directory)
        rows, sources, coverage, instruments = [], {}, {}, {}
        for ticker in m.TICKERS:
            history = load_history(directory, ticker)
            examples, quality = m.build_examples(history['candles'], ticker, history['instrument']['lot'])
            rows.extend(examples); coverage[ticker] = quality; sources[ticker] = history['sources']
            instruments[ticker] = history['instrument']
            self.stdout.write(f'{ticker}: {len(examples)} вечерних примеров; отсутствующих целей {quality["unknown_targets"]}.')
        groups = m.split(rows)
        model = m.fit(groups['train'], groups['calibration'])
        periods = {name:{**m.evaluate(groups[name], model), 'start':m.PERIODS[name][0],
                         'end_exclusive':m.PERIODS[name][1]} for name in ('test','later')}
        latest = {}
        for ticker in m.TICKERS:
            selected = [r for r in rows if r['ticker']==ticker]
            if selected:
                latest[ticker] = m.forecast(model, max(selected,key=lambda r:r['at']))
        artifact = {'version':m.VERSION, 'rules':m.RULES, 'features':list(m.FEATURES), 'tickers':list(m.TICKERS),
            'entry_allowed':False, 'trained_at':datetime.now(timezone.utc).isoformat(), 'source_sha256':m.source_hashes(),
            'protocol_sha256':protocol, 'model':model, 'forecasts':latest, 'sources':sources, 'coverage':coverage,
            'instruments':instruments,
            'report':{'trained_rows':sum(r['y'] is not None for r in groups['train']),
                'calibration_rows':sum(r['y'] is not None for r in groups['calibration']), 'periods':periods,
                'note':'Проверка уже изученной истории, не независимые новые прогнозы. Прибыльность не доказана. '
                       'Рост/снижение считаются от17:45, а условная покупка — позже в18:00. '
                       'Дивиденды, налоги и реальные исполнения не моделируются.'},
            'note':'Отдельная экспериментальная модель. Не управляет прежним ИИ, Telegram или биржевыми заявками.'}
        artifact['artifact_sha256'] = m.digest(artifact)
        output = Path(settings.BASE_DIR)/'dashboard'/'ai_models'; output.mkdir(parents=True,exist_ok=True)
        path = output/f'morning-ridge-v1-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")}.json'
        immutable(path, artifact)
        def value(number):
            return 'нет данных' if number is None else f'{number:.2f}'
        for name, result in periods.items():
            p=result['paper']
            self.stdout.write(f'{name}: примеров {result["samples"]}, MAE {value(result["mae_bps"])} б.п., '
                f'наивный {value(result["baseline_mae_bps"])} б.п., направлений {value(result["direction_accuracy_pct"])}%, '
                f'условных сделок {p["trades"]}, результат {p["net_return_pct"]}, неизвестных {p["unknown_trades"]}.')
        self.stdout.write(f'Модель сохранена: {path.name}. entry_allowed=False; независимой проверки ещё нет.')
