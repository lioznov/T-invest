import collections
import os
import statistics
import time
import urllib.parse
import urllib.request
import certifi
import threading
import asyncio
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from dotenv import load_dotenv

from t_tech.invest import (
    Client,
    AsyncClient,
    MarketDataRequest,
    SubscribeOrderBookRequest,
    SubscriptionAction,
    OrderBookInstrument,
    CandleInterval,
    InstrumentIdType
)
from t_tech.invest.utils import quotation_to_decimal

os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certifi.where()
env_path = os.path.join(settings.BASE_DIR, '.env')
load_dotenv(env_path)

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INVEST_TOKEN = os.getenv("INVEST_TOKEN")

LOT_SIZE = 10
ANOMALY_MULTIPLIER = 3.0

# === 1. ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ ОДИНОЧНОГО РАДАРА ===
GLOBAL_DATA = {}
ACTIVE_FIGI = None
STREAM_THREAD = None
BOT_ALERTS_ENABLED = False
PREV_BOT_STATE = False

# === 2. ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ МУЛЬТИ-РАДАРА ===
MULTI_GLOBAL_DATA = {}
TRACKED_FIGIS = set()
TICKER_CACHE = {}
MULTI_STREAM_THREAD = None
MULTI_BOT_ALERTS_ENABLED = False
PREV_MULTI_BOT_STATE = False

# === 3. ФОНОВЫЙ КЭШ ПОИСКА (ДЛЯ ТУРБО-СКОРОСТИ) ===
CACHED_INSTRUMENTS = []
CACHE_READY = False


def _load_cache():
    """Скачивает тикеры один раз при запуске сервера, чтобы поиск работал моментально"""
    global CACHED_INSTRUMENTS, CACHE_READY
    if not INVEST_TOKEN: return
    try:
        with Client(INVEST_TOKEN) as client:
            shares = client.instruments.shares().instruments
            etfs = client.instruments.etfs().instruments
            currencies = client.instruments.currencies().instruments

            temp_cache = []
            for inst in shares + etfs + currencies:
                if getattr(inst, 'api_trade_available_flag', False):
                    temp_cache.append({
                        'figi': inst.figi,
                        'ticker': inst.ticker,
                        'name': inst.name,
                        'type': inst.instrument_type,
                        'class_code': getattr(inst, 'class_code', '')
                    })
            CACHED_INSTRUMENTS = temp_cache
            CACHE_READY = True
            print(f"✅ Кэш поиска загружен: {len(CACHED_INSTRUMENTS)} инструментов.")
    except Exception as e:
        print(f"Ошибка загрузки кэша поиска: {e}")


# Запускаем загрузку кэша в фоне, чтобы не вешать сервер
threading.Thread(target=_load_cache, daemon=True).start()


