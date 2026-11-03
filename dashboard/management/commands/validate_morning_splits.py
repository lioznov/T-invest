"""Freeze and run three temporal splits from cached real prices; no network."""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import morning_model as m
from dashboard import multiyear_model as multi
from dashboard import temporal_validation as study
from dashboard.multiyear_inputs import load_history
from .train_morning_forecast import immutable


def freeze(directory):
    spec = json.loads(json.dumps({'version': study.VERSION, 'rules': study.RULES,
        'folds': study.FOLDS, 'current_year': study.CURRENT_YEAR,
        'features': m.FEATURES, 'source_sha256': study.source_hashes()}))
    path = directory/'morning-temporal-protocol-v1.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['spec'] != spec:
            raise CommandError('Хронологический протокол изменён; нужна новая версия, старые результаты не перезаписываются.')
    else:
        immutable(path, {'registered_at': datetime.now(timezone.utc).isoformat(),
                         'spec': spec, 'entry_allowed': False})
    return m.digest(spec)


class Command(BaseCommand):
    help = 'Validate three expanding-time morning Ridge models on cached EXCHANGE data; no API, orders or Telegram.'

    def handle(self, *args, **options):
        root = Path(settings.BASE_DIR)/'data'
        root.mkdir(parents=True, exist_ok=True)
        protocol = freeze(root)  # Freeze exact code/choices before reading any prices.
        rows, coverage, sources, quality = [], {}, {}, {}
        for ticker in m.TICKERS:
            history = load_history(root/'multiyear', ticker, '15m')
            examples, detail = multi.build_intraday(history['candles'], ticker, history['instrument']['lot'])
            rows.extend(examples['control'])
            coverage[ticker] = history['coverage']
            sources[ticker] = history['sources']
            quality[ticker] = detail['control']
            self.stdout.write(f'{ticker}: {len(examples["control"])} пригодных примеров, включая неизвестные цели.')
        folds = study.run_folds(rows, progress=self.stdout.write)
        artifact = {'version': study.VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'entry_allowed': False, 'source_sha256': study.source_hashes(),
                    'protocol_sha256': protocol, 'rules': study.RULES, 'folds': folds,
                    'coverage': coverage, 'sources': sources, 'feature_coverage': quality,
                    'note': study.NOTE,
                    'publication_note': 'Все три варианта опубликованы без выбора победителя. '
                        'Общий тест2026 повторяет тест третьего варианта, не является дополнительным независимым опытом. '
                        'Новые даты, чьи прогнозы были заранее записаны, этим расчётом не создаются.'}
        artifact['artifact_sha256'] = m.digest(artifact)
        output = Path(settings.BASE_DIR)/'dashboard'/'ai_models'
        output.mkdir(parents=True, exist_ok=True)
        path = output/f'morning-temporal-v1-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")}.json'
        immutable(path, artifact)
        def value(metric):
            return 'не определено' if metric is None else f'{metric:.4f}'
        for fold in folds:
            for label, report in (('основной', fold['report']), ('общий2026', fold['current_year_report'])):
                self.stdout.write(f'{fold["id"]} {label}: N={report["samples"]}, '
                    f'MAE={value(report["mae_bps"])}б.п., RMSE={value(report["rmse_bps"])}б.п., '
                    f'baselineMAE={value(report["baseline_mae_bps"])}б.п., '
                    f'LONG={report["paper"]["trades"]}, счёт={value(report["paper"]["net_return_pct"])}%.')
        self.stdout.write(f'Сохранено {path.name}. Только исследование, не разрешение торговли.')
