"""Display fixed stream evidence; never turn exploratory results into LONG."""
import hashlib
import json
import math
from pathlib import Path
from django.conf import settings

from .management.commands.study_pressure import hashes
from .pressure_study import VERSION, VARIANTS, RULES, summary


def load_pressure_research(ticker):
    empty = {"status": "insufficient", "entry_allowed": False, "variants": [],
             "message": "Проверяемого отчёта по записанному стакану и ленте пока нет."}
    directory = (Path(settings.BASE_DIR)/'data').resolve()
    try:
        def read(path, limit):
            if not path.resolve().is_relative_to(directory) or path.stat().st_size > limit:
                raise ValueError('Invalid report path or size')
            raw = path.read_bytes()
            if len(raw) > limit:
                raise ValueError('Report too large')
            return raw, json.loads(raw)
        paths = sorted(directory.glob('pressure-stream-study-*.json'), reverse=True)
        if not paths:
            return empty
        raw, protocol = read(directory/'pressure-stream-protocol-v1.json', 50000)
        spec = protocol['spec']
        if (protocol['source_sha256'] != hashes() or spec['version'] != VERSION
                or spec['variants'] != VARIANTS or spec['rules'] != RULES or spec['entry_allowed'] is not False):
            return empty
        _, report = read(paths[0], 2_000_000)
        if (report['version'] != VERSION or report['entry_allowed'] is not False
                or report['protocol_sha256'] != hashlib.sha256(raw).hexdigest()
                or report['source_sha256'] != hashes() or report['session'] != spec['session']
                or set(report['outcomes']) != set(VARIANTS)):
            return empty
        outcomes = report['outcomes']
        tickers = set()
        for values in outcomes.values():
            if not isinstance(values, list) or len(values) > 10000:
                return empty
            for r in values:
                tickers.add(r['ticker'])
                if r['status'] not in ('unverified', 'verified_quote_replay'):
                    return empty
                if r['status'] == 'verified_quote_replay' and not all(
                        type(r[k]) in (int, float) and math.isfinite(r[k])
                        for k in ('return_pct', 'stress_return_pct', 'gross_quote_return_pct')):
                    return empty
        if ticker not in tickers:
            return {**empty, 'message': 'Для этой акции проверенных потоковых сценариев нет. Это не означает доходность 0%.'}
        variants = []
        names = {'persistent_bid': 'Устойчивая поддержка покупателей', 'positive_ofi': 'Изменение спроса в стакане'}
        for key, variant in VARIANTS.items():
            values = summary([r for r in outcomes[key] if r['ticker'] == ticker])
            variants.append({'key': key, 'name': f"{names[variant['family']]} · {variant['horizon_seconds']//60} мин.", **values})
        return {'status': 'ok', 'entry_allowed': False, 'variants': variants, 'at': report['at'],
                'message': 'Первые тесты записанного стакана и сделок. Один день наблюдений — недостаточно для вывода о прибыльности.',
                'note': 'Средний результат одной условной сделки, не доходность счёта. Покупка по следующей цене продавца, выход по цене покупателя; по 0,05% комиссии и проскальзывания за сторону. Разрывы и окончания сегментов — непроверенные исходы. Полнота ленты и исполнение не гарантированы; разрешения на торговлю нет.'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return empty
