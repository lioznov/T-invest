import math
import os
import re
import time
from functools import lru_cache
from django.utils import timezone
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from .signal_service import get_signal, get_backtest
from .forward_review import load_review
from .paper_journal import load_paper_journal
from .risk import position_size


@lru_cache(maxsize=96)
def _helper_evidence(ticker, bucket):
    """Public local reports only; no extra broker or paid AI requests."""
    from .neural_service import model_status
    import sqlite3
    def optional(reader):
        try:
            return reader()
        except (OSError,ValueError,sqlite3.Error):
            return {'status':'unavailable'}
    return optional(model_status),optional(lambda:load_paper_journal(ticker)),optional(lambda:load_review(ticker))


def page(request, watchlist=False):
    return render(request,"signals.html",{"watchlist":watchlist,"has_token":bool(os.getenv("INVEST_TOKEN"))})


@require_GET
def monitor_health(request):
    from pathlib import Path
    from django.conf import settings
    from .continuous_watch import load_health
    result = load_health(Path(settings.BASE_DIR)/"data"/"watch")
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store"
    return response


def arguments(request):
    mode=request.GET.get("mode","live")
    ticker=request.GET.get("ticker","SBER").strip().upper()
    if mode not in ("live","demo") or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}",ticker):
        raise ValueError("Укажите корректный тикер и режим данных.")
    return mode,ticker


@require_GET
def signal(request):
    try:
        mode,ticker=arguments(request)
    except ValueError as exc:
        return JsonResponse({"status":"error","message":str(exc)},status=400)
    from .ai_service import expire_payload
    from .structure_research import expire_structure_payload
    from .technical_context import expire_payload as expire_technical
    payload=expire_technical(expire_structure_payload(expire_payload(get_signal(mode,ticker))))
    from .trading_research_helper import summarize_research
    model=paper_report=forward_report=None
    if mode=='live' and payload.get('status')=='ok':
        model,paper_report,forward_report=_helper_evidence(ticker,int(time.monotonic()//30))
    payload={**payload,'research_helper':summarize_research(payload,now=timezone.now(),
        model_report=model,paper_report=paper_report,forward_report=forward_report)}
    response=JsonResponse(payload)
    response["Cache-Control"]="no-store"
    return response


@require_GET
def ai_model(request):
    from .ai_service import model_status
    from .neural_service import model_status as neural_status
    from .technical_research import load_report as channel_report
    response=JsonResponse({**model_status(),'neural':neural_status(),'technical_research':channel_report()})
    response["Cache-Control"]="no-store"
    return response


@require_GET
def research(request):
    try:
        mode,ticker=arguments(request)
        fee=float(request.GET.get("fee","5"))
        slip=float(request.GET.get("slip","5"))
        history=request.GET.get("history","current")
        if history not in ("current", "archive"):
            raise ValueError("Выбери текущие свечи или сохранённую историю.")
        if not all(math.isfinite(x) and 0<=x<=100 for x in (fee,slip)):
            raise ValueError("Расходы должны быть от 0 до 100 базисных пунктов.")
    except ValueError as exc:
        return JsonResponse({"status":"error","message":str(exc)},status=400)
    response=JsonResponse(get_backtest(mode,ticker,fee,slip,history))
    response["Cache-Control"]="no-store"
    return response


@require_GET
def forward(request):
    try:
        mode,ticker=arguments(request)
        fee=float(request.GET.get("fee","5"))
        slip=float(request.GET.get("slip","5"))
        if not all(math.isfinite(x) and 0<=x<=100 for x in (fee,slip)):
            raise ValueError("Расходы должны быть от 0 до 100 базисных пунктов.")
    except ValueError as exc:
        return JsonResponse({"status":"error","message":str(exc)},status=400)
    result=load_review(ticker,fee,slip) if mode=="live" else {
        "status":"unavailable","message":"Проверка будущих котировок доступна только для реального рынка."}
    response=JsonResponse(result)
    response["Cache-Control"]="no-store"
    return response


@require_GET
def paper(request):
    try:
        mode, ticker = arguments(request)
        fee = float(request.GET.get("fee", "5"))
        slip = float(request.GET.get("slip", "5"))
        if not all(math.isfinite(x) and 0 <= x <= 100 for x in (fee, slip)):
            raise ValueError("Расходы должны быть от 0 до 100 базисных пунктов.")
    except ValueError as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)
    result = load_paper_journal(ticker, fee, slip) if mode == "live" else {
        "status": "unavailable", "message": "Журнал собирается по реальным наблюдениям. Учебные цены в него не попадают."}
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def risk(request):
    try:
        result = position_size(
            capital=request.GET.get("capital"), risk_pct=request.GET.get("risk_pct"),
            allocation_pct=request.GET.get("allocation_pct"), entry=request.GET.get("entry"),
            stop=request.GET.get("stop"), target=request.GET.get("target"), lot=request.GET.get("lot"),
            fee_bps=request.GET.get("fee", "5"), slippage_bps=request.GET.get("slip", "5"))
    except ValueError as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def pattern_research(request):
    try:
        mode, ticker = arguments(request)
    except ValueError as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)
    from .pattern_research import load_pattern_research
    result = load_pattern_research(ticker) if mode == "live" else {
        "status": "unavailable", "message": "Учебные паттерны не являются результатами проверки на реальном рынке."}
    if mode == "live":
        from .pattern_flow_research import load_pattern_flow_research
        result["historical_flow"] = load_pattern_flow_research(ticker)
        from .candle_pattern_research import load_candle_research
        result["candlestick_v3"] = load_candle_research(ticker)
        from .pressure_research import load_pressure_research
        result["stream_pressure"] = load_pressure_research(ticker)
        from .range_research import load_range_research
        result["range_swing"] = load_range_research(ticker)
        from .trader_tactics_research import load_trader_research
        result["trader_tactics"] = load_trader_research(ticker)
        from .joint_research import load_joint_research
        result["joint_factors"] = load_joint_research(ticker)
        from .monthly_research import load_monthly_research
        result["monthly_momentum"] = load_monthly_research()
        from .opening_activity_research import load_opening_research
        result["opening_activity"] = load_opening_research(ticker)
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store"
    return response
