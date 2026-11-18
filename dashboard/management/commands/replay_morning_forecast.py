"""Reproduce morning forecast errors from cached, verified exchange candles."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.morning_evidence import replay
from dashboard.morning_service import load_artifact


class Command(BaseCommand):
    help = "Проверить сохранённую утреннюю модель по биржевым свечам из локального архива."

    def handle(self, *args, **options):
        artifact = load_artifact()
        if not artifact:
            raise CommandError("Проверенная утренняя модель недоступна.")
        try:
            report = replay(artifact, Path(settings.BASE_DIR) / "data")
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
