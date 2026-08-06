import asyncio
import os
import certifi
from datetime import datetime
from dotenv import load_dotenv
from t_tech.invest import (
    AsyncClient,
    MarketDataRequest,
    SubscribeOrderBookRequest,
    SubscriptionAction,
    OrderBookInstrument
)
from t_tech.invest.utils import quotation_to_decimal

# --- НАСТРОЙКИ ---
# Обязательный фикс для работы защищенного соединения в Windows
os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certifi.where()

# Загружаем ключи
load_dotenv()
TOKEN = os.getenv("INVEST_TOKEN")

# Настройки инструмента
FIGI = "BBG004730N88"  # Сбербанк
LOT_SIZE = 10          # В 1 лоте Сбера 10 акций


def create_bar(percentage, length=20):
    """Рисует прогресс-бар"""
    filled = int(length * (percentage / 100))
    return '█' * filled + '░' * (length - filled)


async def request_iterator():
    """Генератор запроса на подписку к стакану (топ-10 уровней)"""
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[OrderBookInstrument(figi=FIGI, depth=10)]
        )
    )
    while True:
        await asyncio.sleep(1)


async def main():
    if not TOKEN:
        print("❌ ОШИБКА: Токен не найден! Проверьте файл .env")
        return

    async with AsyncClient(TOKEN) as client:
        try:
            print("⏳ Установка соединения с серверами Т-Банка...")
            
            # Подключаемся к потоку биржевых данных
            async for marketdata in client.market_data_stream.market_data_stream(request_iterator()):
                if marketdata.orderbook:
                    ob = marketdata.orderbook
                    
                    # Извлекаем и конвертируем данные стакана (только первые 10 уровней)
                    bids = [{"price": float(quotation_to_decimal(b.price)), "qty": b.quantity} for b in ob.bids[:10]]
                    asks = [{"price": float(quotation_to_decimal(a.price)), "qty": a.quantity} for a in ob.asks[:10]]
                    
                    # Считаем объемы в рублях
                    total_bid_rubles = sum(b["price"] * b["qty"] * LOT_SIZE for b in bids)
                    total_ask_rubles = sum(a["price"] * a["qty"] * LOT_SIZE for a in asks)
                    total_volume = total_bid_rubles + total_ask_rubles
                    
                    # Считаем процентное соотношение сил
                    bid_percent = (total_bid_rubles / total_volume) * 100 if total_volume > 0 else 50
                    ask_percent = (total_ask_rubles / total_volume) * 100 if total_volume > 0 else 50

                    # --- ОТРИСОВКА В ТЕРМИНАЛЕ ---
                    # Очищаем экран (работает и в Windows, и в Mac/Linux)
                    os.system('cls' if os.name == 'nt' else 'clear')
                    
                    print(f"📈 СТАКАН В ТЕРМИНАЛЕ: СБЕРБАНК (Время: {ob.time.strftime('%H:%M:%S')})")
                    print("=" * 65)
                    print(f"Покупатели: {total_bid_rubles/1_000_000:>6.1f}М ₽ [{create_bar(bid_percent)}] {bid_percent:.1f}%")
                    print(f"Продавцы:   {total_ask_rubles/1_000_000:>6.1f}М ₽ [{create_bar(ask_percent)}] {ask_percent:.1f}%")
                    print("=" * 65)
                    
                    # Заголовок таблицы
                    print(f"{'Объем (Покупка)':<16} | {'Цена ₽':<10} || {'Цена ₽':>10} | {'Объем (Продажа)':>16}")
                    print("-" * 65)
                    
                    # Рисуем 10 строк стакана
                    for i in range(10):
                        bid_qty = str(bids[i]["qty"]) if i < len(bids) else "-"
                        bid_price = f"{bids[i]['price']:.2f}" if i < len(bids) else "-"
                        
                        ask_qty = str(asks[i]["qty"]) if i < len(asks) else "-"
                        ask_price = f"{asks[i]['price']:.2f}" if i < len(asks) else "-"
                        
                        print(f"{bid_qty:<16} | {bid_price:<10} || {ask_price:>10} | {ask_qty:>16}")
                        
                    print("=" * 65)
                    print("Нажмите Ctrl+C для остановки...")
                    
        except Exception as e:
            print(f"❌ Произошла ошибка: {e}")

if __name__ == "__main__":
    try:
        # Запускаем асинхронный цикл
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Работа радара завершена.")