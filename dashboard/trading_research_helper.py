"""Local explanations of data, evidence and risk; no forecasts, orders or APIs.

This deterministic reviewer complements the site's existing trained neural
model. It never invents probabilities or turns historical profit into a live
instruction. Caller supplies already validated public reports, not secrets.
"""
import math
from datetime import datetime, timedelta, timezone

VERSION = 'trading_research_helper_v1'
MAX_ANALYSIS_AGE = timedelta(seconds=30)
MAX_BOOK_AGE = timedelta(seconds=45)
RULE_NAMES = {
    'trend': 'Цена движется вверх', 'flow': 'Покупатели активнее',
    'volume': 'Объём выше обычного', 'vwap': 'Цена удерживает среднюю дня',
    'spread': 'Небольшой разрыв цен', 'book': 'Объём покупок устойчив',
}


def _time(value):
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        return None
    return at.astimezone(timezone.utc)


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _data(payload, analysis, now):
    if payload.get('mode') == 'demo':
        return {'state': 'demo', 'message': 'График учебный: цены вымышлены, результат не проверяет торговую идею.', 'valid_until': None}
    if payload.get('historical') is True or payload.get('mode') in ('archive', 'historical'):
        return {'state': 'historical', 'message': 'Это разбор прошлых данных; он не является текущим сигналом.', 'valid_until': None}
    if payload.get('status') != 'ok' or payload.get('mode') != 'live' or payload.get('source') != 'Т-Банк · TQBR':
        return {'state': 'unavailable', 'message': 'Нет подтверждённых текущих рыночных данных для разбора.', 'valid_until': None}
    updated, book = _time(analysis.get('updated_at')), _time(analysis.get('data_time'))
    if updated is None or book is None or updated > now or book > now:
        return {'state': 'invalid_time', 'message': 'Не удалось подтвердить время оценки и биржевых котировок.', 'valid_until': None}
    until = min(updated+MAX_ANALYSIS_AGE, book+MAX_BOOK_AGE)
    if now > until:
        return {'state': 'stale_live', 'message': 'Котировки или оценка устарели; нужен новый снимок рынка.', 'valid_until': None}
    quality = analysis.get('data_quality')
    blockers = analysis.get('blockers')
    if (not isinstance(quality, dict) or quality.get('action_blocked') is not False
            or quality.get('book_consistency_confirmed') is not True
            or not isinstance(blockers, list) or bool(blockers)):
        return {'state': 'blocked_live', 'message': 'Рыночная проверка отмечает ограничения или пробелы в данных.', 'valid_until': None}
    return {'state': 'fresh_live', 'message': 'Время оценки и котировок укладывается в допустимое окно.',
            'valid_until': until.isoformat()}


def _rules(analysis):
    values = analysis.get('rules')
    values = values if isinstance(values, list) else []
    by_key, duplicate = {}, False
    for rule in values:
        if (not isinstance(rule, dict) or not isinstance(rule.get('key'), str)
                or rule['key'] not in RULE_NAMES):
            continue
        if rule['key'] in by_key:
            duplicate = True
        by_key[rule['key']] = rule.get('pass')
    conditions = [{'key': key, 'name': name,
        'state': 'passed' if by_key.get(key) is True else 'failed' if by_key.get(key) is False else 'missing'}
        for key, name in RULE_NAMES.items()]
    passed = sum(c['state'] == 'passed' for c in conditions)
    failed = sum(c['state'] == 'failed' for c in conditions)
    missing = len(conditions)-passed-failed
    if duplicate:
        missing = len(conditions)
        passed = failed = 0
        conditions = [{**c, 'state': 'missing'} for c in conditions]
    return {'passed': passed, 'failed': failed, 'missing': missing, 'total': len(conditions),
            'conditions': conditions,
            'message': f'Условий выполнено: {passed} из 6. Не выполнено: {failed}; без данных: {missing}.',
            'score_note': 'Баллы условий и согласие моделей не равны вероятности заработка.'}


def _paper(report):
    base = {'state': 'unavailable', 'closed': 0, 'unknown': 0, 'open': 0, 'pending': 0,
            'closed_sample_return_pct': None, 'message': 'Журнал исходов ещё не получен.'}
    if not isinstance(report, dict) or report.get('status') != 'ok' or not isinstance(report.get('summary'), dict):
        return base
    summary = report['summary']
    counts = {key: _count(summary.get(key)) for key in ('closed','unverified','open','pending')}
    if any(value is None for value in counts.values()):
        return {**base, 'message': 'Не удалось проверить количества исходов в журнале.'}
    closed, unknown, opened, pending = (counts[k] for k in ('closed','unverified','open','pending'))
    complete = closed > 0 and not (unknown or opened or pending)
    value = summary.get('net_return_pct')
    value = float(value) if complete and _number(value) else None
    message = (f'Закрытых учебных исходов: {closed}; неизвестных: {unknown}; '
               f'открытых или ожидающих входа: {opened+pending}.')
    if unknown:
        message += ' Положительный результат только известных сделок не показывает итог всех сигналов.'
    elif complete:
        message += ' Это расчёт по снимкам рынка, реальные деньги не участвовали.'
    return {'state': 'complete_sample' if complete and value is not None else 'incomplete' if unknown or opened or pending else 'empty',
            'closed': closed, 'unknown': unknown, 'open': opened, 'pending': pending,
            'closed_sample_return_pct': value, 'message': message}


