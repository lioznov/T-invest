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
    CandleInterval
)
from t_tech.invest.utils import quotation_to_decimal

os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certifi.where()
env_path = os.path.join(settings.BASE_DIR, '.env')
load_dotenv(env_path)

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INVEST_TOKEN = os.getenv("INVEST_TOKEN")

GLOBAL_DATA = {}
ACTIVE_FIGI = None
STREAM_THREAD = None
LOT_SIZE = 10
ANOMALY_MULTIPLIER = 3.0


def send_telegram_message(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode("utf-8")
    try:
        urllib.request.urlopen(url, data=data)
    except:
        pass


def start_stream_in_thread(figi):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(stream_data(figi))


async def stream_data(figi):
    global GLOBAL_DATA, ACTIVE_FIGI
    history_bids = collections.deque(maxlen=30)
    history_asks = collections.deque(maxlen=30)
    last_tg_alert, last_calib_time = 0, 0
    active_whales = {}

    while ACTIVE_FIGI == figi:
        try:
            async with AsyncClient(INVEST_TOKEN) as client:
                async def request_iterator():
                    # ПОДПИСЫВАЕМСЯ на стакан
                    yield MarketDataRequest(
                        subscribe_order_book_request=SubscribeOrderBookRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                            instruments=[OrderBookInstrument(figi=figi, depth=50)]
                        )
                    )

                    # Ждем, пока пользователь не переключит акцию
                    while ACTIVE_FIGI == figi:
                        await asyncio.sleep(1)

                    # ОТПИСЫВАЕМСЯ (чтобы биржа не заблокировала нас за спам)
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
                        dynamic_whale_bid = 100_000_000
                        dynamic_whale_ask = 100_000_000

                        if not is_calib:
                            med_bid = statistics.median(history_bids)
                            med_ask = statistics.median(history_asks)

                            dynamic_whale_bid = max(med_bid * 0.25, 2_000_000)
                            dynamic_whale_ask = max(med_ask * 0.25, 2_000_000)

                            if cur_bid_rub > (med_bid * ANOMALY_MULTIPLIER):
                                alerts_to_send.append(
                                    f"🌊 <b>КАСКАД ПОКУПОК!</b>\nОбъем вырос в 3 раза: {cur_bid_rub / 1_000_000:.1f}М ₽")
                            elif cur_ask_rub > (med_ask * ANOMALY_MULTIPLIER):
                                alerts_to_send.append(
                                    f"🌊 <b>КАСКАД ПРОДАЖ!</b>\nОбъем вырос в 3 раза: {cur_ask_rub / 1_000_000:.1f}М ₽")

                            for price, data in active_whales.items():
                                w_type = data['type']
                                if w_type == 'bid':
                                    if bids_dict.get(price, 0) * price * LOT_SIZE < dynamic_whale_bid:
                                        if cur_price <= price:
                                            alerts_to_send.append(
                                                f"📉 <b>ПРОБОЙ ВНИЗ!</b>\nПлита покупателя на {price} ₽ уничтожена!")
                                        else:
                                            alerts_to_send.append(
                                                f"👻 <b>СПУФИНГ!</b>\nПлита покупателя на {price} ₽ пропала.")
                                        prices_to_remove.append(price)
                                elif w_type == 'ask':
                                    if asks_dict.get(price, 0) * price * LOT_SIZE < dynamic_whale_ask:
                                        if cur_price >= price:
                                            alerts_to_send.append(
                                                f"🚀 <b>ПРОБОЙ ВВЕРХ!</b>\nПлита продавца на {price} ₽ уничтожена!")
                                        else:
                                            alerts_to_send.append(
                                                f"👻 <b>СПУФИНГ!</b>\nПлита продавца на {price} ₽ пропала.")
                                        prices_to_remove.append(price)

                            for p in prices_to_remove: del active_whales[p]

                            for price, qty in bids_dict.items():
                                vol = price * qty * LOT_SIZE
                                if vol >= dynamic_whale_bid and price not in active_whales:
                                    active_whales[price] = {'type': 'bid', 'vol': vol}
                                    alerts_to_send.append(
                                        f"🟢 <b>ПЛИТА (Покупка)</b>\nЦена: {price} ₽\nЛоты: <b>{qty} шт.</b>\nСумма: {vol / 1_000_000:.1f}М ₽")

                            for price, qty in asks_dict.items():
                                vol = price * qty * LOT_SIZE
                                if vol >= dynamic_whale_ask and price not in active_whales:
                                    active_whales[price] = {'type': 'ask', 'vol': vol}
                                    alerts_to_send.append(
                                        f"🔴 <b>ПЛИТА (Продажа)</b>\nЦена: {price} ₽\nЛоты: <b>{qty} шт.</b>\nСумма: {vol / 1_000_000:.1f}М ₽")

                        anomaly_msg, is_anomaly = "", False
                        if alerts_to_send:
                            anomaly_msg = alerts_to_send[0].replace("\n", " ")
                            is_anomaly = True
                            if current_time - last_tg_alert > 10:
                                tg_msg = f"🔔 <b>АКЦИЯ: {figi}</b>\n\n" + "\n\n".join(alerts_to_send)
                                await asyncio.to_thread(send_telegram_message, tg_msg)
                                last_tg_alert = current_time

                        GLOBAL_DATA[figi] = {
                            "status": "ok", "bids": bids, "asks": asks,
                            "is_calibrating": is_calib, "anomaly_msg": anomaly_msg, "is_anomaly": is_anomaly,
                            "total_bid_rubles": cur_bid_rub, "total_ask_rubles": cur_ask_rub,
                            "total_volume": cur_bid_rub + cur_ask_rub, "current_price": cur_price,
                            "dynamic_whale_bid": dynamic_whale_bid,
                            "dynamic_whale_ask": dynamic_whale_ask
                        }
        except Exception as e:
            # ЕСЛИ БИРЖА ОТВЕРГЛА ТИКЕР (например, он не торгуется)
            print(f"Ошибка потока: {e}")
            if ACTIVE_FIGI == figi:
                GLOBAL_DATA[figi] = {"status": "error", "message": "Инструмент не торгуется (выберите другой)"}
                await asyncio.sleep(3)