def send_telegram_message(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode("utf-8")
    try:
        urllib.request.urlopen(url, data=data)
    except:
        pass


# =========================================================================
# === ЛОГИКА ОДИНОЧНОГО РАДАРА (real_market.html) ===
# =========================================================================
def start_stream_in_thread(figi):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(stream_data(figi))


async def stream_data(figi):
    global GLOBAL_DATA, ACTIVE_FIGI, BOT_ALERTS_ENABLED
    history_bids = collections.deque(maxlen=30)
    history_asks = collections.deque(maxlen=30)
    last_tg_alert, last_calib_time = 0, 0
    active_whales = {}

    while ACTIVE_FIGI == figi:
        try:
            async with AsyncClient(INVEST_TOKEN) as client:
                async def request_iterator():
                    yield MarketDataRequest(
                        subscribe_order_book_request=SubscribeOrderBookRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                            instruments=[OrderBookInstrument(figi=figi, depth=50)]
                        )
                    )
                    while ACTIVE_FIGI == figi:
                        await asyncio.sleep(1)
                    yield MarketDataRequest(
                        subscribe_order_book_request=SubscribeOrderBookRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_UNSUBSCRIBE,
                            instruments=[OrderBookInstrument(figi=figi, depth=50)]
                        )
                    )

                async for marketdata in client.market_data_stream.market_data_stream(request_iterator()):
                    if ACTIVE_FIGI != figi: break

                    if marketdata.orderbook:
                        ob = marketdata.orderbook
                        current_time = time.time()

                        bids = [{"price": float(quotation_to_decimal(b.price)), "quantity": b.quantity} for b in
                                ob.bids[:50]]
                        asks = [{"price": float(quotation_to_decimal(a.price)), "quantity": a.quantity} for a in
                                ob.asks[:50]]

                        cur_bid_rub = sum(b['price'] * b['quantity'] * LOT_SIZE for b in bids)
                        cur_ask_rub = sum(a['price'] * a['quantity'] * LOT_SIZE for a in asks)
                        cur_price = (bids[0]['price'] + asks[0]['price']) / 2 if bids and asks else 0

                        if current_time - last_calib_time >= 1.0:
                            history_bids.append(cur_bid_rub)
                            history_asks.append(cur_ask_rub)
                            last_calib_time = current_time

                        is_calib = len(history_bids) < 30
                        bids_dict = {b['price']: b['quantity'] for b in bids}
                        asks_dict = {a['price']: a['quantity'] for a in asks}

                        alerts_to_send = []
                        prices_to_remove = []
                        dynamic_whale_bid, dynamic_whale_ask = 100_000_000, 100_000_000

                        if not is_calib:
                            med_bid = statistics.median(history_bids)
                            med_ask = statistics.median(history_asks)

                            dynamic_whale_bid = max(med_bid * 0.25, 2_000_000)
                            dynamic_whale_ask = max(med_ask * 0.25, 2_000_000)

                            if cur_bid_rub > (med_bid * ANOMALY_MULTIPLIER):
                                alerts_to_send.append(
                                    f"🌊 <b>[ОДИНОЧНЫЙ РАДАР] КАСКАД ПОКУПОК!</b>\nОбъем вырос в 3 раза: {cur_bid_rub / 1_000_000:.1f}М ₽")
                            elif cur_ask_rub > (med_ask * ANOMALY_MULTIPLIER):
                                alerts_to_send.append(
                                    f"🌊 <b>[ОДИНОЧНЫЙ РАДАР] КАСКАД ПРОДАЖ!</b>\nОбъем вырос в 3 раза: {cur_ask_rub / 1_000_000:.1f}М ₽")

                            for price, data in list(active_whales.items()):
                                w_type = data['type']
                                initial_vol = data.get('initial_vol', data['vol'])
                                pre_alert_sent = data.get('pre_alert_sent', False)

                                current_vol = (bids_dict.get(price, 0) if w_type == 'bid' else asks_dict.get(price,
                                                                                                             0)) * price * LOT_SIZE
                                percent_left = (current_vol / initial_vol) * 100 if initial_vol > 0 else 0
                                threshold = dynamic_whale_bid if w_type == 'bid' else dynamic_whale_ask

                                if 0 < percent_left <= 25 and not pre_alert_sent:
                                    alerts_to_send.append(
                                        f"⚠️ <b>[ОДИНОЧНЫЙ РАДАР] ГОТОВЬСЯ! ПРОБОЙ {'ВНИЗ' if w_type == 'bid' else 'ВВЕРХ'}!</b>\n"
                                        f"Плиту на {price} ₽ доедают!\nОсталось: {current_vol / 1_000_000:.1f}М ₽ ({percent_left:.1f}%)"
                                    )
                                    active_whales[price]['pre_alert_sent'] = True

                                if current_vol < threshold:
                                    is_eaten = (cur_price <= price) if w_type == 'bid' else (cur_price >= price)
                                    if pre_alert_sent or is_eaten:
                                        alerts_to_send.append(
                                            f"{'📉' if w_type == 'bid' else '🚀'} <b>[ОДИНОЧНЫЙ РАДАР] ПРОБОЙ {'ВНИЗ' if w_type == 'bid' else 'ВВЕРХ'}!</b>\nПлиту на {price} ₽ сожрали!")
                                    else:
                                        alerts_to_send.append(
                                            f"👻 <b>[ОДИНОЧНЫЙ РАДАР] СПУФИНГ!</b>\nПлита на {price} ₽ пропала.")
                                    prices_to_remove.append(price)

                            for p in prices_to_remove:
                                del active_whales[p]

                            for price, qty in bids_dict.items():
                                vol = price * qty * LOT_SIZE
                                if vol >= dynamic_whale_bid and price not in active_whales:
                                    active_whales[price] = {'type': 'bid', 'vol': vol, 'initial_vol': vol,
                                                            'pre_alert_sent': False}
                                    alerts_to_send.append(
                                        f"🟢 <b>[ОДИНОЧНЫЙ РАДАР] Покупка</b>\nЦена: {price} ₽\nЛоты: {qty} шт.\nСумма: {vol / 1_000_000:.1f}М ₽")

                            for price, qty in asks_dict.items():
                                vol = price * qty * LOT_SIZE
                                if vol >= dynamic_whale_ask and price not in active_whales:
                                    active_whales[price] = {'type': 'ask', 'vol': vol, 'initial_vol': vol,
                                                            'pre_alert_sent': False}
                                    alerts_to_send.append(
                                        f"🔴 <b>[ОДИНОЧНЫЙ РАДАР] Продажа</b>\nЦена: {price} ₽\nЛоты: {qty} шт.\nСумма: {vol / 1_000_000:.1f}М ₽")

                        anomaly_msg, is_anomaly = "", False
                        if alerts_to_send:
                            anomaly_msg = alerts_to_send[0].replace("\n", " ")
                            is_anomaly = True

                            if current_time - last_tg_alert > 10 and BOT_ALERTS_ENABLED:
                                ticker_name = TICKER_CACHE.get(figi, figi)
                                tg_msg = f"🔔 <b>{ticker_name}</b>\n\n" + "\n\n".join(alerts_to_send)
                                await asyncio.to_thread(send_telegram_message, tg_msg)
                                last_tg_alert = current_time

                        GLOBAL_DATA[figi] = {
                            "status": "ok", "bids": bids, "asks": asks,
                            "is_calibrating": is_calib, "anomaly_msg": anomaly_msg, "is_anomaly": is_anomaly,
                            "total_bid_rubles": cur_bid_rub, "total_ask_rubles": cur_ask_rub,
                            "total_volume": cur_bid_rub + cur_ask_rub, "current_price": cur_price,
                            "dynamic_whale_bid": dynamic_whale_bid, "dynamic_whale_ask": dynamic_whale_ask
                        }
        except Exception as e:
            print(f"Ошибка потока (real_market): {e}")
            if ACTIVE_FIGI == figi:
                GLOBAL_DATA[figi] = {"status": "error", "message": "Инструмент не торгуется"}
                await asyncio.sleep(3)


