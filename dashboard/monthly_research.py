"""Read the bounded, source-bound monthly portfolio report for the research UI."""
import hashlib
import json
import math
from pathlib import Path
from django.conf import settings
from .monthly_momentum import VERSION, RULES, VARIANTS, TICKERS, PERIODS, SCENARIOS, screen
from .management.commands.study_monthly_momentum import hashes
from .management.commands import audit_monthly_calendar


def load_monthly_research():
    empty = {'status': 'insufficient', 'entry_allowed': False, 'variants': [],
             'title': 'Месячный выбор сильных акций', 'message': 'Проверенного отчёта пока нет.'}
    root = (Path(settings.BASE_DIR)/'data').resolve()
    def read(path, size):
        if path.resolve().parent != root or path.stat().st_size > size:
            raise ValueError('Invalid research path')
        raw = path.read_bytes()
        return raw, json.loads(raw)
    try:
        paths = sorted(root.glob('monthly-calendar-review-2*.json'), reverse=True)
        if not paths:
            return empty
        _, audit = read(paths[0], 3_000_000)
        registration_raw, registration = read(root/'monthly-calendar-review-protocol-v1.json', 20000)
        if (audit['version'] != 'monthly_calendar_review_v1' or audit['entry_allowed'] is not False
                or audit['source_sha256'] != hashlib.sha256(Path(audit_monthly_calendar.__file__).read_bytes()).hexdigest()
                or audit['protocol_sha256'] != hashlib.sha256(registration_raw).hexdigest()
                or registration['spec']['report_sha256'] != audit['original_sha256']
                or registration['spec']['report_file'] != audit['original_report']
                or registration['spec']['source_sha256'] != audit['source_sha256']
                or registration['spec']['closed_days'] != list(audit_monthly_calendar.CLOSED)):
            return empty
        original_raw, report = read(root/audit['original_report'], 20_000_000)
        protocol_raw, protocol = read(root/'monthly-momentum-protocol-v1.json', 50000)
        if (hashlib.sha256(original_raw).hexdigest() != audit['original_sha256'] or report['version'] != VERSION
                or report['source_sha256'] != hashes() or report['entry_allowed'] is not False
                or report['protocol_sha256'] != hashlib.sha256(protocol_raw).hexdigest()
                or protocol['spec']['rules'] != RULES or protocol['spec']['source_sha256'] != hashes()
                or protocol['spec']['tickers'] != list(TICKERS)):
            return empty
        for ticker in TICKERS:
            source = report['inputs'][ticker]
            raw, _ = read(root/source['file'], 3_000_000)
            if source['file'] != f'range-input-{ticker}-20261003.json' or hashlib.sha256(raw).hexdigest() != source['sha256']:
                return empty
        summaries = audit['summaries']
        if len(summaries) != len(VARIANTS) or {r['key'] for r in summaries} != {r.key for r in VARIANTS}:
            return empty
        rows = []
        for rule in VARIANTS:
            summary = next(r for r in summaries if r['key'] == rule.key)
            periods = summary['periods']
            if set(periods) != set(PERIODS):
                return empty
            for period, scenarios in periods.items():
                if set(scenarios) != set(SCENARIOS):
                    return empty
                for scenario, m in scenarios.items():
                    original = report['results'][rule.key][period][scenario]
                    if (type(m['trades']) is not int or m['trades'] < 0 or m['trades'] != original['trades']
                            or type(m['verified']) is not bool or type(m['unknown_trades']) is not int
                            or not 0 <= m['unknown_trades'] <= m['trades']):
                        return empty
                    for key in ('return_pct', 'max_drawdown_pct'):
                        if type(m[key]) not in (int, float) or not math.isfinite(m[key]) or m[key] != original[key]:
                            return empty
            rows.append({'key': rule.key, 'name': f'{rule.months} мес. · до {rule.count} акц.'+(' · фильтр рынка' if rule.breadth else ''),
                         'full': periods['full']['base'], 'double_cost': periods['full']['double_cost'],
                         'delayed': periods['full']['delayed'], 'selection': periods['selection']['base'],
                         'recent': periods['recent']['base'], 'passes_screen': screen(periods)})
        return {**empty, 'status': 'ok', 'variants': rows, 'at': audit['at'], 'simulations': report['simulations'],
                'passed': sum(r['passes_screen'] for r in rows), 'rolling': report['rolling_summary'],
                'bootstrap': audit['bootstrap'],
                'message': '12 способов выбирать одну или три сильные акции раз в месяц. Ни один не прошёл все проверки.',
                'note': 'Общий портфель из списка SBER, GAZP, LKOH, NVTK, GMKN, ROSN, YDEX; таблица не относится только к выбранному тикеру. '
                        'Условный капитал 100 000 ₽. Весь период: январь 2024 — 2 октября 2026; поздняя проверка: январь — июнь 2026, последний участок: июль — 2 октября. '
                        'По 0,05% комиссии и проскальзывания за сторону, риск до 0,5% на акцию, до 25% капитала в одной позиции. '
                        'Новогодняя пауза проверена по календарю биржи; цены и сделки сохранены. '
                        'Дивиденды, налоги и доход на свободные деньги не учтены. Эти даты уже изучались; независимое подтверждение ещё требуется.'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, StopIteration):
        return empty
