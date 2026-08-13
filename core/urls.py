from django.contrib import admin
from django.urls import path
from dashboard import views

urlpatterns = [
    path('admin/', admin.site.urls),

    path('', views.real_market_page, name='home'),
    path('api/real-data/', views.api_real_data, name='api_real_data'),

    # ВОТ ЭТА СТРОЧКА ОТПРАВЛЯЛА 404 ОШИБКУ НА НОУТБУКЕ:
    path('api/history/', views.api_history_data, name='api_history_data'),
]