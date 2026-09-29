"""Bounded background polling; HTTP requests never wait for broker calls."""
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from django.conf import settings
from .market_provider import demo_snapshot, live_snapshot, error_message
from .signals import analyse, backtest, valid_book
from .history_archive import load_long_history

LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="market-reader")
ENTRIES = {}
TTL = 25
LIMIT = 40
SIGNAL_VERSION = "six_rules_v1"


def archive(ticker, snapshot, analysis):
    directory = Path(settings.BASE_DIR)/"data"
    directory.mkdir(exist_ok=True)
    with closing(sqlite3.connect(directory/"market.sqlite3", timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS observations (ticker TEXT, at TEXT, payload TEXT, PRIMARY KEY(ticker,at))")
        db.execute("CREATE TABLE IF NOT EXISTS paper_observations (ticker TEXT, at TEXT, payload TEXT, PRIMARY KEY(ticker,at))")
        book = snapshot.get("books", [])[-1] if snapshot.get("books") else {}
        ready = (snapshot.get("trading", False) and valid_book(book)
                 and not any("Недостаточно свежих рыночных данных" in reason
                             for reason in analysis["blockers"]))
        payload = {"book":book,"trades":snapshot.get("trades", []),
                   "decision":analysis["decision"],"score":analysis["score"],
                   "review_ready":bool(ready), "strategy_version":SIGNAL_VERSION,
                   "plan":analysis.get("plan"), "instrument":snapshot.get("instrument", {})}
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)", (ticker,analysis["updated_at"],json.dumps(payload,default=lambda x:x.isoformat())))
        compact = {key:value for key,value in payload.items() if key != "trades"}
        compact["book"] = {"time":book.get("time"), "bids":book.get("bids", [])[:1], "asks":book.get("asks", [])[:1]}
        db.execute("INSERT OR IGNORE INTO paper_observations VALUES (?,?,?)", (ticker,analysis["updated_at"],json.dumps(compact,default=lambda x:x.isoformat())))
        db.execute("DELETE FROM observations WHERE julianday(at) < julianday('now','-30 days')")
        db.execute("DELETE FROM paper_observations WHERE julianday(at) < julianday('now','-180 days')")


def update(key):
    mode,ticker = key
    with LOCK:
        previous = ENTRIES[key].get("snapshot")
    try:
        snapshot = demo_snapshot(ticker) if mode=="demo" else live_snapshot(ticker,previous)
        analysis = analyse(snapshot)
        if mode=="live":
            try:
                archive(ticker,snapshot,analysis)
            except (OSError,sqlite3.Error):
                analysis["blockers"].append("Не удалось сохранить наблюдения на диск. История стакана не записана.")
                analysis["decision"] = "WAIT"
                analysis["plan"] = None
        response={"status":"ok","mode":mode,"instrument":snapshot["instrument"],"analysis":analysis,
                  "source":"Синтетические учебные данные" if mode=="demo" else "Т-Банк · TQBR", "trading_status":snapshot["trading_status"]}
        with LOCK:
            ENTRIES[key].update(snapshot=snapshot,response=response,pending=False,finished=time.monotonic())
    except Exception as exc:
        with LOCK:
            ENTRIES[key].update(response={"status":"error","mode":mode,"ticker":ticker,"message":error_message(exc)},pending=False,finished=time.monotonic())


def get_signal(mode,ticker):
    key=(mode,ticker)
    with LOCK:
        if key not in ENTRIES:
            if len(ENTRIES)>=LIMIT:
                idle = [k for k,v in ENTRIES.items() if not v.get("pending")]
                if not idle: return {"status":"loading","ticker":ticker,"message":"Очередь занята, повторим запрос."}
                oldest=min(idle,key=lambda k:ENTRIES[k].get("access",0))
                del ENTRIES[oldest]
            ENTRIES[key]={"finished":0,"pending":False}
        entry=ENTRIES[key]
        entry["access"]=time.monotonic()
        age=time.monotonic()-entry["finished"]
        retry_after=60 if entry.get("response",{}).get("status")=="error" else TTL
        if not entry["pending"] and age>=retry_after:
            entry["pending"]=True
            POOL.submit(update,key)
        response=entry.get("response")
        if not response:
            return {"status":"loading","ticker":ticker,"message":"Получаем свечи, сделки и стакан…"}
        if age>=TTL:
            if response["status"]=="error": return response
            # A stale LONG is hidden immediately. A non-actionable WAIT may remain
            # visible while its replacement loads, avoiding a blank chart every 30 s.
            if response.get("analysis",{}).get("decision") in ("LONG","EXIT"):
                return {"status":"loading","ticker":ticker,"message":"Проверяем, сохраняются ли условия сигнала…"}
            return {**response,"refreshing":True}
        return response


def get_backtest(mode,ticker,fee,slip,history="current"):
    saved_at = None
    if history == "archive":
        if mode != "live":
            return {"status":"unavailable", "message":"Сохранённая рыночная история доступна в режиме «Рынок»."}
        dataset = load_long_history(ticker)
        if not dataset:
            return {"status":"insufficient", "message":"Для этой акции ещё нет сохранённой длинной истории. Выбери текущие свечи."}
        candles, saved_at = dataset['candles'], dataset['saved_at']
    else:
        key=(mode,ticker)
        with LOCK:
            entry=ENTRIES.get(key,{})
            if entry.get("response",{}).get("status")!="ok":
                return {"status":"loading","message":"Сначала дождитесь загрузки данных акции."}
            candles=list(entry["snapshot"]["candles"])
    result=backtest(candles,fee,slip)
    result.update(mode=mode,ticker=ticker,history_source=history,dataset_saved_at=saved_at,
                  dataset_end=candles[-1].time.isoformat() if candles else None)
    return result
