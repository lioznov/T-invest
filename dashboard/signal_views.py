import math
import os
import re
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from .signal_service import get_signal, get_backtest
from .forward_review import load_review
from .paper_journal import load_paper_journal
from .risk import position_size


def page(request, watchlist=False):
    return render(request,"signals.html",{"watchlist":watchlist,"has_token":bool(os.getenv("INVEST_TOKEN"))})


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
    response=JsonResponse(get_signal(mode,ticker))
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