# =========================================================================
# === ЛОГИКА МУЛЬТИ-РАДАРА (radar.html) ===
# =========================================================================
def start_multi_stream_in_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(multi_stream_data())
        except Exception as e:
            print(f"Перезапуск мульти-потока: {e}")
            time.sleep(3)


async def multi_stream_data():
    global MULTI_GLOBAL_DATA, TRACKED_FIGIS, MULTI_BOT_ALERTS_ENABLED
    history_bids = collections.defaultdict(lambda: collections.deque(maxlen=30))
    history_asks = collections.defaultdict(lambda: collections.deque(maxlen=30))
    last_calib_time = collections.defaultdict(float)
    active_whales = collections.defaultdict(dict)
    last_tg_alert = collections.defaultdict(float)

    while True:
        # Если список пуст, ждем. Иначе биржа закроет соединение с ошибкой "No active subscriptions"
        if not TRACKED_FIGIS:
            await asyncio.sleep(1)
            continue

        try:
            async with AsyncClient(INVEST_TOKEN) as client:
                async def request_iterator():
                    subscribed = set()
                    while True:
                        current_tracked = set(TRACKED_FIGIS)

                        # Защита от обрыва: если юзер удалил все акции, подпишемся на фиктивный Сбер
                        if not current_tracked:
                            current_tracked = {'BBG004730N88'}

                        to_sub = current_tracked - subscribed
                        to_unsub = subscribed - current_tracked

                        if to_unsub:
                            yield MarketDataRequest(
                                subscribe_order_book_request=SubscribeOrderBookRequest(
                                    subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_UNSUBSCRIBE,
                                    instruments=[OrderBookInstrument(figi=f, depth=50) for f in to_unsub]
                                )
                            )
                        if to_sub:
                            yield MarketDataRequest(
                                subscribe_order_book_request=SubscribeOrderBookRequest(
                                    subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                                    instruments=[OrderBookInstrument(figi=f, depth=50) for f in to_sub]
                                )
                            )
                        subscribed = current_tracked
                        await asyncio.sleep(1)

                async for marketdata in client.market_data_stream.market_data_stream(request_iterator()):
                    if marketdata.orderbook:
                        ob = marketdata.orderbook
                        figi = ob.figi

                        # Пропускаем фиктивный Сбер, если его нет в списке
                        if figi not in TRACKED_FIGIS:
                            continue

                        current_time = time.time()
                        bids = [{"price": float(quotation_to_decimal(b.price)), "quantity": b.quantity} for b in
                                ob.bids[:50]]
                        asks = [{"price": float(quotation_to_decimal(a.price)), "quantity": a.quantity} for a in
                                ob.asks[:50]]

                        cur_bid_rub = sum(b['price'] * b['quantity'] * LOT_SIZE for b in bids)
                        cur_ask_rub = sum(a['price'] * a['quantity'] * LOT_SIZE for a in asks)
                        cur_price = (bids[0]['price'] + asks[0]['price']) / 2 if bids and asks else 0

                        if current_time - last_calib_time[figi] >= 1.0:
                            history_bids[figi].append(cur_bid_rub)
                            history_asks[figi].append(cur_ask_rub)
                            last_calib_time[figi] = current_time

                        is_calib = len(history_bids[figi]) < 30
                        bids_dict = {b['price']: b['quantity'] for b in bids}
                        asks_dict = {a['price']: a['quantity'] for a in asks}

                        alerts_to_send = []
                        prices_to_remove = []
                        dynamic_whale_bid, dynamic_whale_ask = 100_000_000, 100_000_000

                        if not is_calib:
                            med_bid = statistics.median(history_bids[figi])
                            med_ask = statistics.median(history_asks[figi])
                            dynamic_whale_bid = max(med_bid * 0.25, 2_000_000)
                            dynamic_whale_ask = max(med_ask * 0.25, 2_000_000)

                            if cur_bid_rub > (med_bid * ANOMALY_MULTIPLIER):
                                alerts_to_send.append(
                                    f"🌊 <b>[МУЛЬТИ-РАДАР] КАСКАД ПОКУПОК!</b>\nОбъем вырос: {cur_bid_rub / 1_000_000:.1f}М ₽")
                            elif cur_ask_rub > (med_ask * ANOMALY_MULTIPLIER):
                                alerts_to_send.append(
                                    f"🌊 <b>[МУЛЬТИ-РАДАР] КАСКАД ПРОДАЖ!</b>\nОбъем вырос: {cur_ask_rub / 1_000_000:.1f}М ₽")

                            for price, data in list(active_whales[figi].items()):
                                w_type = data['type']
                                initial_vol = data.get('initial_vol', data['vol'])
                                pre_alert_sent = data.get('pre_alert_sent', False)

                                current_vol = (bids_dict.get(price, 0) if w_type == 'bid' else asks_dict.get(price,
                                                                                                             0)) * price * LOT_SIZE
                                percent_left = (current_vol / initial_vol) * 100 if initial_vol > 0 else 0
                                threshold = dynamic_whale_bid if w_type == 'bid' else dynamic_whale_ask

                                if 0 < percent_left <= 25 and not pre_alert_sent:
                                    alerts_to_send.append(
                                        f"⚠️ <b>[МУЛЬТИ-РАДАР] ГОТОВЬСЯ! ПРОБОЙ {'ВНИЗ' if w_type == 'bid' else 'ВВЕРХ'}!</b>\nПлиту на {price} ₽ доедают! Осталось: {current_vol / 1_000_000:.1f}М ₽ ({percent_left:.1f}%)")
                                    active_whales[figi][price]['pre_alert_sent'] = True

                                if current_vol < threshold:
                                    is_eaten = (cur_price <= price) if w_type == 'bid' else (cur_price >= price)
                                    if pre_alert_sent or is_eaten:
                                        alerts_to_send.append(
                                            f"{'📉' if w_type == 'bid' else '🚀'} <b>[МУЛЬТИ-РАДАР] ПРОБОЙ {'ВНИЗ' if w_type == 'bid' else 'ВВЕРХ'}!</b>\nПлиту на {price} ₽ сожрали!")
                                    prices_to_remove.append(price)

                            for p in prices_to_remove:
                                del active_whales[figi][p]

                            # В Мульти-Радаре молча фиксируем новые плиты (без отправки спама)
                            for price, qty in bids_dict.items():
                                vol = price * qty * LOT_SIZE
                                if vol >= dynamic_whale_bid and price not in active_whales[figi]:
                                    active_whales[figi][price] = {'type': 'bid', 'vol': vol, 'initial_vol': vol,
                                                                  'pre_alert_sent': False}

                            for price, qty in asks_dict.items():
                                vol = price * qty * LOT_SIZE
                                if vol >= dynamic_whale_ask and price not in active_whales[figi]:
                                    active_whales[figi][price] = {'type': 'ask', 'vol': vol, 'initial_vol': vol,
                                                                  'pre_alert_sent': False}

                        anomaly_msg, is_anomaly = "", False
                        if alerts_to_send:
                            anomaly_msg = alerts_to_send[0].replace("\n", " ")
                            is_anomaly = True

                            if current_time - last_tg_alert[figi] > 10 and MULTI_BOT_ALERTS_ENABLED:
                                ticker_name = TICKER_CACHE.get(figi, figi)
                                tg_msg = f"🔔 <b>{ticker_name}</b>\n\n" + "\n\n".join(alerts_to_send)
                                await asyncio.to_thread(send_telegram_message, tg_msg)
                                last_tg_alert[figi] = current_time

                        MULTI_GLOBAL_DATA[figi] = {
                            "status": "ok", "ticker": TICKER_CACHE.get(figi, figi),
                            "bids": bids, "asks": asks, "is_calibrating": is_calib,
                            "anomaly_msg": anomaly_msg, "is_anomaly": is_anomaly,
                            "total_bid_rubles": cur_bid_rub, "total_ask_rubles": cur_ask_rub,
                            "total_volume": cur_bid_rub + cur_ask_rub, "current_price": cur_price,
                            "dynamic_whale_bid": dynamic_whale_bid, "dynamic_whale_ask": dynamic_whale_ask
                        }
        except Exception as e:
            print(f"Ошибка потока мульти-радара: {e}")
            await asyncio.sleep(3)


