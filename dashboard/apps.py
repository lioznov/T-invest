from django.apps import AppConfig


class DashboardConfig(AppConfig):
    name = 'dashboard'

    def ready(self):
        import os
        import sys
        # No background work in tests, migrations or the autoreloader parent.
        if 'runserver' in sys.argv and ('--noreload' in sys.argv or os.getenv('RUN_MAIN') == 'true'):
            from .telegram_alerts import ensure_worker
            ensure_worker()
