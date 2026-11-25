from django.contrib import admin
from django.urls import path
from dashboard import signal_views
from dashboard import news_views
from dashboard import telegram_views
from dashboard import vk_views
from dashboard import radar_vk_views
from dashboard import notification_views
from dashboard import morning_views
from dashboard import auto_morning_views
from dashboard import close_timing_views
from dashboard import multiyear_views
from dashboard import temporal_validation_views


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
    path('news/', news_views.page, name='news'),
    path('api/news/', news_views.feed, name='news_feed'),
    path('api/news/calendar/',news_views.calendar,name='news_calendar'),
    path('api/news/reaction/',news_views.reaction,name='news_reaction'),
    path('api/signals/', signal_views.signal, name='signals'),
    path('api/telegram-alerts/', telegram_views.alerts, name='telegram_alerts'),
    path('api/vk-alerts/', vk_views.alerts, name='vk_alerts'),
    path('api/radar-vk/', radar_vk_views.alerts, name='radar_vk'),
    path('api/notifications/', notification_views.settings, name='notification_settings'),
    path('api/ai-model/', signal_views.ai_model, name='ai_model'),
    path('api/morning-forecast/', morning_views.forecast, name='morning_forecast'),
    path('api/analysis/morning/', auto_morning_views.morning, name='auto_morning'),
    path('api/morning-forecast/job/', morning_views.forecast_job, name='morning_forecast_job'),
    path('api/close-timing/', close_timing_views.report, name='close_timing'),
    path('api/multiyear-research/', multiyear_views.report, name='multiyear_research'),
    path('api/temporal-validation/', temporal_validation_views.report, name='temporal_validation'),
    path('api/monitor-health/', signal_views.monitor_health, name='monitor_health'),
    path('api/research/', signal_views.research, name='research'),
    path('api/pattern-research/', signal_views.pattern_research, name='pattern_research'),
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
