"""Bounded, local-only request queue for automatic morning estimates."""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from .auto_morning import HISTORY_DAYS, calculate
from .market_provider import MarketUnavailable, error_message

logger = logging.getLogger(__name__)
lock = Lock()
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="morning-estimate")
active = {}
stages = {"loading", "analyzing", "forecasting"}


def cached_history(uid, midnight, fetch):
    key = f"morning_history_v1:{uid}:{midnight.date()}"
    value = cache.get(key)
    if value is None:
        value = fetch(midnight - timedelta(days=HISTORY_DAYS), midnight)
        if value:
            cache.set(key, value, 3600)
    return value


def run(ticker, started):
    def progress(stage):
        if stage in stages:
            with lock:
                active[ticker] = stage

    try:
        forecast = calculate(ticker, started, progress, cached_history)
        now = datetime.now(timezone.utc)
        if datetime.fromisoformat(forecast["target_at"]) <= now:
            raise MarketUnavailable("Время прогноза уже прошло. Нажми «Анализировать» ещё раз.")
        result = {"status": "ok", "forecast": forecast}
    except MarketUnavailable as exc:
        message = str(exc)
        if "INVEST_TOKEN" in message:
            message = "Нет подключения к Т-Банку. Подключи рыночные данные и попробуй снова."
        result = {"status": "error", "message": message}
    except (ValueError, ArithmeticError, KeyError, TypeError):
        result = {"status": "error", "message": "Данные для прогноза не прошли проверку. Попробуй позже."}
    except Exception as exc:
        logger.warning("Morning market request failed: %s", type(exc).__name__)
        message = error_message(exc)
        if message.startswith("Т-Банк сейчас недоступен"):
            message = "Не удалось загрузить данные Т-Банка. Попробуй снова."
        result = {"status": "error", "message": message}

    with lock:
        cache.set(f"auto_morning_v1:{ticker}", result, 900)
        active.pop(ticker, None)


def response(ticker, result, status=200):
    return JsonResponse({"ticker": ticker, **result}, status=status, headers={"Cache-Control": "no-store"})


@require_http_methods(["GET", "POST"])
def morning(request):
    if request.META.get("REMOTE_ADDR") not in ("127.0.0.1", "::1"):
        return response("", {"status": "error", "message": "Прогноз доступен в локальной копии сайта."}, 403)
    ticker = request.GET.get("ticker", "")
    if request.method == "POST":
        try:
            if request.content_type != "application/json" or len(request.body) > 1024:
                raise ValueError
            payload = json.loads(request.body)
            ticker = payload["ticker"] if isinstance(payload, dict) else None
        except (ValueError, KeyError, UnicodeDecodeError, RecursionError):
            ticker = None
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", ticker):
        return response("", {"status": "error", "message": "Введи биржевой тикер."}, 400)
    key = f"auto_morning_v1:{ticker}"
    with lock:
        if ticker in active:
            return response(ticker, {"status": "running", "stage": active[ticker]}, 202)
        if request.method == "GET":
            return response(ticker, cache.get(key) or {"status": "idle"})
        if len(active) >= 2:
            return response(ticker, {"status": "error", "message": "Идёт расчёт другой акции. Попробуй чуть позже."}, 429)
        cache.delete(key)
        active[ticker] = "loading"
        try:
            pool.submit(run, ticker, datetime.now(timezone.utc))
        except RuntimeError:
            active.pop(ticker, None)
            return response(ticker, {"status": "error", "message": "Не удалось запустить расчёт. Попробуй снова."}, 503)
    return response(ticker, {"status": "running", "stage": "loading"}, 202)