# =========================================================================
# === ПРЕДСТАВЛЕНИЯ (VIEWS) ===
# =========================================================================
def real_market_page(request): return render(request, "real_market.html")


def portfolio_page(request): return render(request, "portfolio.html")


def radar_page(request): return render(request, "radar.html")


def api_real_data(request):
    global ACTIVE_FIGI, STREAM_THREAD, BOT_ALERTS_ENABLED, PREV_BOT_STATE
    if not INVEST_TOKEN: return JsonResponse({"status": "error", "message": "Токен не найден!"})

    figi = request.GET.get('figi', 'BBG004730N88')
    bot_state = request.GET.get('bot', 'false') == 'true'

    if figi not in TICKER_CACHE:
        try:
            with Client(INVEST_TOKEN) as sync_client:
                inst = sync_client.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                                                                 id=figi, class_code="")
                TICKER_CACHE[figi] = inst.instrument.ticker
        except Exception:
            TICKER_CACHE[figi] = figi

    if bot_state != PREV_BOT_STATE:
        if bot_state:
            threading.Thread(target=send_telegram_message,
                             args=("🟢 <b>[ОДИНОЧНЫЙ РАДАР] ВКЛЮЧЕН</b>\nУведомления активированы.",)).start()
        else:
            threading.Thread(target=send_telegram_message,
                             args=("🔴 <b>[ОДИНОЧНЫЙ РАДАР] ВЫКЛЮЧЕН</b>\nУведомления приостановлены.",)).start()
        PREV_BOT_STATE = bot_state

    BOT_ALERTS_ENABLED = bot_state

    if ACTIVE_FIGI != figi:
        ACTIVE_FIGI = figi
        GLOBAL_DATA[figi] = {"status": "loading", "message": "Подключение к потоку биржи..."}
        STREAM_THREAD = threading.Thread(target=start_stream_in_thread, args=(figi,), daemon=True)
        STREAM_THREAD.start()

    return JsonResponse(GLOBAL_DATA.get(figi, {"status": "loading", "message": "Инициализация..."}))


