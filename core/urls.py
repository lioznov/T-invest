from django.contrib import admin
from django.urls import path
from dashboard import views

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Теперь при открытии сайта сразу будет загружаться реальный рынок!
    path('', views.real_market_page, name='home'),
    path('api/real-data/', views.api_real_data, name='api_real_data'),
]