"""Fail-closed display of adapted trader tactics, never entry authorization."""
import hashlib
import json
import math
from pathlib import Path

from django.conf import settings
from .trader_tactics import VERSION,RULES,NAMES,registry
from .range_study import TICKERS,PERIODS
from .strategy_search import aggregate
from .management.commands.study_trader_tactics import hashes
from .management.commands.study_ranges_full import load_input


def load_trader_research(ticker):
    empty = {'status':'insufficient','entry_allowed':False,'variants':[],'title':'Тактики трейдеров: покупки после отката',
             'message':'Проверяемого отчёта по адаптациям тактик пока нет.'}
    directory = (Path(settings.BASE_DIR)/'data').resolve()
    try:
        def read(path,limit):
            if not path.resolve().is_relative_to(directory) or path.stat().st_size>limit:
                raise ValueError('Invalid research file')
            raw = path.read_bytes()
            if len(raw)>limit:
                raise ValueError('Oversized research file')
            return raw,json.loads(raw)
        paths = sorted(directory.glob('trader-tactics-study-*.json'),reverse=True)
        if not paths:
            return empty
        raw,protocol = read(directory/'trader-tactics-protocol-v1.json',50000)
        spec = protocol['spec']
        if (protocol['source_sha256']!=hashes() or spec['version']!=VERSION or spec['rules']!=json.loads(json.dumps(RULES))
                or spec['registry']!=registry() or spec['periods']!={k:list(v) for k,v in PERIODS.items()}
                or spec['tickers']!=list(TICKERS) or spec['entry_allowed'] is not False):
            return empty
        _,report = read(paths[0],3_000_000)
        if (report['version']!=VERSION or report['source_sha256']!=hashes()
                or report['protocol_sha256']!=hashlib.sha256(raw).hexdigest() or report['entry_allowed'] is not False
                or len(report['results'])!=7 or {r['ticker'] for r in report['results']}!=set(TICKERS)):
            return empty
        for stock in report['results']:
            name = f"range-input-{stock['ticker']}-20261003.json"
            source = stock['source']
            if source['file']!=name or stock['quality'] is not True or set(stock['variants'])!=set(registry()):
                return empty
            input_raw,_ = read(directory/name,3_000_000)
            data = load_input(directory/name,stock['ticker'])
            if hashlib.sha256(input_raw).hexdigest()!=source['sha256'] or source['bars']!=len(data['candles']) or not data['quality']:
                return empty
            for periods in stock['variants'].values():
                if set(periods)!=set(PERIODS):
                    return empty
                for m in periods.values():
                    if (m['status']!='ok' or type(m['trades']) is not int or m['trades']<0
                            or len(m['trade_log'])!=m['trades'] or type(m['unverified_trades']) is not int
                            or not 0 <= m['unverified_trades'] <= m['trades']
                            or not all(type(m[k]) in (int,float) and math.isfinite(m[k]) for k in ('return_pct','stress_return_pct','max_drawdown_pct'))
                            or any(type(t['return_pct']) not in (int,float) or not math.isfinite(t['return_pct']) for t in m['trade_log'])
                            or len(m['daily_curve'])>2000
                            or any(type(c['equity']) not in (int,float) or not math.isfinite(c['equity']) or c['equity']<=0 for c in m['daily_curve'])):
                        return empty
        if ticker not in TICKERS:
            return {**empty,'message':'Эта акция не участвовала в тесте тактик. Отсутствие результата — не 0% доходности.'}
        selected = next(r for r in report['results'] if r['ticker']==ticker)
        def metrics(m):
            return {k:None if m['trades']==0 and k in ('return_pct','stress_return_pct') else m[k]
                    for k in ('trades','return_pct','stress_return_pct','unverified_trades')}
        variants = [{'key':key,'name':name,'periods':{p:metrics(selected['variants'][key][p]) for p in PERIODS},
                     'group_periods':{p:metrics(aggregate([r['variants'][key][p] for r in report['results']])) for p in PERIODS}}
                    for key,name in NAMES.items()]
        return {**empty,'status':'ok','ticker':ticker,'variants':variants,'at':report['at'],
                'message':'Адаптации исследований Сезара Альвареса: сильная акция, короткое падение и ожидание отскока. Ни один из трёх вариантов не подтвердил прибыльность.',
                'sources':list(RULES['source']),
                'note':'Не копия результатов автора: другой рынок, наш стоп и риск. SMA200 и RSI2 считаются по всем закрытым дневным свечам, включая выходные; новые входы только в будни. Проценты — изменение условного счёта 100 000 ₽ на акцию, риск 0,5%, позиция до 25%. Комиссия и проскальзывание по 0,05% за сторону, стресс ×2. Вход по более низкой цене моделируется только после строгого пересечения; касание не считается исполнением. Все исполнения условны; непроверенные исходы относятся к качеству свечей, не к гарантии сделок. Периоды уже изучались: независимого подтверждения, дивидендов, налогов и очереди заявок нет.'}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
        return empty
