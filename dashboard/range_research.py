"""Read fixed exploratory range results; positive numbers never authorize LONG."""
import hashlib
import json
import math
from pathlib import Path

from django.conf import settings
from .management.commands.study_ranges_full import hashes, load_input, VERSION, RULES
from .range_study import TICKERS, PERIODS, registry
from .strategy_search import aggregate

NAMES = {'breakout_trailing_20': 'Пробой максимума 20 дневных свечей + защитный выход',
         'breakout_trailing_55': 'Пробой максимума 55 дневных свечей + защитный выход',
         'low_reclaim_20': 'Возврат выше минимума 20 дневных свечей + подтверждение',
         'low_reclaim_55': 'Возврат выше минимума 55 дневных свечей + подтверждение'}


def load_range_research(ticker):
    empty = {'status': 'insufficient', 'entry_allowed': False, 'variants': [],
             'message': 'Проверяемого отчёта по максимумам и минимумам пока нет.'}
    directory = (Path(settings.BASE_DIR)/'data').resolve()
    try:
        def read(path, limit):
            if not path.resolve().is_relative_to(directory) or path.stat().st_size > limit:
                raise ValueError('Invalid report')
            raw = path.read_bytes()
            if len(raw) > limit:
                raise ValueError('Oversized report')
            return raw, json.loads(raw)
        paths = sorted(directory.glob('range-swing-v2-study-*.json'), reverse=True)
        if not paths:
            return empty
        raw, protocol = read(directory/'range-swing-protocol-v2.json', 50000)
        spec = protocol['spec']
        if (protocol['source_sha256'] != hashes() or spec['version'] != VERSION
                or spec['registry'] != registry() or spec['rules'] != RULES
                or spec['periods'] != {k:list(v) for k,v in PERIODS.items()}
                or spec['tickers'] != list(TICKERS) or spec['entry_allowed'] is not False):
            return empty
        _, report = read(paths[0], 3_000_000)
        if (report['version'] != VERSION or report['source_sha256'] != hashes()
                or report['protocol_sha256'] != hashlib.sha256(raw).hexdigest()
                or report['entry_allowed'] is not False or len(report['results']) != len(TICKERS)
                or {r['ticker'] for r in report['results']} != set(TICKERS)):
            return empty
        for stock in report['results']:
            source = stock['source']
            name = f"range-input-{stock['ticker']}-20261003.json"
            if source['file'] != name or stock['quality'] is not True or set(stock['variants']) != set(registry()):
                return empty
            input_raw, _ = read(directory/name, 3_000_000)
            dataset = load_input(directory/name, stock['ticker'])
            if hashlib.sha256(input_raw).hexdigest() != source['sha256'] or not dataset['quality'] or source['bars'] != len(dataset['candles']):
                return empty
            for periods in stock['variants'].values():
                if set(periods) != set(PERIODS):
                    return empty
                for m in periods.values():
                    if (m['status'] != 'ok' or type(m['trades']) is not int or m['trades'] < 0
                            or type(m['unverified_trades']) is not int or not 0 <= m['unverified_trades'] <= m['trades']
                            or not all(type(m[k]) in (int,float) and math.isfinite(m[k]) for k in ('return_pct','stress_return_pct','max_drawdown_pct'))):
                        return empty
                    if (len(m['trade_log']) != m['trades'] or len(m['daily_curve']) > 2000
                            or any(type(t['return_pct']) not in (int,float) or not math.isfinite(t['return_pct']) for t in m['trade_log'])
                            or any(type(c['equity']) not in (int,float) or not math.isfinite(c['equity']) or c['equity'] <= 0 for c in m['daily_curve'])):
                        return empty
        if ticker not in TICKERS:
            return {**empty, 'message': 'Эта акция не участвовала в проверке. Отсутствие результата — не доходность 0%.'}
        selected = next(r for r in report['results'] if r['ticker'] == ticker)
        def metrics(m):
            return {k: (None if m['trades'] == 0 and k in ('return_pct', 'stress_return_pct') else m[k])
                    for k in ('trades','return_pct','stress_return_pct','unverified_trades')}
        variants = [{'key': key, 'name': name, 'periods': {p:metrics(selected['variants'][key][p]) for p in PERIODS},
                     'group_periods': {p:metrics(aggregate([r['variants'][key][p] for r in report['results']])) for p in PERIODS}}
                    for key,name in NAMES.items()]
        return {'status': 'ok', 'ticker': ticker, 'entry_allowed': False, 'variants': variants, 'at': report['at'],
                'message': 'Четыре дневных сценария, версия 2: учтены свечи выходных при удержании. Ни один не доказал устойчивую прибыльность; правила не включены в сигнал покупки.',
                'note': 'Проценты — изменение условного счёта 100 000 ₽ на акцию, риск 0,5%, позиция до 25%. Комиссия и проскальзывание по 0,05% за сторону; стресс — расходы ×2. Вход только в будни, стопы проверены на всех наблюдаемых дневных барах, включая выходные. 2023–июнь 2026, июль–август, сентябрь–2 октября. Эти периоды уже изучались и не являются независимой проверкой. Непроверенные исходы показаны, но не подтверждены. Дивиденды, налоги и исполнение не учтены.'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return empty
