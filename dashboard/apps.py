from django.apps import AppConfig


class DashboardConfig(AppConfig):
    name = 'dashboard'

    def ready(self):
        import os
        import sys
        # No background work in tests, migrations or the autoreloader parent.
        if 'runserver' in sys.argv and ('--noreload' in sys.argv or os.getenv('RUN_MAIN') == 'true'):
            # Initialize the SDK once before request/analysis threads can import
            # its mutually dependent schemas and services in different orders.
            import t_tech.invest
            from .telegram_alerts import ensure_worker
            ensure_worker()
            from .vk_alerts import ensure_worker as ensure_vk_worker
            ensure_vk_worker()
