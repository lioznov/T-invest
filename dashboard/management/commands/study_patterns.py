"""Six pre-registered candle events; exploratory only, no broker or order calls."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.method_study import validate_candles
from dashboard.pattern_research import PROTOCOL, NOTE, SOURCE_MODULES
from dashboard.signals import Candle
from dashboard.strategy_search import aggregate


TICKERS = ("SBER", "GAZP", "LKOH", "NVTK", "GMKN", "ROSN", "YDEX")
RESERVES = ("TATN", "CHMF", "PHOR")
PERIODS = {"development": ("2026-03-30", "2026-08-01"),
           "selection": ("2026-08-01", "2026-09-01"),
           "recent": ("2026-09-01", "2026-10-03")}
DEFAULT_INPUT = "method-study-20261002-172009-234325.json"
MAX_INPUT_BYTES = 100_000_000


def _dump(path, saved, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(saved, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)


def protocol_spec(variants):
    if (not isinstance(variants, dict) or len(variants) != 6
            or {(v["family"], v["volume"]) for v in variants.values()} !=
               {(family, volume) for family in ("reclaim", "orb_retest", "flag") for volume in (False, True)}
            or any(not isinstance(v["volume"], bool) for v in variants.values())):
        raise CommandError("Exactly three event families × two registered volume variants are required.")
    return {"version": PROTOCOL, "registry": variants, "tickers": TICKERS,
            "reserve_tickers": RESERVES, "periods_halfopen": PERIODS,
            "previous_strategy_trials": 50,
            "prior_history": "All three periods have already been inspected in other experiments; recent is not an independent holdout.",
            "execution": {"direction": "long_only", "real_orders": False,
                          "capital_per_stock": 100000, "risk_pct": .5, "allocation_pct": 25,
                          "aggregation": "Equal-capital separate stock accounts; not a shared executable portfolio.",
                          "fee_bps_per_side": 5, "slippage_bps_per_side": 5,
                          "stress_cost_multiplier": 2, "volume_threshold": 1.3},
            "selection": {"all_periods_normal_and_stress_positive": True,
                          "all_periods_unverified_trades": 0,
                          "minimum_accounts_each_period": 7,
                          "minimum_trades": {"development": 40, "selection": 20, "recent": 20},
                          "minimum_positive_stocks": {"development": 4, "selection": 4},
                          "maximum_drawdown_pct_exclusive": 15,
                          "choice": "Highest selection stress return; ascending key breaks ties. Freeze at most one before reading reserve prices."},
            "limitations": [NOTE, "Independent stock reserve is not loaded by this command.",
                            "No historical order book, quote latency, taxes, dividends or guaranteed liquidity are modeled."]}


def freeze_protocol(directory, variants):
    """Freeze source and hypotheses BEFORE reading any input prices; preserve mismatches."""
    # Normalize tuples exactly as persisted JSON, so a faithful replay compares
    # equal rather than mistaking list/tuple encoding for a changed hypothesis.
    spec = json.loads(json.dumps(protocol_spec(variants), ensure_ascii=False, sort_keys=True))
    directory.mkdir(parents=True, exist_ok=True)
    modules = Path(__file__).resolve().parents[2]
    sources = {name: hashlib.sha256((modules/name).read_bytes()).hexdigest() for name in SOURCE_MODULES}
    # The actual aggregation, validation and command are dependencies too.
    sources.update({name: hashlib.sha256((modules/name).read_bytes()).hexdigest()
                    for name in ("strategy_search.py", "method_study.py")})
    sources["study_patterns.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    spec_hash = hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    signature = {"spec": spec, "source_sha256": sources, "protocol_spec_sha256": spec_hash}
    path = directory/"pattern-study-protocol-v1.json"
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            matches = all(old.get(key) == value for key, value in signature.items())
        except (OSError, ValueError, TypeError, AttributeError):
            old, matches = {"error": "Existing protocol is unreadable; original retained."}, False
        if not matches:
            at = datetime.now(timezone.utc)
            audit = directory/f"pattern-study-protocol-mismatch-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
            _dump(audit, {"at": at.isoformat(), "frozen": old, "attempted": signature}, exclusive=True)
            raise CommandError(f"Frozen protocol/source changed. Original retained; audit: {audit.name}. Register a new version, do not overwrite v1.")
        saved = old
    else:
        saved = {"at": datetime.now(timezone.utc).isoformat(), **signature}
        _dump(path, saved, exclusive=True)
    return saved, hashlib.sha256(path.read_bytes()).hexdigest()


def read_input(directory, requested):
    source = directory/DEFAULT_INPUT if not requested else Path(requested)
    if not source.is_absolute():
        resolved = source.resolve()
        source = resolved if resolved.is_relative_to(directory.resolve()) else directory/source
    source = source.resolve()
    if (not source.is_relative_to(directory.resolve()) or not source.is_file()
            or source.stat().st_size > MAX_INPUT_BYTES):
        raise CommandError("Input must be an existing JSON snapshot inside data/, at most 100 MB.")
    try:
        raw = source.read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("Oversized snapshot")
        saved = json.loads(raw.decode("utf-8"))
        if not isinstance(saved, dict) or not isinstance(saved.get("results"), list):
            raise ValueError("Invalid input structure")
    except (OSError, ValueError, TypeError) as exc:
        raise CommandError("Invalid input JSON snapshot.") from exc
    datasets, errors, seen = {}, {}, set()
    for row in saved["results"]:
        if not isinstance(row, dict) or row.get("ticker") not in TICKERS:
            continue
        ticker = row["ticker"]
        if ticker in seen:
            datasets.pop(ticker, None)
            errors[ticker] = "Duplicate ticker in input snapshot."
            continue
        seen.add(ticker)
        try:
            instrument = row["instrument"]
            lot = instrument["lot"]
            if isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
                raise ValueError("Invalid lot")
            candles = []
            for item in row["candles"]:
                volume = item["volume"]
                if isinstance(volume, bool) or not isinstance(volume, int):
                    raise ValueError("Volume must be an integer")
                candles.append(Candle(datetime.fromisoformat(item["time"]),
                                      *(float(item[k]) for k in ("open", "high", "low", "close")), volume))
            if not candles:
                raise ValueError("Empty candles")
            validate_candles(candles)
            datasets[ticker] = {"instrument": instrument, "candles": candles,
                                "original_last_candle": row.get("original_last_candle")}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            errors[ticker] = "Invalid/missing candles or lot; excluded, not counted as zero return."
    return datasets, errors, {"file": source.name, "sha256": hashlib.sha256(raw).hexdigest()}


def eligible(periods):
    if set(periods) != set(PERIODS):
        return False
    for name, summary in periods.items():
        if (summary["accounts"] != len(TICKERS) or summary["return_pct"] is None
                or summary["return_pct"] <= 0 or summary["stress_return_pct"] <= 0
                or summary["unverified_trades"] != 0 or summary["max_drawdown_pct"] >= 15
                or summary["trades"] < {"development": 40, "selection": 20, "recent": 20}[name]):
            return False
    return all(periods[name]["positive_accounts"] >= 4 for name in ("development", "selection"))


def compact_pattern(key, variant, model):
    insufficient = model["status"] != "ok" or not model["trades"]
    good = (not insufficient and not model["unverified_trades"]
            and model["return_pct"] > 0 and model["stress_return_pct"] > 0)
    return {"key": key, "name": variant["name"], "trades": model["trades"],
            **{field: model[field] if model["status"] == "ok" else None
               for field in ("return_pct", "stress_return_pct", "win_rate_pct", "max_drawdown_pct")},
            "verdict": "insufficient" if insufficient else "exploratory" if good else "failed"}


class Command(BaseCommand):
    help = "Research six registered long-only price events on saved 15-minute candles. No broker calls."

    def add_arguments(self, parser):
        parser.add_argument("--input", help="Saved snapshot inside data/; defaults to the fixed October 2 method study.")

    def handle(self, *args, **options):
        from dashboard.price_patterns import scan_patterns
        from dashboard.pattern_study import VARIANTS, simulate_pattern
        directory = (Path(settings.BASE_DIR)/"data").resolve()
        protocol, protocol_hash = freeze_protocol(directory, VARIANTS)
        datasets, errors, source = read_input(directory, options["input"])
        at = datetime.now(timezone.utc)
        results = []
        empty_model = {"status": "insufficient", "trades": 0, "return_pct": None,
                       "stress_return_pct": None, "unverified_trades": 0, "max_drawdown_pct": None,
                       "trade_log": [], "daily_curve": [], "win_rate_pct": None}
        for ticker in TICKERS:
            dataset = datasets.get(ticker)
            patterns = {}
            events = scan_patterns(dataset["candles"]) if dataset else None
            for key, variant in VARIANTS.items():
                periods = {name: simulate_pattern(dataset["candles"], events, variant, first, last,
                                                  dataset["instrument"]["lot"]) if dataset else dict(empty_model)
                           for name, (first, last) in PERIODS.items()}
                patterns[key] = periods
            results.append({"ticker": ticker, "status": "ok" if dataset else "insufficient",
                            "error": errors.get(ticker, None if dataset else "No input row for ticker."),
                            "instrument": dataset["instrument"] if dataset else None, "patterns": patterns})
        summaries = []
        for key, variant in VARIANTS.items():
            periods = {name: aggregate([r["patterns"][key][name] for r in results]) for name in PERIODS}
            for summary in periods.values():
                if not summary["accounts"]:
                    summary.update(return_pct=None, stress_return_pct=None, max_drawdown_pct=None)
            summaries.append({"key": key, "name": variant["name"], "periods": periods,
                              "eligible": eligible(periods)})
            values = "; ".join(f"{name}: {p['return_pct']:+.3f}%/{p['stress_return_pct']:+.3f}% stress, {p['trades']} trades, {p['accounts']}/7 stocks"
                               if p["return_pct"] is not None else f"{name}: insufficient" for name, p in periods.items())
            self.stdout.write(f"{key}: {values}; eligible={summaries[-1]['eligible']}")
        candidates = sorted((s for s in summaries if s["eligible"]),
                            key=lambda s: (-s["periods"]["selection"]["stress_return_pct"], s["key"]))
        choice_path = directory/"pattern-study-choice-v1.json"
        candidate = None
        if choice_path.exists() and not candidates:
            raise CommandError("A candidate was already frozen but does not qualify in this replay. Original choice retained; do not silently replace or hide it.")
        if candidates:
            chosen = candidates[0]
            candidate = json.loads(json.dumps({"key": chosen["key"], "variant": VARIANTS[chosen["key"]],
                         "protocol_sha256": protocol_hash, "source": source,
                         "selection_stress_return_pct": chosen["periods"]["selection"]["stress_return_pct"],
                         "reserve_tickers": RESERVES, "status": "frozen_exploratory_not_confirmed"}))
            if choice_path.exists():
                old_choice = json.loads(choice_path.read_text(encoding="utf-8"))
                if any(old_choice.get(k) != v for k, v in candidate.items()):
                    raise CommandError("An existing candidate is already frozen; do not replace it after new results.")
                candidate = old_choice
            else:
                candidate = {"at": at.isoformat(), **candidate}
                _dump(choice_path, candidate, exclusive=True)
        metadata = {"schema_version": 1, "protocol": PROTOCOL, "at": at.isoformat(),
                    "protocol_sha256": protocol_hash, "source_sha256": protocol["source_sha256"],
                    "source": source, "periods": PERIODS, "candidate": candidate, "note": NOTE}
        stamp = at.strftime("%Y%m%d-%H%M%S-%f")
        full_path = directory/f"pattern-study-{stamp}.json"
        _dump(full_path, {**metadata, "results": results, "summaries": summaries}, exclusive=True)
        compact_results = [{"ticker": r["ticker"], "status": r["status"], "error": r["error"],
                            "patterns": [compact_pattern(key, VARIANTS[key], r["patterns"][key]["recent"])
                                         for key in VARIANTS]} for r in results]
        compact_summaries = [{**s, "periods": {name: {k: v for k, v in p.items() if k != "daily_curve"}
                                               for name, p in s["periods"].items()}} for s in summaries]
        summary_path = directory/f"pattern-study-summary-{stamp}.json"
        _dump(summary_path, {**metadata, "results": compact_results, "summaries": compact_summaries}, exclusive=True)
        self.stdout.write(f"Full report: {full_path}\nCompact report: {summary_path}\nNo reserve data was read. {NOTE}")
