"""Lock one screened rotation before reading reserved instruments or recent dates."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.rotation_search import VERSION, ROTATIONS, prepare, simulate_rotation
from dashboard.strategy_search import (DISCOVERY_TICKERS, RESERVE_TICKERS,
                                       DEVELOPMENT, VALIDATION, feature_rows)
from .search_strategies import acquire


def select_candidate(report):
    passing = [r for r in report["results"] if r["passes_screen"]]
    if not passing:
        raise CommandError("No screened candidate; do not open reserved inputs.")
    return sorted(passing, key=lambda r: (-r["validation"]["stress_return_pct"], r["rule"]["key"]))[0]


class Command(BaseCommand):
    help = "Freeze and evaluate one long-only rotation; never place broker orders."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Discovery JSON inside data/")
        parser.add_argument("--fetch", action="store_true", help="Read missing reserved daily prices from T-Bank")

    def handle(self, *args, **options):
        directory = (Path(settings.BASE_DIR)/"data").resolve()
        path = (Path(settings.BASE_DIR)/options["input"]).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise CommandError("Input must be an existing JSON inside data/.")
        report = json.loads(path.read_text(encoding="utf-8"))
        code_hash = hashlib.sha256((Path(__file__).resolve().parents[2]/"rotation_search.py").read_bytes()).hexdigest()
        if report["protocol"]["version"] != VERSION or report["protocol"]["code_sha256"] != code_hash:
            raise CommandError("Discovery protocol does not match the frozen algorithm.")
        winner = select_candidate(report)
        rule = next(r for r in ROTATIONS if r.key == winner["rule"]["key"])
        at = datetime.now(timezone.utc)
        choice = {"rule": winner["rule"], "selected_at": at.isoformat(),
                  "code_sha256": code_hash, "source_report": path.name,
                  "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "reserved_tickers": RESERVE_TICKERS,
                  "confirmation_rule": "Reserved full history and both development/validation must each have positive base/stress return and no unverified trades; minimum 20 full trades and 4 validation trades. This is a descriptive screen, not statistical proof.",
                  "recent_rule": "Original universe 2026-09-01 through 2026-10-03, positive base/stress, at least 4 trades and no unverified trades. These dates partially overlap earlier research, so are not a fresh future test."}
        frozen = directory/f"rotation-search-choice-{VERSION.rsplit('_', 1)[-1]}.json"
        if frozen.exists():
            old = json.loads(frozen.read_text(encoding="utf-8"))
            if old["rule"] != choice["rule"] or old["code_sha256"] != code_hash:
                raise CommandError("Another candidate was frozen; its reserved data cannot be reused.")
            choice = old
        else:
            # This write precedes any reserved dataset access.
            frozen.write_text(json.dumps(choice, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(f"FROZEN {rule.key}; no parameter changes during confirmation.")
        reserved = {}
        for ticker in RESERVE_TICKERS:
            saved, bars = acquire(directory, ticker, "day", options["fetch"], at)
            reserved[ticker] = prepare(saved, bars, feature_rows(bars))
            self.stdout.write(f"RESERVED INPUT {ticker}: {len(bars)} weekday bars")
        periods = {"development": DEVELOPMENT, "validation": VALIDATION,
                   "full": (DEVELOPMENT[0], "2026-10-03")}
        held = {key: simulate_rotation(reserved, rule, *dates) for key, dates in periods.items()}
        discovery = {}
        for ticker in DISCOVERY_TICKERS:
            saved, bars = acquire(directory, ticker, "day", False, at)
            discovery[ticker] = prepare(saved, bars, feature_rows(bars))
        recent = simulate_rotation(discovery, rule, "2026-09-01", "2026-10-03")
        held_pass = (all(r["return_pct"] > 0 and r["stress_return_pct"] > 0 and r["unverified_trades"] == 0
                         for r in held.values()) and held["full"]["trades"] >= 20 and held["validation"]["trades"] >= 4)
        recent_pass = (recent["return_pct"] > 0 and recent["stress_return_pct"] > 0
                       and recent["trades"] >= 4 and recent["unverified_trades"] == 0)
        result = {"at": at.isoformat(), "candidate": choice, "reserved": held,
                  "recent_original_universe": recent, "reserved_pass": held_pass,
                  "recent_pass": recent_pass, "passes_descriptive_confirmation": held_pass and recent_pass,
                  "limitations": ["Reserved stocks share historical market regimes with discovery stocks.",
                                  "Multiple hypotheses tried; selecting a positive one is not proof.",
                                  "Daily fills, corporate actions, dividends and taxes require additional validation.",
                                  "No real orders and no change to live site signals."]}
        output = directory/f"rotation-confirmation-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        for key, r in {**held, "recent_original_universe": recent}.items():
            self.stdout.write(f"{key}: {r['return_pct']:+.4f}%; stress {r['stress_return_pct']:+.4f}%; {r['trades']} trades; unverified {r['unverified_trades']}")
        self.stdout.write(f"Confirmation passed: {held_pass and recent_pass}. Report: {output}")
