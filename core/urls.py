from django.contrib import admin
from django.urls import path
from dashboard import signal_views


def legacy(name):
    # Import the original terminal only when it is actually opened.
    def handler(request, *args, **kwargs):
        from dashboard import views
        return getattr(views, name)(request, *args, **kwargs)
    return handler


class LegacyViews:
    def __getattr__(self, name):
        return legacy(name)


views = LegacyViews()

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', signal_views.page, name='home'),
    path('watchlist/', signal_views.page, {'watchlist': True}, name='watchlist'),
    path('api/signals/', signal_views.signal, name='signals'),
    path('api/research/', signal_views.research, name='research'),
    path('api/forward/', signal_views.forward, name='forward'),
    path('api/paper/', signal_views.paper, name='paper'),
    path('api/risk/', signal_views.risk, name='risk'),
    path('terminal/', views.real_market_page, name='terminal'),
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
