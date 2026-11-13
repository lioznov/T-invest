import json
import sqlite3
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from .news_views import local_request
from .radar_vk import Store, NAMES


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def alerts(request):
    if not local_request(request):
        return JsonResponse({'message': 'Личный бот доступен только с localhost.'}, status=403)
    try:
        scope = request.GET.get('scope', '')
        if scope not in NAMES:
            raise ValueError('Неизвестный радар.')
        store = Store()
        if request.method == 'POST':
            if len(request.body) > 1024:
                raise ValueError('Слишком большой запрос.')
            body = json.loads(request.body)
            if not isinstance(body, dict) or set(body) != {'enabled'}:
                raise ValueError('Можно менять только галочку ВК. Получатель задаётся в .env.')
            result = store.set_enabled(scope, body['enabled'])
        else:
            result = store.status(scope)
    except (ValueError, UnicodeError):
        return JsonResponse({'message': 'Проверь радар, положение галочки и настройку ВК в .env.'}, status=400)
    except (OSError, sqlite3.Error):
        return JsonResponse({'message': 'Настройки ВК недоступны. Проверь папку data.'}, status=503)
    response = JsonResponse(result)
    response['Cache-Control'] = 'no-store'
    return response