def api_radar_data(request):
    global MULTI_STREAM_THREAD, MULTI_BOT_ALERTS_ENABLED, PREV_MULTI_BOT_STATE
    if not INVEST_TOKEN: return JsonResponse({"status": "error", "message": "Токен не найден!"})

    if not MULTI_STREAM_THREAD:
        MULTI_STREAM_THREAD = threading.Thread(target=start_multi_stream_in_thread, daemon=True)
        MULTI_STREAM_THREAD.start()

    # === ВОССТАНОВЛЕННАЯ ФУНКЦИЯ СИНХРОНИЗАЦИИ (ЧТОБЫ АКЦИИ НЕ СБРАСЫВАЛИСЬ) ===
    sync_param = request.GET.get('sync')
    if sync_param is not None:
        frontend_figis = set()
        if sync_param != "":
            for pair in sync_param.split(','):
                if ':' in pair:
                    f, t = pair.split(':', 1)
                    TRACKED_FIGIS.add(f)
                    TICKER_CACHE[f] = t
                    frontend_figis.add(f)
                    if f not in MULTI_GLOBAL_DATA:
                        MULTI_GLOBAL_DATA[f] = {"status": "loading", "ticker": t, "message": "Подключение..."}

        to_remove = TRACKED_FIGIS - frontend_figis
        for f in list(to_remove):
            TRACKED_FIGIS.remove(f)
            if f in MULTI_GLOBAL_DATA:
                del MULTI_GLOBAL_DATA[f]

    bot_state = request.GET.get('bot', 'false') == 'true'

    if bot_state != PREV_MULTI_BOT_STATE:
        if bot_state:
            threading.Thread(target=send_telegram_message,
                             args=("🟢 <b>[МУЛЬТИ-РАДАР] ВКЛЮЧЕН</b>\nГалерея активирована.",)).start()
        else:
            threading.Thread(target=send_telegram_message,
                             args=("🔴 <b>[МУЛЬТИ-РАДАР] ВЫКЛЮЧЕН</b>\nУведомления из галереи приостановлены.",)).start()
        PREV_MULTI_BOT_STATE = bot_state

    MULTI_BOT_ALERTS_ENABLED = bot_state
    return JsonResponse({"status": "ok", "data": MULTI_GLOBAL_DATA})


