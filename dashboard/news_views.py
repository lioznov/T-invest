import re
import sqlite3
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .news_sources import SOURCES
from .news_feed import TOPICS, ALIASES
from .news_store import payload
from .news_analysis import TONES,KINDS


def local_request(request):
    # Personal-use feeds must not become a public relay by changing ALLOWED_HOSTS.
    # Do not trust a client-supplied X-Forwarded-For header.
    return request.META.get('REMOTE_ADDR') in ('127.0.0.1','::1')


@require_GET
def page(request):
    if not local_request(request):
        return HttpResponseForbidden('Лента предназначена только для личного чтения на localhost.')
    return render(request,'news.html',{'sources':[s.public() for s in SOURCES.values()],
                                    'topics':TOPICS,'tickers':sorted(ALIASES),'tones':TONES,'kinds':KINDS})


@require_GET
def feed(request):
    if not local_request(request):
        return JsonResponse({'status':'forbidden','message':'Новостная лента доступна только локально.'},status=403)
    ticker = request.GET.get('ticker','').strip().upper()
    source,topic = request.GET.get('source',''),request.GET.get('topic','')
    query = request.GET.get('q','').strip()
    tone,kind = request.GET.get('tone',''),request.GET.get('kind','')
    if ((ticker and not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,14}',ticker))
            or source and source not in SOURCES or topic and topic not in TOPICS or len(query)>100
            or request.GET.get('macro','0') not in ('0','1') or tone and tone not in TONES or kind and kind not in KINDS):
        return JsonResponse({'status':'error','message':'Проверь фильтры новостей.'},status=400)
    try:
        result = payload(ticker=ticker,source=source,topic=topic,query=query,
                         include_macro=request.GET.get('macro','0') == '1',market_only=True,tone=tone,kind=kind)
    except (OSError,ValueError,sqlite3.Error):
        result = {'status':'unavailable','items':[],'sources':[],
                  'message':'Локальное хранилище новостей недоступно. Проверь права на папку data.',
                  'entry_allowed':False}
    response = JsonResponse(result)
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def calendar(request):
    if not local_request(request):
        return JsonResponse({'status':'forbidden'},status=403)
    from .news_context import ContextStore,schedule,TICKERS
    from datetime import datetime,timezone
    ticker = request.GET.get('ticker','').upper()
    if ticker and ticker not in TICKERS:
        return JsonResponse({'status':'unsupported','events':[],'entry_allowed':False,
                             'message':'Календарь пока подключён для семи исследуемых акций.'})
    try:
        result = ContextStore().calendar(datetime.now(timezone.utc),ticker)
        schedule('calendar',[ticker] if ticker else list(TICKERS))
    except (OSError,ValueError,sqlite3.Error):
        result = {'status':'unavailable','events':[],'entry_allowed':False,
                  'message':'Локальное хранилище календаря недоступно. Даты не подставляются.'}
    response = JsonResponse(result)
    response['Cache-Control']='no-store'
    return response


@require_GET
def reaction(request):
    if not local_request(request):
        return JsonResponse({'status':'forbidden'},status=403)
    from .news_context import reaction_payload,TICKERS
    ticker = request.GET.get('ticker','').upper()
    item_id = request.GET.get('id','')
    if ticker not in TICKERS or not re.fullmatch(r'[a-f0-9]{64}',item_id):
        return JsonResponse({'status':'error','message':'Нужен сохранённый материал и поддерживаемая акция.'},status=400)
    try:
        result = reaction_payload(item_id,ticker)
    except (OSError,ValueError,sqlite3.Error):
        result = {'status':'unavailable','entry_allowed':False,
                  'message':'Наблюдения цены сейчас недоступны. Результат не подставляется.'}
    response = JsonResponse(result,status=404 if result.get('status')=='not_found' else 200)
    response['Cache-Control']='no-store'
    return response
