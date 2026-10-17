import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.rebound_search import VERSION, RULES, RESERVED, simulate_rebound
from dashboard.rotation_search import prepare
from dashboard.strategy_search import DISCOVERY_TICKERS, DEVELOPMENT, VALIDATION, feature_rows
from .search_strategies import acquire


class Command(BaseCommand):
    help = "Eight registered long-only recovery/volume hypotheses, using saved daily inputs."

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/"data"
        directory.mkdir(exist_ok=True)
        at = datetime.now(timezone.utc)
        code = (Path(__file__).resolve().parents[2]/"rebound_search.py").read_bytes()
        protocol = {"version": VERSION, "registry": [asdict(r) for r in RULES],
                    "code_sha256": hashlib.sha256(code).hexdigest(), "registered_at": at.isoformat(),
                    "development": DEVELOPMENT, "validation": VALIDATION,
                    "unopened_reserve": RESERVED,
                    "prior_trials": "42 hypotheses, one failed reserved-stock test; original validation already inspected",
                    "selection": "Positive normal/stress in both periods; 20 development and 4 validation trades, no unverified trades, drawdown below 15%. If several pass: maximum validation stress result, then key."}
        frozen = directory/f"rebound-search-protocol-{VERSION}.json"
        if frozen.exists():
            old = json.loads(frozen.read_text(encoding="utf-8"))
            if old["code_sha256"] != protocol["code_sha256"]:
                raise CommandError("Preserve registered experiment; code changed.")
            protocol = old
        else:
            frozen.write_text(json.dumps(protocol, ensure_ascii=False), encoding="utf-8")
        data = {}
        for ticker in DISCOVERY_TICKERS:
            saved, bars = acquire(directory, ticker, "day", False, at)
            data[ticker] = prepare(saved, bars, feature_rows(bars))
        results = []
        for rule in RULES:
            first, second = (simulate_rebound(data, rule, *dates) for dates in (DEVELOPMENT, VALIDATION))
            passed = (all(r["return_pct"] > 0 and r["stress_return_pct"] > 0
                          and r["unverified_trades"] == 0 and r["max_drawdown_pct"] < 15
                          for r in (first, second)) and first["trades"] >= 20 and second["trades"] >= 4)
            results.append({"rule": asdict(rule), "development": first, "validation": second, "passes_screen": passed})
            self.stdout.write(f"{rule.key}: development {first['return_pct']:+.4f}%/{first['stress_return_pct']:+.4f}% stress ({first['trades']}); validation {second['return_pct']:+.4f}%/{second['stress_return_pct']:+.4f}% stress ({second['trades']}); passes={passed}")
        candidates = [r for r in results if r["passes_screen"]]
        chosen = sorted(candidates, key=lambda r: (-r["validation"]["stress_return_pct"], r["rule"]["key"]))[0]["rule"] if candidates else None
        output = directory/f"rebound-search-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        output.write_text(json.dumps({"protocol": protocol, "results": results, "candidate": chosen}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        self.stdout.write(f"Candidate: {chosen}. Report: {output}")
