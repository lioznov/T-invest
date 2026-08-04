import asyncio
import os
import certifi
import collections
import statistics
from dotenv import load_dotenv
from t_tech.invest import (
    AsyncClient,
    MarketDataRequest,
    SubscribeOrderBookRequest,
    SubscriptionAction,
    OrderBookInstrument
)
from t_tech.invest.utils import quotation_to_decimal

load_dotenv()
os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certifi.where()

TOKEN = os.getenv("INVEST_TOKEN")
FIGI = "BBG004730N88"  # Можете менять на любой тикер!
LOT_SIZE = 10

# --- НАСТРОЙКИ УМНОГО АЛГОРИТМА ---
HISTORY_SECONDS = 30  # Сколько секунд храним в памяти для оценки "спокойствия"
ANOMALY_MULTIPLIER = 3.0  # Во сколько раз объем должен превысить норму (3.0 = на 200% больше нормы)

# Очереди для хранения истории объемов (автоматически удаляют старые данные, сохраняя только последние N)
bid_history = collections.deque(maxlen=HISTORY_SECONDS)
ask_history = collections.deque(maxlen=HISTORY_SECONDS)


def create_bar(percentage, length=30):
    filled = int(length * (percentage / 100))
    return '█' * filled + '░' * (length - filled)


async def request_iterator():
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[OrderBookInstrument(figi=FIGI, depth=20)]
        )
    )
    while True:
        await asyncio.sleep(1)


async def main():
    async with AsyncClient(TOKEN) as client:
        try:
            async for marketdata in client.market_data_stream.market_data_stream(request_iterator()):
                if marketdata.orderbook:
                    ob = marketdata.orderbook

                    # 1. Считаем текущий объем всего стакана
                    current_bid_rubles = sum(
                        float(quotation_to_decimal(b.price)) * b.quantity * LOT_SIZE for b in ob.bids)
                    current_ask_rubles = sum(
                        float(quotation_to_decimal(a.price)) * a.quantity * LOT_SIZE for a in ob.asks)

                    # 2. Добавляем текущие данные в историю
                    bid_history.append(current_bid_rubles)
                    ask_history.append(current_ask_rubles)

                    os.system('cls' if os.name == 'nt' else 'clear')
                    print(f"📊 УМНЫЙ РАДАР (Время: {ob.time.strftime('%H:%M:%S')})")
                    print("=" * 70)

                    # 3. АЛГОРИТМ АНАЛИЗА
                    if len(bid_history) < HISTORY_SECONDS:
                        # Идет сбор данных (калибровка)
                        print(f"⏳ КАЛИБРОВКА: Сбор данных для акции... ({len(bid_history)}/{HISTORY_SECONDS} сек)")
                        print("=" * 70)
                    else:
                        # Радар активен! Высчитываем медианный "спокойный" объем
                        median_bid = statistics.median(bid_history)
                        median_ask = statistics.median(ask_history)

                        # Проверяем на аномалии
                        anomaly_msg = ""
                        if current_bid_rubles > (median_bid * ANOMALY_MULTIPLIER):
                            anomaly_msg = f"🚨 ВНИМАНИЕ! КАСКАД ПОКУПОК! Объем х{current_bid_rubles / median_bid:.1f} от нормы!"
                        elif current_ask_rubles > (median_ask * ANOMALY_MULTIPLIER):
                            anomaly_msg = f"🩸 ВНИМАНИЕ! КАСКАД ПРОДАЖ! Объем х{current_ask_rubles / median_ask:.1f} от нормы!"

                        if anomaly_msg:
                            print(anomaly_msg)
                        else:
                            print("✅ Рынок в пределах нормы (Аномалий нет).")

                        print(
                            f"Норма покупок: ~{median_bid / 1_000_000:.1f}М ₽ | Сейчас: {current_bid_rubles / 1_000_000:.1f}М ₽")
                        print(
                            f"Норма продаж:  ~{median_ask / 1_000_000:.1f}М ₽ | Сейчас: {current_ask_rubles / 1_000_000:.1f}М ₽")
                        print("=" * 70)

                    # 4. Отрисовка баланса (как в прошлом шаге)
                    total_volume = current_bid_rubles + current_ask_rubles
                    if total_volume > 0:
                        bid_percent = (current_bid_rubles / total_volume) * 100
                        ask_percent = (current_ask_rubles / total_volume) * 100
                    else:
                        bid_percent, ask_percent = 50, 50

                    print(f"Покупатели: [{create_bar(bid_percent, 20)}] {bid_percent:.1f}%")
                    print(f"Продавцы:   [{create_bar(ask_percent, 20)}] {ask_percent:.1f}%")
                    print("=" * 70)
                    print("Нажмите Ctrl+C для остановки...")

        except Exception as e:
            print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Сканер остановлен.")