"""Local read-only endpoint for the separate next-morning model."""
import re

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .morning_service import forecast_payload


@require_GET
def forecast(request):
    if request.META.get('REMOTE_ADDR') not in ('127.0.0.1', '::1'):
        result, code = {'status': 'forbidden', 'entry_allowed': False,
                        'message': 'Исследовательский прогноз доступен только локально.'}, 403
    else:
        ticker = request.GET.get('ticker', 'SBER').strip().upper()
        mode = request.GET.get('mode', 'live')
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,14}', ticker) or mode not in ('live', 'demo'):
            result, code = {'status': 'error', 'entry_allowed': False,
                            'message': 'Нужны корректный тикер и режим live или demo.'}, 400
        else:
            result, code = forecast_payload(ticker, mode), 200
    response = JsonResponse(result, status=code)
    response['Cache-Control'] = 'no-store'
    return response
