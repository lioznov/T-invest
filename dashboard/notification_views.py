"""One local settings surface over the existing analysis and radar stores."""
import json
import sqlite3
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from .news_views import local_request
from .telegram_alerts import Store as TelegramAnalysis, ensure_worker as ensure_telegram
from .vk_alerts import Store as VKAnalysis, ensure_worker as ensure_vk
from .radar_vk import Store as VKRadar, TelegramStore as TelegramRadar

MODES = {'analysis-single':('analysis','single'), 'analysis-multi':('analysis','multi'),
         'radar-single':('radar','single'), 'radar-multi':('radar','multi')}


def stores(kind):
    return {'vk':VKAnalysis() if kind=='analysis' else VKRadar(),
            'telegram':TelegramAnalysis() if kind=='analysis' else TelegramRadar()}


def state(kind, scope, channels):
    result={}
    for channel,store in channels.items():
        if kind=='analysis':
            status=store.status(scope=scope); selected=store.mode_config(scope)
            items=[r for r in status['items'] if r['ticker'] in selected['tickers']]
            attempts=[r for r in items if r['delivery_detail']]
            latest=max(attempts,key=lambda r:r['checked']) if attempts else {}
            result[channel]={**selected,'configured':status['configured'],
                'worker_running':status['worker_running'], 'collector':status['collector'],
                'items':items, 'result':latest.get('result',''),
                'message':(latest.get('ticker','')+' · '+latest.get('delivery_detail','')).strip(' ·') or 'Сигналов анализа пока не отправляли.'}
        else:
            result[channel]=store.status(scope)
        result[channel]['setup'] = ('Нужны VK_COMMUNITY_TOKEN и VK_USER_ID; разреши сообщения от своего сообщества.' if channel=='vk'
            else 'Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID; открой диалог с ботом и нажми «Старт».')+' После изменения .env перезапусти сервер.'
    if kind=='analysis':
        if channels['telegram'].config()['enabled']:ensure_telegram()
        if channels['vk'].config()['enabled']:ensure_vk()
    return result


@ensure_csrf_cookie
@require_http_methods(['GET','POST'])
def settings(request):
    if not local_request(request):
        return JsonResponse({'message':'Настройки личных уведомлений доступны только с localhost.'},status=403)
    try:
        mode=request.GET.get('mode','')
        if mode not in MODES:raise ValueError('Неизвестный режим уведомлений.')
        kind,scope=MODES[mode]; channels=stores(kind)
        test=None
        if request.method=='POST':
            if len(request.body)>1024*1024:raise ValueError('Слишком большой запрос.')
            body=json.loads(request.body)
            if not isinstance(body,dict) or body.get('channel') not in channels:raise ValueError('Выбери канал уведомлений.')
            channel=body['channel']
            if body.get('action')=='test':
                if set(body)!={'channel','action'}:raise ValueError('Получатель задаётся только в .env.')
                # Reuse the existing manual test and its persisted per-channel rate limit.
                test=(VKAnalysis() if channel=='vk' else TelegramAnalysis()).test_message()
            else:
                allowed={'channel','enabled','tickers'} if kind=='analysis' else {'channel','enabled'}
                if set(body)!=allowed:raise ValueError('Некорректные настройки. Ключи и получателей здесь менять нельзя.')
                if kind=='analysis':channels[channel].save_mode(scope,body['enabled'],body['tickers'])
                else:channels[channel].set_enabled(scope,body['enabled'])
        result={'mode':mode,'channels':state(kind,scope,channels)}
        if test is not None:result['test']=test
    except (ValueError,UnicodeError) as exc:
        return JsonResponse({'message':'Некорректный JSON.' if isinstance(exc,json.JSONDecodeError) else str(exc)},status=400)
    except (OSError,sqlite3.Error):
        return JsonResponse({'message':'Хранилище уведомлений недоступно. Проверь папку data.'},status=503)
    response=JsonResponse(result);response['Cache-Control']='no-store'
    return response
