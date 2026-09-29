"""T-Bank SDK adapter, bounded read-only requests and explicitly synthetic demo data."""
import hashlib
import math
import os
import random
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from .signals import Candle, MSK


class MarketUnavailable(Exception):
    pass


def number(q):
    return q.units + q.nano / 1_000_000_000


def open_client():
    token = os.getenv("INVEST_TOKEN", "").strip()
    if not token:
        raise MarketUnavailable("В этой копии проекта не настроен INVEST_TOKEN. Добавьте read-only токен в .env и перезапустите сервер. Учебный режим доступен без токена.")
    try:
        import grpc
        from t_tech.invest import Client
    except ImportError as exc:
        raise MarketUnavailable("Не установлена библиотека t-tech-investments. Установите зависимости из requirements.txt.") from exc
    Details = namedtuple("Details", "method timeout metadata credentials wait_for_ready compression")

    class Deadline(grpc.UnaryUnaryClientInterceptor):
        def intercept_unary_unary(self, continuation, details, request):
            return continuation(Details(details.method, min(details.timeout or 25,25), details.metadata,
                                        details.credentials, False, getattr(details,"compression",None)), request)

    return Client(token, interceptors=[Deadline()], app_name="vorby-pro-research")


def error_message(exc):
    if isinstance(exc, MarketUnavailable): return str(exc)
    # Do not send raw SDK exceptions (metadata may contain credentials) to the browser.
    code = getattr(exc,"code",None)
    if callable(code): code = code()
    name = getattr(code,"name", str(code))
    if "UNAUTHENTICATED" in name: return "Т-Банк отклонил токен: проверьте срок действия и права INVEST_TOKEN."
    if "PERMISSION" in name: return "Т-Банк не разрешил запрос для этого токена или инструмента."
    if "RESOURCE_EXHAUSTED" in name: return "Лимит запросов Т-Банка. Обновление повторится через минуту."
    return "Т-Банк сейчас недоступен: проверьте подключение, сертификаты и доступ к API. Живой сигнал временно заблокирован."


def resolve(client, ticker):
    response = client.instruments.find_instrument(query=ticker, api_trade_available_flag=True)
    matches = [x for x in response.instruments if x.ticker.upper()==ticker and x.class_code=="TQBR" and x.instrument_type=="share"]
    if not matches:
        raise MarketUnavailable(f"Акция {ticker} в основном режиме TQBR не найдена. Введите биржевой тикер, например SBER.")
    x = matches[0]
    return {"ticker":x.ticker,"uid":x.uid,"name":x.name,"lot":x.lot,"currency":"rub"}


def fetch_candles(client, uid, start, end, interval, step_minutes):
    # Documented per-request window: 15-minute candles <= 1 week; 1 minute <= 1 day.
    result = {}
    cursor = start
    window = timedelta(days=7 if step_minutes==15 else 1)
    while cursor < end:
        until = min(cursor+window,end)
        response = client.market_data.get_candles(instrument_id=uid, from_=cursor, to=until, interval=interval)
        for c in response.candles:
            if c.is_complete and c.time+timedelta(minutes=step_minutes)<=end:
                converted = Candle(c.time,number(c.open),number(c.high),number(c.low),number(c.close),c.volume)
                if converted.close>0 and converted.low>0:
                    result[c.time] = converted
        cursor = until
    return [result[t] for t in sorted(result)]


