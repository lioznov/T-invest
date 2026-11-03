"""Local GET-only delivery of the historical multi-year research."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .multiyear_service import payload


@require_GET
def report(request):
    if request.META.get('REMOTE_ADDR') not in ('127.0.0.1', '::1'):
        result, code = {'status': 'forbidden', 'entry_allowed': False,
                        'message': 'Исследовательский отчёт доступен только локально.'}, 403
    else:
        result, code = payload(), 200
    response = JsonResponse(result, status=code)
    response['Cache-Control'] = 'no-store'
    return response