def api_radar_add(request):
    figi = request.GET.get('figi')
    ticker = request.GET.get('ticker')
    if figi:
        TRACKED_FIGIS.add(figi)
        if ticker: TICKER_CACHE[figi] = ticker
    return JsonResponse({"status": "ok"})


def api_radar_remove(request):
    figi = request.GET.get('figi')
    if figi in TRACKED_FIGIS:
        TRACKED_FIGIS.remove(figi)
        if figi in MULTI_GLOBAL_DATA: del MULTI_GLOBAL_DATA[figi]
    return JsonResponse({"status": "ok"})


def api_search(request):
    query = request.GET.get('q', '').strip().lower()
    if not INVEST_TOKEN or len(query) < 2:
        return JsonResponse({"status": "ok", "results": []})

    global CACHE_READY, CACHED_INSTRUMENTS

    results = []

    if CACHE_READY:
        # ПРИОРИТЕТ 1: Точное совпадение тикера (ввел "SBER" -> выдал SBER)
        for inst in CACHED_INSTRUMENTS:
            if query == inst['ticker'].lower():
                results.append(inst)

        # ПРИОРИТЕТ 2: Совпадение по началу имени или тикера
        for inst in CACHED_INSTRUMENTS:
            if inst not in results and (inst['ticker'].lower().startswith(query) or query in inst['name'].lower()):
                results.append(inst)
            if len(results) >= 8:
                break

        # Сортировка: Мосбиржа выше
        results = sorted(results, key=lambda x: (x['class_code'] != 'TQBR', x['name']))

    else:
        # Fallback (если кэш еще не скачался за пару секунд после старта)
        try:
            with Client(INVEST_TOKEN) as client:
                response = client.instruments.find_instrument(query=query)
                sorted_instruments = sorted(response.instruments, key=lambda x: (x.class_code != 'TQBR', x.name))
                seen_tickers = set()
                for inst in sorted_instruments:
                    if inst.instrument_type in ['share', 'etf', 'currency'] and inst.ticker not in seen_tickers:
                        if getattr(inst, 'api_trade_available_flag', True):
                            results.append({
                                "figi": inst.figi, "ticker": inst.ticker, "name": inst.name,
                                "type": inst.instrument_type, "class_code": getattr(inst, 'class_code', '')
                            })
                            seen_tickers.add(inst.ticker)
                        if len(results) >= 8: break
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)})

    formatted_results = []
    for inst in results[:8]:
        name_lower = inst['name'].lower()
        display_name = inst['name']

        if "сбер" in name_lower and "ап" in name_lower or "pref" in inst['ticker'].lower():
            display_name = f"{inst['name']} (Прив.)"
        elif inst['ticker'] == "SBER":
            display_name = "Сбербанк (Основная акция)"
        elif inst['ticker'] == "YDEX":
            display_name = "Яндекс (МКПАО)"

        formatted_results.append({
            "figi": inst['figi'],
            "ticker": inst['ticker'],
            "name": display_name,
            "type": inst['type']
        })

    return JsonResponse({"status": "ok", "results": formatted_results})


