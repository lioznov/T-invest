"""Local read-only and calculation endpoints for the next-morning model."""
import json
import re
import sqlite3

from django.core.management.base import CommandError
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from .morning_evidence import load_replay
from .morning_service import forecast_payload, load_artifact


def _response(result, code=200):
    response = JsonResponse(result, status=code)
    response['Cache-Control'] = 'no-store'
    return response


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
            if result.get("trained_at"):
                artifact = load_artifact()
                if artifact and artifact["trained_at"] == result["trained_at"]:
                    replay = load_replay(artifact)
                    if replay is not None:
                        result["replay"] = replay
    return _response(result, code)


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def forecast_job(request):
    from . import morning_job

    if request.META.get('REMOTE_ADDR') not in ('127.0.0.1', '::1'):
        return _response({'status': 'error', 'message': 'Расчёт доступен в локальной копии сайта.'}, 403)
    action = 'forecast'
    if request.method == 'POST':
        try:
            if request.content_type != 'application/json' or len(request.body) > 1024:
                raise ValueError('Expected small JSON request')
            data = json.loads(request.body)
            if not isinstance(data, dict) or data.keys() - {'ticker', 'action'}:
                raise ValueError('Unexpected request data')
            ticker, action = data.get('ticker'), data.get('action', action)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return _response({'status': 'error', 'message': 'Не удалось прочитать запрос. Выбери акцию ещё раз.'}, 400)
    else:
        ticker = request.GET.get('ticker', 'SBER')
    if not isinstance(ticker, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,14}', ticker.strip().upper()):
        return _response({'status': 'error', 'message': 'Укажи тикер акции, например SBER.'}, 400)
    ticker = ticker.strip().upper()
    if action not in ('forecast', 'compare'):
        return _response({'status': 'error', 'message': 'Выбери расчёт или сравнение.'}, 400)
    if ticker not in morning_job.model.TICKERS:
        return _response({'status': 'error', 'ticker': ticker,
                          'message': 'Модель рассчитана для SBER, GAZP, LKOH, NVTK, GMKN, ROSN и YDEX.'}, 422)
    try:
        if request.method == 'GET':
            return _response(morning_job.state(ticker))
        return _response(*morning_job.start(ticker, action))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, CommandError):
        return _response({'status': 'error', 'ticker': ticker,
                          'message': 'Не удалось прочитать сохранённый расчёт. Попробуй позже.'}, 503)
