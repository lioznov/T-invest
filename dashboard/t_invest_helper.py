import os
import certifi
from t_tech.invest import Client
from t_tech.invest.exceptions import RequestError

# Железобетонная защита от ошибки SSL_ERROR_SSL (перехват сертификатов)
os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certifi.where()


def get_my_portfolio():
    """
    Подключается к T-Invest API по токену из .env,
    находит первый активный счет и возвращает базовые данные портфеля.
    """
    token = os.getenv("INVEST_TOKEN")

    if not token:
        return {"error": "Токен не найден! Проверьте файл .env в корне проекта."}

    try:
        # Открываем защищенное соединение
        with Client(token) as client:
            # 1. Получаем список счетов
            accounts = client.users.get_accounts().accounts
            if not accounts:
                return {"error": "У вас нет открытых брокерских счетов."}

            # Берем ID первого активного счета
            account_id = accounts[0].id

            # 2. Получаем данные портфеля
            portfolio = client.operations.get_portfolio(account_id=account_id)

            # 3. Возвращаем удобный словарь
            return {
                "account_id": account_id,
                "total_shares_rub": portfolio.total_amount_shares.units + (portfolio.total_amount_shares.nano / 1e9),
                "total_bonds_rub": portfolio.total_amount_bonds.units + (portfolio.total_amount_bonds.nano / 1e9),
                "total_etf_rub": portfolio.total_amount_etf.units + (portfolio.total_amount_etf.nano / 1e9),
                "positions_count": len(portfolio.positions),
            }

    except RequestError as e:
        return {"error": f"Ошибка ответа API Т-Банка: {str(e)}"}
    except Exception as e:
        return {"error": f"Внутренняя ошибка: {str(e)}"}