import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.rotation_search import VERSION, ROTATIONS, prepare, simulate_rotation
from dashboard.strategy_search import DISCOVERY_TICKERS, DEVELOPMENT, VALIDATION, feature_rows
from .search_strategies import acquire


class Command(BaseCommand):
    help = "Compare registered long-only stock selection hypotheses."

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/"data"
        directory.mkdir(exist_ok=True)
        at = datetime.now(timezone.utc)
        source = Path(__file__).resolve().parents[2]/"rotation_search.py"
        protocol = {"version": VERSION, "at": at.isoformat(),
                    "registry": [asdict(r) for r in ROTATIONS],
                    "code_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "development": DEVELOPMENT, "validation": VALIDATION,
                    "selection": "No final candidate without positive normal and stress results in both periods; minimum 20 and 4 trades",
                    "past_trials": "24 candle rules and 12 rotations already explored. Six less frequent reversal rules added after observing weak weekly reversal; the validation period is no longer untouched."}
        frozen = directory/f"rotation-search-protocol-{VERSION.rsplit('_', 1)[-1]}.json"
        if frozen.exists():
            old = json.loads(frozen.read_text(encoding="utf-8"))
            if old["code_sha256"] != protocol["code_sha256"]:
                raise CommandError("Code changed after registration; preserve this experiment.")
            protocol = old
        else:
            frozen.write_text(json.dumps(protocol, ensure_ascii=False), encoding="utf-8")
        data = {}
        for ticker in DISCOVERY_TICKERS:
            saved, bars = acquire(directory, ticker, "day", False, at)
            data[ticker] = prepare(saved, bars, feature_rows(bars))
        results = []
        for rule in ROTATIONS:
            first = simulate_rotation(data, rule, *DEVELOPMENT)
            second = simulate_rotation(data, rule, *VALIDATION)
            passed = bool(first["return_pct"] > 0 and first["stress_return_pct"] > 0
                          and second["return_pct"] > 0 and second["stress_return_pct"] > 0
                          and first["trades"] >= 20 and second["trades"] >= 4
                          and first["unverified_trades"] == second["unverified_trades"] == 0)
            results.append({"rule": asdict(rule), "development": first, "validation": second, "passes_screen": passed})
            self.stdout.write(f"{rule.key}: development {first['return_pct']:+.3f}% ({first['trades']}); validation {second['return_pct']:+.3f}% ({second['trades']}); passes={passed}")
        path = directory/f"rotation-search-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        path.write_text(json.dumps({"protocol": protocol, "results": results}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        self.stdout.write(f"Report: {path}")
