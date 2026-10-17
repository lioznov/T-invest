"""Freeze six v3 hypotheses before replaying immutable exchange inputs."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.candle_patterns_v3 import RULES, VARIANTS, scan_candle_patterns
from dashboard.management.commands.study_pattern_flow import acquire, write_immutable
from dashboard.pattern_flow_study import PERIODS, RESERVES, TICKERS, classify_flow, passes_flow_screen
from dashboard.pattern_study import simulate_pattern
from dashboard.strategy_search import aggregate


NOTE = ("Шесть дополнительных исследовательских гипотез после 59 прежних вариантов. "
        "Эти периоды уже изучались: независимой проверки здесь нет. Исторического стакана, "
        "налогов, дивидендов и гарантированных исполнений нет. Даже прохождение фильтра не разрешает LONG.")


def freeze(directory):
    modules = Path(__file__).resolve().parents[2]
    names = ("candle_patterns_v3.py", "pattern_study.py", "risk.py", "signals.py", "method_study.py",
             "strategy_search.py", "pattern_flow_study.py", "management/commands/study_patterns.py",
             "management/commands/study_pattern_flow.py", "management/commands/study_candle_patterns.py")
    spec = {"version": "candle_patterns_v3", "previous_trials": 59, "variants": VARIANTS,
            "rules": RULES, "tickers": TICKERS, "reserves_not_read": RESERVES, "periods": PERIODS,
            "engine": "unchanged simulate_pattern: next open; stop first; 2R; 180min/session close",
            "capital_per_stock": 100000, "risk_pct": .5, "allocation_pct": 25,
            "fee_bps_per_side": 5, "slippage_bps_per_side": 5, "stress_cost_multiplier": 2,
            "eligibility": "unchanged passes_flow_screen; 40/20/20 trades, all seven stocks, all periods positive normal/stress; no unverified trades",
            "entry_allowed": False, "note": NOTE}
    signature = json.loads(json.dumps({"spec": spec,
        "source_sha256": {name: hashlib.sha256((modules/name).read_bytes()).hexdigest() for name in names}},
        ensure_ascii=False, sort_keys=True))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/"candle-patterns-protocol-v3.json"
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CommandError("Unreadable v3 protocol retained; no prices read.") from None
        if any(old.get(k) != v for k, v in signature.items()):
            raise CommandError("Frozen v3 code/rules changed; original retained. Register another version.")
    else:
        write_immutable(path, {"at": datetime.now(timezone.utc).isoformat(), **signature})
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(dataset):
    candles = dataset["candles"]
    events = scan_candle_patterns(candles)
    fractions = [classify_flow(c.volume, b, s)[1]
                 for c, b, s in zip(candles, dataset["buys"], dataset["sells"])]
    filtered = {key: [event if fraction is not None and fraction >= .60 else None
                      for event, fraction in zip(values, fractions)] for key, values in events.items()}
    return {key: {period: simulate_pattern(candles, filtered if variant["flow"] else events,
                                          variant, first, last, dataset["lot"])
                  for period, (first, last) in PERIODS.items()} for key, variant in VARIANTS.items()}


class Command(BaseCommand):
    help = "Six fixed candle pattern tests on cached EXCHANGE candles; no broker calls or orders."

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/"data"
        protocol_hash = freeze(directory)  # BEFORE acquire or any price read.
        datasets = {ticker: acquire(directory, ticker, fetch=False) for ticker in TICKERS}
        results = [{"ticker": ticker, "input": dataset["source"], "quality": dataset["quality"],
                    "variants": evaluate(dataset)} for ticker, dataset in datasets.items()]
        summaries = []
        for key, variant in VARIANTS.items():
            periods = {p: aggregate([r["variants"][key][p] for r in results]) for p in PERIODS}
            passing = passes_flow_screen(periods, {t: d["quality"] for t, d in datasets.items()})
            summaries.append({"key": key, "name": variant["name"], "periods": periods,
                              "passes_exploratory_screen": passing, "entry_allowed": False})
            self.stdout.write(key + ": " + "; ".join(
                f"{p} {v['return_pct']:+.3f}% / stress {v['stress_return_pct']:+.3f}% ({v['trades']} trades)"
                for p, v in periods.items()) + f"; passes={passing}")
        at = datetime.now(timezone.utc)
        path = directory/f"candle-patterns-study-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        write_immutable(path, {"version": "candle_patterns_v3", "protocol_sha256": protocol_hash,
                              "at": at.isoformat(), "note": NOTE, "results": results, "summaries": summaries})
        self.stdout.write(f"Report: {path}\n{NOTE}")
