from django.contrib import admin
from django.urls import path
from dashboard import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.real_market_page, name='home'),
    path('portfolio/', views.portfolio_page, name='portfolio_page'),
    path('api/real-data/', views.api_real_data, name='api_real_data'),
    path('api/history/', views.api_history_data, name='api_history_data'),
    path('api/portfolio/', views.api_portfolio_data, name='api_portfolio_data'),

    # === НОВЫЙ МАРШРУТ ДЛЯ ПОИСКА ===
    path('api/search/', views.api_search, name='api_search'),
    path('radar/', views.radar_page, name='radar'),
    path('api/radar-data/', views.api_radar_data, name='api_radar_data'),
    path('api/radar-add/', views.api_radar_add, name='api_radar_add'),
    path('api/radar-remove/', views.api_radar_remove, name='api_radar_remove'),
]