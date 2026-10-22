"""Bounded delivery of opening-activity evidence, never a live trade approval."""
import hashlib
import json
import math
from pathlib import Path
from django.conf import settings
from .opening_activity import VERSION, RULES, VARIANTS, TICKERS, PERIODS, SCENARIOS, aggregate, passes
from .management.commands.study_opening_activity import hashes


def load_opening_research(ticker):
    empty = {'status':'insufficient','entry_allowed':False,'variants':[],
             'title':'Утренний пробой и необычный объём','message':'Проверенного отчёта нового алгоритма пока нет.'}
    root = (Path(settings.BASE_DIR)/'data').resolve()
    def read(path, maximum):
        if path.resolve().parent != root or path.stat().st_size > maximum:
            raise ValueError('Invalid research file')
        raw = path.read_bytes()
        return raw,json.loads(raw)
    try:
        paths = sorted(root.glob('opening-activity-study-*.json'),reverse=True)
        if not paths or ticker not in TICKERS:
            return empty
        protocol_raw,protocol = read(root/'opening-activity-protocol-v1.json',50000)
        _,report = read(paths[0],20_000_000)
        if (report['version'] != VERSION or report['source_sha256'] != hashes() or report['entry_allowed'] is not False
                or report['protocol_sha256'] != hashlib.sha256(protocol_raw).hexdigest() or report['rules'] != RULES
                or protocol['spec']['source_sha256'] != hashes() or protocol['spec']['rules'] != RULES
                or protocol['spec']['variants'] != VARIANTS or set(report['results']) != set(TICKERS)):
            return empty
        results = report['results']
        for name,row in results.items():
            sources = [row['source']]+([row['additional_source']] if row['additional_source'] else [])
            expected_names = {f'flow-input-{name}-20261003.json',f'opening-activity-input-{name}-20261013.json'}
            for source in sources:
                raw,_ = read(root/source['file'],4_000_000)
                if source['file'] not in expected_names or hashlib.sha256(raw).hexdigest() != source['sha256']:
                    return empty
            if set(row['variants']) != set(VARIANTS):
                return empty
            for periods in row['variants'].values():
                if set(periods) != set(PERIODS):
                    return empty
                for scenarios in periods.values():
                    if set(scenarios) != set(SCENARIOS):
                        return empty
                    for m in scenarios.values():
                        if (type(m['trades']) is not int or m['trades'] < 0 or len(m['trade_log']) != m['trades']
                                or type(m['unverified_trades']) is not int or not 0 <= m['unverified_trades'] <= m['trades']
                                or m['entry_allowed'] is not False):
                            return empty
                        if m['status'] == 'ok':
                            if type(m['return_pct']) not in (int,float) or not math.isfinite(m['return_pct']):
                                return empty
                        elif m['status'] != 'insufficient' or m['return_pct'] is not None or m['trades']:
                            return empty
        expected = []
        for key,name in VARIANTS.items():
            periods = {p:{s:aggregate([r['variants'][key][p][s] for r in results.values()]) for s in SCENARIOS} for p in PERIODS}
            expected.append({'key':key,'name':name,'periods':periods,'passes_screen':passes(periods),'entry_allowed':False})
        if report['summaries'] != expected:
            return empty
        def small(m):
            return {k:m[k] for k in ('trades','return_pct','unverified_trades')}
        variants = [{'key':key,'name':name,'stock':{p:{s:small(results[ticker]['variants'][key][p][s]) for s in SCENARIOS} for p in PERIODS},
                     'group':next(r['periods'] for r in expected if r['key']==key)} for key,name in VARIANTS.items()]
        return {**empty,'status':'ok','ticker':ticker,'at':report['at'],'variants':variants,
                'passed':sum(r['passes_screen'] for r in expected),'simulations':report['simulations'],
                'message':'Сравнение обычного пробоя, выбора активных акций и входа после возврата к уровню. Устойчивый заработок пока не подтверждён.',
                'additional_accounts':expected[0]['periods']['additional']['base']['accounts'],
                'sources':report['sources'],'prospective_start':protocol['prospective_start'],
                'note':'Условный счёт 100 000 ₽ на акцию, риск 0,5%, доля позиции до 25%. Среднее семи отдельных счетов; это не общий торговый портфель. '
                       'Вход по следующей цене открытия, стоп или выход в 18:00 МСК; по 0,05% комиссии и проскальзывания за сторону. '
                       'Отдельно проверены удвоенные расходы и вход на 15 минут позже. Объём сравнивается с тем же утренним интервалом предыдущих 14 дней. '
                       'Это адаптация идей авторов на 15-минутные свечи российских акций; их результаты сюда не переносятся. '
                       '3–12 октября — дополнительная история, существовавшая при фиксации правил. Стакан, новости, исполнение и налоги в расчёт не входят.'}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError,StopIteration):
        return empty
