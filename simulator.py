import asyncio
import os
import random
from datetime import datetime

LOT_SIZE = 10
WHALE_THRESHOLD = 15_000_000


def create_bar(percentage, length=30):
    """Рисует прогресс-бар"""
    filled = int(length * (percentage / 100))
    return '█' * filled + '░' * (length - filled)


class MockOrder:
    def __init__(self, price, quantity):
        self.price = price
        self.quantity = quantity


async def generate_stream():
    """Генератор искусственного потока данных"""
    base_price = 284.00
    iteration = 0

    while True:
        iteration += 1
        time_now = datetime.now()

        # 1. Генерируем "Спокойный рынок"
        bids = [MockOrder(base_price - (i + 1) * 0.01, random.randint(100, 3000)) for i in range(10)]
        asks = [MockOrder(base_price + i * 0.01, random.randint(100, 3000)) for i in range(10)]

        # 2. Имитируем появление "Кита" (каждые 6 итераций)
        is_whale_active = (iteration % 6 == 0) or (iteration % 6 == 1)

        if is_whale_active:
            # МАГИЯ ЗДЕСЬ: Имитируем каскадную панику или работу крупного алгоритма
            # Заливаем красным сразу 5 уровней подряд!
            asks[2] = MockOrder(base_price + 0.02, 50000)  # ~142 млн руб.
            asks[3] = MockOrder(base_price + 0.03, 85000)  # ~241 млн руб.
            asks[4] = MockOrder(base_price + 0.04, 150000)  # ~426 млн руб.
            asks[5] = MockOrder(base_price + 0.05, 110000)  # ~312 млн руб.
            asks[6] = MockOrder(base_price + 0.06, 70000)  # ~198 млн руб.

        yield bids, asks, time_now, is_whale_active
        await asyncio.sleep(1)


async def run_simulation():
    async for bids, asks, time_now, is_whale_active in generate_stream():
        total_bid_rubles = sum(b.price * b.quantity * LOT_SIZE for b in bids)
        total_ask_rubles = sum(a.price * a.quantity * LOT_SIZE for a in asks)
        total_volume = total_bid_rubles + total_ask_rubles

        bid_percent = (total_bid_rubles / total_volume) * 100 if total_volume > 0 else 50
        ask_percent = (total_ask_rubles / total_volume) * 100 if total_volume > 0 else 50

        os.system('cls' if os.name == 'nt' else 'clear')

        if is_whale_active:
            print(f"🚨 ВНИМАНИЕ: ЭКСТРЕМАЛЬНОЕ ДАВЛЕНИЕ ПРОДАВЦОВ! (Время: {time_now.strftime('%H:%M:%S')})")
        else:
            print(f"🌊 СИМУЛЯЦИЯ: Спокойный рынок... (Время: {time_now.strftime('%H:%M:%S')})")

        print("=" * 75)
        print("⚖️ БАЛАНС СИЛ (ТОП-10 УРОВНЕЙ):")
        print(f"Покупатели: {total_bid_rubles / 1_000_000:>6.1f}М ₽ [{create_bar(bid_percent, 20)}] {bid_percent:.1f}%")
        print(f"Продавцы:   {total_ask_rubles / 1_000_000:>6.1f}М ₽ [{create_bar(ask_percent, 20)}] {ask_percent:.1f}%")
        print("=" * 75)

        print(f"{'Цена Пок.':<10} | {'Объем':<8} || {'Объем':>8} | {'Цена Прод.':>10} | {'Аномалии'}")
        print("-" * 75)

        max_len = max(len(bids), len(asks))

        for i in range(max_len):
            bid_price_str, bid_qty_str, bid_alert = "-", "-", ""
            if i < len(bids):
                bid_rubles = bids[i].price * bids[i].quantity * LOT_SIZE
                if bid_rubles >= WHALE_THRESHOLD:
                    bid_alert = f"🟩 +{bid_rubles / 1_000_000:.1f}М"
                bid_price_str = f"{bids[i].price:.2f} ₽"
                bid_qty_str = str(bids[i].quantity)

            ask_price_str, ask_qty_str, ask_alert = "-", "-", ""
            if i < len(asks):
                ask_rubles = asks[i].price * asks[i].quantity * LOT_SIZE
                if ask_rubles >= WHALE_THRESHOLD:
                    ask_alert = f"🟥 -{ask_rubles / 1_000_000:.1f}М"
                ask_price_str = f"{asks[i].price:.2f} ₽"
                ask_qty_str = str(asks[i].quantity)

            alerts = f"{bid_alert} {ask_alert}".strip()
            print(f"{bid_price_str:<10} | {bid_qty_str:<8} || {ask_qty_str:>8} | {ask_price_str:>10} | {alerts}")

        print("=" * 75)
        print("Нажмите Ctrl+C для остановки...")


if __name__ == "__main__":
    try:
        asyncio.run(run_simulation())
    except KeyboardInterrupt:
        print("\n🛑 Симуляция остановлена.")