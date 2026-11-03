"""Local GET-only access to the fixed historical timing experiment."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .close_timing_service import payload


@require_GET
def report(request):
    if request.META.get('REMOTE_ADDR') not in ('127.0.0.1', '::1'):
        result, code = {'status': 'forbidden', 'entry_allowed': False,
                        'message': 'Историческое исследование доступно только локально.'}, 403
    else:
        result, code = payload(), 200
    response = JsonResponse(result, status=code)
    response['Cache-Control'] = 'no-store'
    return response
