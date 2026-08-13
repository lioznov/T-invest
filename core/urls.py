from django.contrib import admin
from django.urls import path
from dashboard import views

urlpatterns = [
    path('admin/', admin.site.urls),

    path('', views.real_market_page, name='home'),
    path('api/real-data/', views.api_real_data, name='api_real_data'),

<<<<<<< HEAD
    # ВОТ ЭТА СТРОЧКА ОТПРАВЛЯЛА 404 ОШИБКУ НА НОУТБУКЕ:
=======
    # НОВАЯ ССЫЛКА ДЛЯ ИСТОРИИ ГРАФИКА
>>>>>>> f7f7735eeefa842427d7ac48ff88a0857287a069
    path('api/history/', views.api_history_data, name='api_history_data'),
]