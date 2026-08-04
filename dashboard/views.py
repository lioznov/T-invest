from django.shortcuts import render
from .t_invest_helper import get_my_portfolio


def portfolio_view(request):
    # Вызываем нашу функцию для получения данных
    portfolio_data = get_my_portfolio()

    # Передаем полученные данные в HTML-шаблон
    return render(request, 'portfolio.html', context={'portfolio': portfolio_data})