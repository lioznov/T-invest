from django.contrib import admin
from django.urls import path
from dashboard import views

urlpatterns = [
    path('admin/', admin.site.urls),

    path('', views.real_market_page, name='home'),
    path('api/real-data/', views.api_real_data, name='api_real_data'),
    path('api/history/', views.api_history_data, name='api_history_data'),

    # Новые пути для портфеля
    path('portfolio/', views.portfolio_page, name='portfolio_page'),
    path('api/portfolio/', views.api_portfolio_data, name='api_portfolio_data'),
]