def live_snapshot(ticker, previous=None, now=None):
    now = now or datetime.now(timezone.utc)
    previous = previous or {}
    with open_client() as client:
        from t_tech.invest.schemas import CandleInterval, TradeSourceType
        instrument = previous.get("instrument") or resolve(client,ticker)
        uid = instrument["uid"]
        refresh = (now-previous.get("candles_fetched", datetime.min.replace(tzinfo=timezone.utc))).total_seconds()>=60
        if not previous.get("candles"):
            candles = fetch_candles(client,uid,now-timedelta(days=30),now,CandleInterval.CANDLE_INTERVAL_15_MIN,15)
        elif refresh:
            new = fetch_candles(client,uid,now-timedelta(hours=2),now,CandleInterval.CANDLE_INTERVAL_15_MIN,15)
            combined = {c.time:c for c in previous["candles"]+new}
            candles = [combined[t] for t in sorted(combined) if t>=now-timedelta(days=30)]
        else:
            candles = previous["candles"]
        day_start = now.astimezone(MSK).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
        if refresh or not previous.get("minutes"):
            minutes = fetch_candles(client,uid,day_start,now,CandleInterval.CANDLE_INTERVAL_1_MIN,1)
        else:
            minutes = previous["minutes"]
        ob = client.market_data.get_order_book(instrument_id=uid,depth=20)
        tape = client.market_data.get_last_trades(instrument_id=uid,from_=now-timedelta(minutes=5),to=now,
                                                 trade_source=TradeSourceType.TRADE_SOURCE_EXCHANGE)
        status = client.market_data.get_trading_status(instrument_id=uid)
    book = {"time":ob.orderbook_ts,"bids":[[number(x.price),x.quantity] for x in ob.bids],
            "asks":[[number(x.price),x.quantity] for x in ob.asks]}
    books = [b for b in previous.get("books",[]) if b["time"]>=now-timedelta(seconds=120)]
    if not books or book["time"]>books[-1]["time"]: books.append(book)
    trades = [{"time":t.time,"quantity":t.quantity,"price":number(t.price),
               "side":"buy" if t.direction.name=="TRADE_DIRECTION_BUY" else "sell" if t.direction.name=="TRADE_DIRECTION_SELL" else "unknown"} for t in tape.trades]
    return {"instrument":instrument,"candles":candles,"minutes":minutes,"trades":trades,"books":books,
            "trading":status.trading_status.name=="SECURITY_TRADING_STATUS_NORMAL_TRADING" and status.api_trade_available_flag,
            "trading_status":status.trading_status.name,"candles_fetched":now if refresh else previous["candles_fetched"]}


DEMO_NAMES = {"SBER":"Сбербанк", "GAZP":"Газпром", "LKOH":"Лукойл", "YDEX":"Яндекс", "T":"Т-Технологии", "ROSN":"Роснефть"}


def demo_snapshot(ticker, now=None):
    """Synthetic fixtures, never passed off as market observations."""
    now = now or datetime.now(timezone.utc)
    seed = int(hashlib.sha256(ticker.encode()).hexdigest()[:8],16)
    rng = random.Random(seed)
    base = {"SBER":310,"GAZP":135,"LKOH":6900,"YDEX":4500,"T":3200,"ROSN":510}.get(ticker,250)
    end = now.replace(minute=now.minute//15*15,second=0,microsecond=0)
    candles, value = [], base*.94
    for i in range(600):
        drift = .0005 if ticker=="SBER" and i>570 else .00015*math.sin(i/28)
        change = rng.gauss(drift,.0018)
        close = value*(1+change)
        c = Candle(end-timedelta(minutes=15*(600-i)),value,max(value,close)*1.0008,min(value,close)*.9992,close,rng.randint(900,1800))
        candles.append(c)
        value = close
    if ticker=="SBER":
        c=candles[-1]
        candles[-1]=Candle(c.time,c.open,max(c.high,c.open*1.005),c.low,c.open*1.005,4000)
        value=candles[-1].close
    day_start = now.astimezone(MSK).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    count=min(int((now-day_start).total_seconds()//60),600)
    minutes=[]
    for i in range(count):
        p=value*(.995+.005*i/max(1,count))
        minutes.append(Candle(now.replace(second=0,microsecond=0)-timedelta(minutes=count-i),p,p*1.0002,p*.9998,p,100))
    books=[]
    for age in (75,50,25,0):
        books.append({"time":now-timedelta(seconds=age),"bids":[[value*(1-.0002-i*.0003),900-i*60] for i in range(8)],
                      "asks":[[value*(1+.0002+i*.0003),550+i*30] for i in range(8)]})
    trades=[{"time":now-timedelta(seconds=i*4),"quantity":10+i%9,"price":value,"side":"buy" if i%10<7 else "sell"} for i in range(60)]
    return {"instrument":{"ticker":ticker,"name":DEMO_NAMES.get(ticker,"Учебный инструмент"),"lot":10,"currency":"rub","uid":"demo-"+ticker},
            "candles":candles,"minutes":minutes,"books":books,"trades":trades,"trading":True,"trading_status":"DEMO","candles_fetched":now}