def api_history_data(request):
    figi = request.GET.get('figi', 'BBG004730N88')
    tf = request.GET.get('tf', '1m')

    if not INVEST_TOKEN: return JsonResponse({"status": "error", "message": "Токен не найден!"})

    tf_map = {
        '1m': (CandleInterval.CANDLE_INTERVAL_1_MIN, timedelta(days=1)),
        '2m': (CandleInterval.CANDLE_INTERVAL_2_MIN, timedelta(days=1)),
        '3m': (CandleInterval.CANDLE_INTERVAL_3_MIN, timedelta(days=1)),
        '5m': (CandleInterval.CANDLE_INTERVAL_5_MIN, timedelta(days=2)),
        '10m': (CandleInterval.CANDLE_INTERVAL_10_MIN, timedelta(days=2)),
        '15m': (CandleInterval.CANDLE_INTERVAL_15_MIN, timedelta(days=3)),
        '30m': (CandleInterval.CANDLE_INTERVAL_30_MIN, timedelta(days=5)),
        '1h': (CandleInterval.CANDLE_INTERVAL_HOUR, timedelta(days=14)),
        '2h': (CandleInterval.CANDLE_INTERVAL_2_HOUR, timedelta(days=30)),
        '4h': (CandleInterval.CANDLE_INTERVAL_4_HOUR, timedelta(days=60)),
        '1d': (CandleInterval.CANDLE_INTERVAL_DAY, timedelta(days=365)),
        '1w': (CandleInterval.CANDLE_INTERVAL_WEEK, timedelta(days=365 * 2)),
        '1M': (CandleInterval.CANDLE_INTERVAL_MONTH, timedelta(days=365 * 5)),
    }

    if tf not in tf_map: tf = '1m'
    interval, delta = tf_map[tf]
    now = datetime.now(timezone.utc)
    from_time = now - delta

    try:
        with Client(INVEST_TOKEN) as client:
            candles = client.get_all_candles(figi=figi, from_=from_time, to=now, interval=interval)
            data = []
            for c in candles:
                if c.is_complete:
                    data.append({
                        "time": int(c.time.timestamp()),
                        "open": float(quotation_to_decimal(c.open)),
                        "high": float(quotation_to_decimal(c.high)),
                        "low": float(quotation_to_decimal(c.low)),
                        "close": float(quotation_to_decimal(c.close)),
                    })
            return JsonResponse({"status": "ok", "candles": data})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


