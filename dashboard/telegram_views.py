import json
import sqlite3
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from .news_views import local_request
from .telegram_alerts import Store, ensure_worker


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def alerts(request):
    if not local_request(request):
        return JsonResponse({'message': 'Настройки бота доступны только с localhost.'}, status=403)
    try:
        store = Store()
        if request.method == 'POST':
            if len(request.body) > 1024*1024:
                raise ValueError('Список превышает защитный размер запроса 1 МБ. Раздели его или сократи повторы.')
            body = json.loads(request.body)
            if not isinstance(body, dict):
                raise ValueError('Некорректные настройки.')
            store.save(body.get('enabled'), body.get('tickers'))
        result = store.status()
        if result['enabled']:
            ensure_worker()
    except (ValueError, UnicodeError) as exc:
        return JsonResponse({'message': str(exc) if not isinstance(exc, json.JSONDecodeError) else 'Некорректный JSON.'}, status=400)
    except (OSError, sqlite3.Error):
        return JsonResponse({'message': 'Хранилище уведомлений недоступно. Проверь папку data.'}, status=503)
    response = JsonResponse(result)
    response['Cache-Control'] = 'no-store'
    return response
