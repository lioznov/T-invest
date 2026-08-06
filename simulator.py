import asyncio
import os
import random
import time
import aiohttp
from datetime import datetime
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LOT_SIZE = 10
WHALE_THRESHOLD = 15_000_000
TG_COOLDOWN_SECONDS = 10  # Уменьшенный кулдаун для быстрого тестирования

# Глобальная переменная для отслеживания времени последнего алерта в ТГ
last_tg_alert_time = 0


def create_bar(percentage, length=30):
    """Рисует прогресс-бар"""
    filled = int(length * (percentage / 100))
    return '█' * filled + '░' * (length - filled)


class MockOrder:
    def __init__(self, price, quantity):
        self.price = price
        self.quantity = quantity


async def send_telegram_message(message: str):
    """Отправляет сообщение в Telegram асинхронно"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return  # Если ключи не настроены, пропускаем

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    print(f"⚠️ Ошибка отправки в ТГ: {await response.text()}")
        except Exception as e:
            print(f"❌ Ошибка соединения с ТГ: {e}")


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
            asks[2] = MockOrder(base_price + 0.02, 50000)  # ~142 млн руб.
            asks[3] = MockOrder(base_price + 0.03, 85000)  # ~241 млн руб.
            asks[4] = MockOrder(base_price + 0.04, 150000)  # ~426 млн руб.
            asks[5] = MockOrder(base_price + 0.05, 110000)  # ~312 млн руб.
            asks[6] = MockOrder(base_price + 0.06, 70000)  # ~198 млн руб.

        yield bids, asks, time_now, is_whale_active
        await asyncio.sleep(1)


async def run_simulation():
    global last_tg_alert_time

    async for bids, asks, time_now, is_whale_active in generate_stream():
        total_bid_rubles = sum(b.price * b.quantity * LOT_SIZE for b in bids)
        total_ask_rubles = sum(a.price * a.quantity * LOT_SIZE for a in asks)
        total_volume = total_bid_rubles + total_ask_rubles

        bid_percent = (total_bid_rubles / total_volume) * 100 if total_volume > 0 else 50
        ask_percent = (total_ask_rubles / total_volume) * 100 if total_volume > 0 else 50

        os.system('cls' if os.name == 'nt' else 'clear')

        if is_whale_active:
            print(f"🚨 ВНИМАНИЕ: ЭКСТРЕМАЛЬНОЕ ДАВЛЕНИЕ ПРОДАВЦОВ! (Время: {time_now.strftime('%H:%M:%S')})")
            
            # --- БЛОК ОТПРАВКИ В TELEGRAM ---
            current_time = time.time()
            if current_time - last_tg_alert_time > TG_COOLDOWN_SECONDS:
                tg_msg = (
                    f"🐋 <b>СИМУЛЯТОР: ОБНАРУЖЕН КИТ!</b>\n\n"
                    f"🔴 Замечено экстремальное давление продавцов.\n"
                    f"Объем продаж: {total_ask_rubles / 1_000_000:.1f}М ₽\n"
                    f"Время: {time_now.strftime('%H:%M:%S')}"
                )
                asyncio.create_task(send_telegram_message(tg_msg))
                last_tg_alert_time = current_time
                print("📨 Уведомление отправлено в Telegram!")
            else:
                print(f"⏳ ТГ на паузе ({int(TG_COOLDOWN_SECONDS - (current_time - last_tg_alert_time))} сек)...")
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