def real_market_page(request): return render(request, "real_market.html")


def portfolio_page(request): return render(request, "portfolio.html")


def api_real_data(request):
    global ACTIVE_FIGI, STREAM_THREAD
    if not INVEST_TOKEN: return JsonResponse({"status": "error", "message": "Токен не найден!"})
    figi = request.GET.get('figi', 'BBG004730N88')
    if ACTIVE_FIGI != figi:
        ACTIVE_FIGI = figi
        GLOBAL_DATA[figi] = {"status": "loading", "message": "Подключение к потоку биржи..."}
        STREAM_THREAD = threading.Thread(target=start_stream_in_thread, args=(figi,), daemon=True)
        STREAM_THREAD.start()
    return JsonResponse(GLOBAL_DATA.get(figi, {"status": "loading", "message": "Инициализация..."}))


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

                positions_data.append({
                    "figi": p.figi,
                    "instrument_type": p.instrument_type,
                    "quantity": qty,
                    "average_price": avg_price,
                    "current_price": curr_price,
                    "expected_yield": pos_yield_rub,
                    "yield_percent": yield_percent,
                    "total_sum": curr_price * qty
                })

            total_yield_percent = (total_yield_rub / total_invested * 100) if total_invested > 0 else 0.0
            total_portfolio_cost = float(
                quotation_to_decimal(portfolio.total_amount_portfolio)) if portfolio.total_amount_portfolio else 0.0

            return JsonResponse({
                "status": "ok",
                "total_portfolio_cost": total_portfolio_cost,
                "expected_yield_rubles": total_yield_rub,
                "expected_yield_percent": total_yield_percent,
                "positions": positions_data
            })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


# === НОВАЯ ФУНКЦИЯ ДЛЯ УМНОГО ПОИСКА (С ПРИОРИТЕТАМИ) ===
# === УМНЫЙ ПОИСК БЕЗ ДУБЛИКАТОВ И С ПОДСКАЗКАМИ ===
def api_search(request):
    query = request.GET.get('q', '').strip()
    if not INVEST_TOKEN or len(query) < 2:
        return JsonResponse({"status": "ok", "results": []})

    try:
        with Client(INVEST_TOKEN) as client:
            response = client.instruments.find_instrument(query=query)

            seen_tickers = set()  # Чтобы исключить дубликаты с одинаковым тикером
            results = []

            for inst in response.instruments:
                # Берем только акции, фонды и валюту, которые реально торгуются на основных площадках
                if inst.instrument_type in ['share', 'etf', 'currency'] and inst.ticker not in seen_tickers:

                    # Делаем понятное описание для пользователя
                    name_lower = inst.name.lower()
                    if "сбер" in name_lower and "ап" in name_lower or "pref" in inst.ticker.lower():
                        display_name = f"{inst.name} (Привилегированные)"
                    elif inst.ticker == "SBER":
                        display_name = "Сбербанк (Основная акция)"
                    elif inst.ticker == "YDEX":
                        display_name = "Яндекс (МКПАО ЯДДЕКС)"
                    else:
                        display_name = inst.name

                    results.append({
                        "figi": inst.figi,
                        "ticker": inst.ticker,
                        "name": display_name,
                        "type": inst.instrument_type
                    })
                    seen_tickers.add(inst.ticker)  # Запоминаем тикер, чтобы больше не дублировать

            return JsonResponse({"status": "ok", "results": results[:8]})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})