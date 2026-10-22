"""Explainable headline semantics, not a prediction or a factual verification."""
import re
from copy import deepcopy
from datetime import datetime
from hashlib import sha256

VERSION = 'headline_context_v2'
TONES = {'positive':'Позитивная','negative':'Негативная','neutral':'Нейтральная','uncertain':'Неоднозначная'}
KINDS = {'event':'Сообщение о событии','opinion':'Мнение / прогноз','unconfirmed':'Неподтверждённое сообщение'}
RULES = {
    'negative': (
        (r'(?:не\s+(?:утверд\w*|выплат\w*)|отмен\w*|отказ\w*|без).{0,24}дивиденд', 'Дивиденды отменены, не утверждены или не выплачены'),
        (r'(?:прибыл\w*|выручк\w*).{0,30}(?:сниз\w*|сократ\w*|упал\w*)|(?:сниж\w*|падени\w*|сокращ\w*).{0,20}(?:прибыл|выручк)', 'Сообщается о снижении прибыли или выручки'),
        (r'убыт\w*.{0,25}(?:вырос|увелич|возрос)|(?:рост|увелич\w*).{0,15}убыт', 'Сообщается об увеличении убытка'),
        (r'(?:ввел\w*|введ\w*|попал\w*|включ\w*).{0,25}санкци|санкци\w*.{0,15}против', 'Сообщается о санкционных ограничениях'),
        (r'банкрот|дефолт|приостанов\w*.{0,15}(?:производств|торг)|авари\w*.{0,20}(?:завод|рудник|предприяти)', 'Есть признаки существенного операционного или финансового риска'),
    ),
    'positive': (
        (r'(?:прибыл\w*|выручк\w*).{0,30}(?:вырос\w*|увелич\w*|повыс\w*)|(?:рост|увелич\w*).{0,20}(?:прибыл|выручк)', 'Сообщается о росте прибыли или выручки'),
        (r'убыт\w*.{0,25}(?:сократ|сниз)|(?:сокращ\w*|сниж\w*).{0,15}убыт', 'Сообщается о сокращении убытка'),
        (r'рекордн\w*.{0,20}(?:прибыл|выручк)', 'Указаны рекордные финансовые показатели'),
        (r'(?:снят\w*|отмен\w*).{0,15}санкци|санкци\w*.{0,15}(?:снят|отмен)', 'Сообщается о снятии санкционных ограничений'),
    ),
}


def analyze(title, official=False):
    text = title.casefold().replace('ё','е')
    kind = 'unconfirmed' if re.search(r'слух|неподтвержд|по данным источников|анонимн',text) else (
        'opinion' if re.search(r'аналитик|прогноз|ожида\w*|может\b|могут\b|вероятн|не исключа|по мнению|рекоменду\w* покуп',text) else 'event')
    hits = {tone:[reason for pattern,reason in rules if re.search(pattern,text)] for tone,rules in RULES.items()}
    negated = bool(re.search(r'\bне\b.{0,22}(?:рост|вырос|увелич|рекорд|сократ|сниз|ввел|введ|попал|отмен)',text))
    if negated:
        hits['positive'] = []
        hits['negative'] = []
    if hits['negative'] and hits['positive']:
        tone, reasons = 'uncertain', hits['negative']+hits['positive']
    elif hits['negative']:
        tone,reasons = 'negative',hits['negative']
    elif hits['positive']:
        tone,reasons = 'positive',hits['positive']
    elif negated or kind == 'unconfirmed':
        tone,reasons = 'uncertain',['В заголовке есть отрицание или неподтверждённое утверждение; направление не определено.']
    else:
        tone,reasons = 'neutral',['По заголовку нет однозначного позитивного или негативного изменения.']
    return {'version':VERSION,'tone':tone,'tone_label':TONES[tone],'kind':kind,'kind_label':KINDS[kind],
            'reasons':reasons,'official_source':official,'verified_fact':False,'entry_allowed':False,
            'caution':'Это смысл формулировки, не прогноз цены. Нейтральная метка не означает отсутствия влияния.'}


STOP = {'и','в','на','по','за','с','о','от','к','для','из','об','у','до','а','но','что','как','это','года','году','год','рублей','руб','компания','сообщил','сообщила'}


def signature(title):
    text = title.casefold().replace('ё','е')
    tokens = re.findall(r'[a-zа-я0-9]+',text)
    # Keep numbers and negations: 10 vs 20% and approval vs rejection must not
    # disappear under a generic text-similarity merge.
    words = {t for t in tokens if t not in STOP}
    return words,{t for t in tokens if t.isdigit()},bool(re.search(r'\bне\b|отмен|отказ',text))


def same_event(a,b):
    aw,an,neg_a = signature(a['title']); bw,bn,neg_b = signature(b['title'])
    if neg_a != neg_b or an != bn or a['semantics']['tone'] != b['semantics']['tone'] or a['semantics']['kind'] != b['semantics']['kind']:
        return False
    if set(a.get('tickers',[])) != set(b.get('tickers',[])):
        return False
    ta = datetime.fromisoformat(a.get('published_at') or a['first_seen_at'])
    tb = datetime.fromisoformat(b.get('published_at') or b['first_seen_at'])
    if abs((ta-tb).total_seconds()) > 86400:
        return False
    if aw == bw:
        return True
    common = len(aw & bw)
    return common >= 4 and common/max(1,len(aw|bw)) >= .72


def group_events(items):
    groups = []
    for raw in sorted(items,key=lambda r:(r['first_seen_at'],r['id'])):
        item = deepcopy(raw)
        item['semantics'] = analyze(item['title'],item.get('official',False))
        matched = next((g for g in groups if same_event(g['members'][0],item)),None)
        if matched is None:
            groups.append({**item,'event_id':sha256(('event:'+item['id']).encode()).hexdigest()[:24],'members':[item]})
        else:
            matched['members'].append(item)
            # Prefer a direct official source as the displayed headline, but
            # keep the group's first-observed ID and every original timestamp.
            if item.get('official') and not matched.get('official'):
                for key in ('title','url','source','source_name','source_home','official','semantics','published_at','observed_at','author','license_url'):
                    matched[key] = item.get(key)
    for g in groups:
        g['publications'] = len(g['members'])
        g['publisher_count'] = len({m['source'] for m in g['members']})
        g['confirmation_note'] = 'Число публикаций не равно числу независимых подтверждений.'
    return sorted(groups,key=lambda g:g.get('published_at') or g['first_seen_at'],reverse=True)