def _research(model_report, forward_report, paper_report):
    paper = _paper(paper_report)
    available = isinstance(model_report, dict) and model_report.get('status') == 'ok'
    forward = forward_report if isinstance(forward_report, dict) and forward_report.get('status') == 'ok' else {}
    pairs = forward.get('buy_candidates') or {}
    pairs = pairs if isinstance(pairs, dict) else {}
    count = _count(pairs.get('samples'))
    model_note = ('Обученная модель доступна; её исторические результаты нужно проверять отдельно на новых наблюдениях.'
                  if available else 'Проверенный отчёт обученной модели пока не получен.')
    if available and model_report.get('paper_ready') is not True:
        model_note += ' Заданный моделью порог исторической проверки не пройден.'
    return {'state': 'research_only', 'model_available': available,
            'historical_model_note': model_note, 'forward_candidate_pairs': count,
            'forward_note': ('Отчёт проверки новых котировок не получен.' if count is None else
                             f'Для кандидатов покупки есть {count} завершённых сравнений с более поздними котировками.'
                             if count else 'Новых завершённых сравнений кандидатов покупки пока нет.'),
            'paper': paper,
            'message': 'Исторический расчёт, проверка новых котировок и реальные сделки — разные виды результата.'}


def _risk(analysis, fresh):
    base = {'state': 'waiting', 'entry': None, 'stop': None, 'target': None,
            'price_distance_risk_pct': None, 'reward_risk_before_costs': None,
            'message': 'Для расчёта риска нужны свежий сценарий, капитал и допустимый размер убытка.'}
    if not fresh or analysis.get('decision') != 'LONG':
        return base
    plan = analysis.get('plan')
    if not isinstance(plan, dict):
        return {**base, 'state': 'missing_plan', 'message': 'У кандидата нет проверяемого сценария со стопом и целью.'}
    entry, stop, target = (plan.get(k) for k in ('entry','stop','target'))
    if (not all(_number(v) for v in (entry, stop, target)) or not 0 < stop < entry < target):
        return {**base, 'state': 'invalid_plan', 'message': 'Цены сценария не прошли проверку: нужен стоп ниже входа и цель выше.'}
    distance = entry-stop
    risk, rr = distance/entry*100, (target-entry)/distance
    if not all(math.isfinite(v) for v in (risk, rr)):
        return {**base, 'state': 'invalid_plan', 'message': 'Расстояния сценария не удалось корректно рассчитать.'}
    return {'state': 'scenario_only', 'entry': float(entry), 'stop': float(stop), 'target': float(target),
            'price_distance_risk_pct': risk, 'reward_risk_before_costs': rr,
            'message': 'Показан только риск движения цены до стопа. Размер позиции, расходы и возможный выход хуже стопа проверь в калькуляторе.'}


def summarize_research(signal_payload, *, now, model_report=None, paper_report=None, forward_report=None):
    """Return a JSON-safe explanation from existing data without mutable state/IO.

    ``model_report`` is the public neural/AI model_status result; optional
    ``paper_report`` and ``forward_report`` are the existing read-only APIs.
    Even a valid fresh candidate is REVIEW, never a purchase authorization.
    """
    current = _time(now)
    if current is None:
        raise ValueError('Timezone-aware review time required')
    payload = signal_payload if isinstance(signal_payload, dict) else {}
    analysis = payload.get('analysis')
    analysis = analysis if isinstance(analysis, dict) else {}
    data = _data(payload, analysis, current)
    rules = _rules(analysis)
    fresh = data['state'] == 'fresh_live'
    risk = _risk(analysis, fresh)
    evidence = _research(model_report, forward_report, paper_report)
    observed = analysis.get('decision') if analysis.get('decision') in ('LONG','WAIT','EXIT') else None
    candidate = fresh and observed == 'LONG' and rules['passed'] == 6 and risk['state'] == 'scenario_only'
    headline = ('Есть сценарий для разбора' if candidate else 'Пока наблюдать')
    neural = analysis.get('neural')
    neural = neural if isinstance(neural, dict) else {}
    neural_until = _time(neural.get('valid_until'))
    neural_fresh = fresh and neural.get('status') == 'ok' and neural_until is not None and neural_until >= current
    net = neural.get('expected_net_pct')
    neural_net = float(net) if neural_fresh and _number(net) else None
    if neural_fresh and neural_net is None:
        neural_fresh = False
    return {'status': 'ok', 'version': VERSION, 'created_at': current.isoformat(),
        'action': 'REVIEW' if candidate else 'WAIT', 'entry_allowed': False, 'confidence': None,
        'headline': headline, 'data_review': data, 'signal_review': {**rules, 'observed_decision': observed},
        'research_review': evidence, 'risk_review': risk,
        'neural_review': {'state': 'fresh_estimate' if neural_fresh else 'waiting',
            'expected_net_pct': neural_net,
            'message': 'Это ожидаемый моделью результат условного сценария, а не обещание дохода.' if neural_fresh
                       else 'Свежая проверенная оценка нейросети пока недоступна.'},
        'next_steps': (["Проверь стоп, размер позиции и расходы в калькуляторе.",
                        "Сохрани сценарий и оцени его по следующим реальным котировкам."] if candidate else
                       [data['message'], 'Дождись достаточных данных и совпадения проверяемых условий.']),
        'method': 'Локальный помощник с фиксированными правилами объяснения; внешняя нейросеть и API-ключи не используются.'}
