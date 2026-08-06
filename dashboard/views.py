import collections
import os
import statistics
import time
import urllib.parse
import urllib.request
import certifi
import threading
import asyncio

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from dotenv import load_dotenv

from t_tech.invest import (
    AsyncClient,
    MarketDataRequest,
    SubscribeOrderBookRequest,
    SubscriptionAction,
    OrderBookInstrument
)
from t_tech.invest.utils import quotation_to_decimal

os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certifi.where()
env_path = os.path.join(settings.BASE_DIR, '.env')
load_dotenv(env_path)

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INVEST_TOKEN = os.getenv("INVEST_TOKEN")

def send_telegram_message(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode("utf-8")
    try: urllib.request.urlopen(url, data=data)
    except: pass

# ==========================================
# ФОНОВЫЙ РОБОТ (РАБОТАЕТ КАК В КОНСОЛИ)
# ==========================================
GLOBAL_DATA = {}
ACTIVE_FIGI = None
STREAM_THREAD = None
LOT_SIZE = 10
ANOMALY_MULTIPLIER = 3.0

def start_stream_in_thread(figi):
    """Запускает асинхронный цикл в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(stream_data(figi))

async def stream_data(figi):
    """Сам робот, который качает поток Т-Банка в глобальный словарь"""
    global GLOBAL_DATA, ACTIVE_FIGI
    
    history_bids = collections.deque(maxlen=30)
    history_asks = collections.deque(maxlen=30)
    last_tg_alert = 0
    last_calib_time = 0
    
    try:
        async with AsyncClient(INVEST_TOKEN) as client:
            async def request_iterator():
                yield MarketDataRequest(
                    subscribe_order_book_request=SubscribeOrderBookRequest(
                        subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                        instruments=[OrderBookInstrument(figi=figi, depth=10)]
                    )
                )
                while True:
                    await asyncio.sleep(1)

            async for marketdata in client.market_data_stream.market_data_stream(request_iterator()):
                if ACTIVE_FIGI != figi:
                    break # Если пользователь сменил акцию - выключаем этот поток
                    
                if marketdata.orderbook:
                    ob = marketdata.orderbook
                    current_time = time.time()

                    bids = [{"price": float(quotation_to_decimal(b.price)), "quantity": b.quantity} for b in ob.bids[:10]]
                    asks = [{"price": float(quotation_to_decimal(a.price)), "quantity": a.quantity} for a in ob.asks[:10]]

                    cur_bid_rub = sum(b['price'] * b['quantity'] * LOT_SIZE for b in bids)
                    cur_ask_rub = sum(a['price'] * a['quantity'] * LOT_SIZE for a in asks)

                    if current_time - last_calib_time >= 1.0:
                        history_bids.append(cur_bid_rub)
                        history_asks.append(cur_ask_rub)
                        last_calib_time = current_time

                    is_calib = len(history_bids) < 30
                    anomaly_msg = ""
                    is_anomaly = False

                    if not is_calib:
                        med_bid = statistics.median(history_bids)
                        med_ask = statistics.median(history_asks)

                        if cur_bid_rub > (med_bid * ANOMALY_MULTIPLIER):
                            anomaly_msg = f"🟢 КАСКАД ПОКУПОК! {cur_bid_rub / 1_000_000:.1f}М ₽"
                            is_anomaly = True
                        elif cur_ask_rub > (med_ask * ANOMALY_MULTIPLIER):
                            anomaly_msg = f"🔴 КАСКАД ПРОДАЖ! {cur_ask_rub / 1_000_000:.1f}М ₽"
                            is_anomaly = True

                        if is_anomaly and (current_time - last_tg_alert > 60):
                            await asyncio.to_thread(send_telegram_message, f"🚨 <b>АНОМАЛИЯ!</b>\nАкция: {figi}\n{anomaly_msg}")
                            last_tg_alert = current_time

                    cur_price = (bids[0]['price'] + asks[0]['price']) / 2 if bids and asks else 0

                    # МОМЕНТАЛЬНО ЗАПИСЫВАЕМ В ПАМЯТЬ СЕРВЕРА
                    GLOBAL_DATA[figi] = {
                        "status": "ok",
                        "bids": bids, "asks": asks,
                        "is_calibrating": is_calib, "anomaly_msg": anomaly_msg, "is_anomaly": is_anomaly,
                        "total_bid_rubles": cur_bid_rub, "total_ask_rubles": cur_ask_rub,
                        "total_volume": cur_bid_rub + cur_ask_rub, "current_price": cur_price
                    }
    except Exception as e:
        GLOBAL_DATA[figi] = {"status": "error", "message": f"Ошибка потока: {e}"}

# ==========================================
# ОТДАЧА ДАННЫХ В БРАУЗЕР (БЫСТРО КАК МОЛНИЯ)
# ==========================================
def real_market_page(request):
    return render(request, "real_market.html")

def api_real_data(request):
    global ACTIVE_FIGI, STREAM_THREAD
    
    if not INVEST_TOKEN:
        return JsonResponse({"status": "error", "message": "Токен INVEST_TOKEN не найден!"})
        
    figi = request.GET.get('figi', 'BBG004730N88')
    
    # Если запрашиваем новую акцию - запускаем фонового робота
    if ACTIVE_FIGI != figi:
        ACTIVE_FIGI = figi
        GLOBAL_DATA[figi] = {"status": "loading", "message": "Подключение к бирже..."}
        STREAM_THREAD = threading.Thread(target=start_stream_in_thread, args=(figi,), daemon=True)
        STREAM_THREAD.start()
        
    # Моментально отдаем то, что лежит в оперативной памяти!
    data = GLOBAL_DATA.get(figi, {"status": "loading", "message": "Инициализация..."})
    return JsonResponse(data)