def api_portfolio_data(request):
    if not INVEST_TOKEN: return JsonResponse({"status": "error", "message": "Токен не найден!"})
    try:
        with Client(INVEST_TOKEN) as client:
            accounts_response = client.users.get_accounts()
            if not accounts_response.accounts: return JsonResponse({"status": "error", "message": "Счета не найдены!"})

            account_id = accounts_response.accounts[0].id
            portfolio = client.operations.get_portfolio(account_id=account_id)

            positions_data = []
            total_yield_rub = 0.0
            total_invested = 0.0

            for p in portfolio.positions:
                qty = float(quotation_to_decimal(p.quantity)) if p.quantity else 0.0
                avg_price = float(quotation_to_decimal(p.average_position_price)) if p.average_position_price else 0.0
                curr_price = float(quotation_to_decimal(p.current_price)) if p.current_price else 0.0
                pos_yield_rub = float(quotation_to_decimal(p.expected_yield)) if p.expected_yield else 0.0

                invested_sum = avg_price * qty
                yield_percent = (pos_yield_rub / invested_sum * 100) if invested_sum > 0 else 0.0

                total_yield_rub += pos_yield_rub
                total_invested += invested_sum

                ticker = p.figi
                try:
                    inst_info = client.instruments.get_instrument_by(
                        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                        class_code="",
                        id=p.figi
                    )
                    ticker = inst_info.instrument.ticker
                except:
                    pass

                positions_data.append({
                    "figi": p.figi, "ticker": ticker, "instrument_type": p.instrument_type,
                    "quantity": qty, "average_price": avg_price, "current_price": curr_price,
                    "expected_yield": pos_yield_rub, "yield_percent": yield_percent, "total_sum": curr_price * qty
                })

            total_yield_percent = (total_yield_rub / total_invested * 100) if total_invested > 0 else 0.0
            total_portfolio_cost = float(
                quotation_to_decimal(portfolio.total_amount_portfolio)) if portfolio.total_amount_portfolio else 0.0

            return JsonResponse({
                "status": "ok", "total_portfolio_cost": total_portfolio_cost,
                "expected_yield_rubles": total_yield_rub, "expected_yield_percent": total_yield_percent,
                "positions": positions_data
            })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})