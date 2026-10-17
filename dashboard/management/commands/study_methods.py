"""Repeatable research of five fixed hypotheses on locally saved broker candles."""
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.method_study import METHODS, RULES, PROTOCOL_VERSION, compare_methods
from dashboard.signals import Candle


def read_datasets(directory, tickers):
    best = {}
    for path in sorted(directory.glob("research-*.json")):
        try:
            if path.stat().st_size > 100_000_000:
                continue
            saved = json.loads(path.read_text(encoding="utf-8"))
            for row in saved.get("results", []):
                ticker = row.get("ticker")
                if ticker not in tickers or not row.get("candles"):
                    continue
                instrument = row.get("instrument", {})
                candles = [Candle(datetime.fromisoformat(c["time"]),
                                  *(float(c[k]) for k in ("open", "high", "low", "close")),
                                  int(c["volume"])) for c in row["candles"]]
                from dashboard.method_study import validate_candles
                validate_candles(candles)
                lot = instrument.get("lot")
                if not isinstance(lot, int) or lot <= 0:
                    continue
                rank = (len(candles), candles[-1].time)
                if ticker not in best or rank > best[ticker]["rank"]:
                    best[ticker] = {"candles": candles, "instrument": instrument,
                                    "source": path.name, "saved_at": saved.get("at"),
                                    "rank": rank}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return best


class Command(BaseCommand):
    help = "Test five fixed intraday hypotheses; save input snapshots and a readable report."

    def add_arguments(self, parser):
        parser.add_argument("--tickers", nargs="+", default=["SBER", "GAZP", "LKOH", "NVTK", "GMKN", "ROSN", "YDEX"])
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--refresh", action="store_true", help="Append missing completed candles from T-Bank, at most 14 days")
        source.add_argument("--input", help="Replay an exact method-study JSON snapshot inside data/, without broker calls")

    def handle(self, *args, **options):
        tickers = list(dict.fromkeys(t.upper() for t in options["tickers"]))
        if not 1 <= len(tickers) <= 20 or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", t) for t in tickers):
            raise CommandError("Use 1–20 valid tickers.")
        directory = Path(settings.BASE_DIR)/"data"
        replayed_from = None
        if options["input"]:
            source = Path(options["input"]).resolve()
            if not source.is_relative_to(directory.resolve()) or not source.is_file() or source.stat().st_size > 100_000_000:
                raise CommandError("Input must be an existing study snapshot inside data/ (at most 100 MB).")
            try:
                from dashboard.method_study import validate_candles
                saved = json.loads(source.read_text(encoding="utf-8"))
                datasets = {}
                for row in saved["results"]:
                    if row["ticker"] not in tickers or not row.get("candles"):
                        continue
                    candles = [Candle(datetime.fromisoformat(c["time"]),
                                      *(float(c[k]) for k in ("open", "high", "low", "close")),
                                      int(c["volume"])) for c in row["candles"]]
                    validate_candles(candles)
                    boundary = datetime.fromisoformat(row["original_last_candle"])
                    original = [c for c in candles if c.time <= boundary]
                    if not original or original[-1].time != boundary:
                        raise ValueError("Missing original archive boundary")
                    datasets[row["ticker"]] = {"candles": original, "replay_candles": candles,
                                               "instrument": row["instrument"], "source": row["source"],
                                               "saved_at": row["source_saved_at"],
                                               "refresh_error": row.get("refresh_error")}
                replayed_from = {"file": source.name,
                                 "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                raise CommandError("Invalid study input snapshot.") from exc
        else:
            datasets = read_datasets(directory, tickers)
        if not datasets:
            raise CommandError("No saved broker candles with lot sizes; run research_signals first.")
        frozen_at = datetime.now(timezone.utc)
        protocol_source = Path(__file__).resolve().parents[2]/"method_study.py"
        protocol_hash = hashlib.sha256(protocol_source.read_bytes()).hexdigest()
        results = []
        for ticker in tickers:
            dataset = datasets.get(ticker)
            if not dataset:
                results.append({"ticker": ticker, "error": "Нет проверяемых сохранённых свечей и размера лота."})
                continue
            candles = dataset.get("replay_candles", dataset["candles"])
            old_end = dataset["candles"][-1].time
            refresh_error = dataset.get("refresh_error")
            if options["refresh"]:
                try:
                    from dashboard.market_provider import open_client, fetch_candles
                    from t_tech.invest.schemas import CandleInterval
                    start = old_end+timedelta(minutes=15)
                    if frozen_at-start > timedelta(days=14):
                        raise ValueError("Archive is more than 14 days old; refresh through research_signals first.")
                    with open_client() as client:
                        new = fetch_candles(client, dataset["instrument"]["uid"], start,
                                            frozen_at, CandleInterval.CANDLE_INTERVAL_15_MIN, 15)
                    candles = candles + [c for c in new if c.time > old_end]
                except Exception as exc:
                    from dashboard.market_provider import error_message
                    refresh_error = error_message(exc)
                    self.stderr.write(f"{ticker}: refresh unavailable; using dated local archive.")
            # Do not mix an append-only sample with the original four periods.
            # It is new to this archive, but already existed when rules were frozen.
            primary = compare_methods(dataset["candles"], dataset["instrument"]["lot"])
            append_start = next((i for i, c in enumerate(candles) if c.time > old_end), len(candles))
            recent = None
            if append_start < len(candles):
                from dashboard.method_study import study_features, study_signals, simulate_method
                rows = study_features(candles)
                flags = study_signals(candles, rows)
                recent = {"start": candles[append_start].time.isoformat(),
                          "end": (candles[-1].time+timedelta(minutes=15)).isoformat(),
                          "bars": len(candles)-append_start, "methods": []}
                for key, name in METHODS.items():
                    model = simulate_method(candles, rows, flags[key], append_start,
                                            len(candles), dataset["instrument"]["lot"])
                    model.pop("curve")
                    recent["methods"].append({"key": key, "name": name, **model})
            raw = [asdict(c) for c in candles]
            encoded = json.dumps(raw, default=lambda x: x.isoformat(), sort_keys=True).encode("utf-8")
            result = {"ticker": ticker, "instrument": dataset["instrument"],
                      "source": dataset["source"], "source_saved_at": dataset["saved_at"],
                      "original_last_candle": old_end.isoformat(), "refresh_error": refresh_error,
                      "candles_sha256": hashlib.sha256(encoded).hexdigest(),
                      "candles": raw, "result": primary, "recent_append": recent}
            results.append(result)
            self.stdout.write(f"{ticker}: {primary['status']}; {len(dataset['candles'])} archived bars; {len(candles)-append_start} appended")
            for method in primary.get("methods", []):
                self.stdout.write(f"  {method['key']}: {method['return_pct']:+.2f}%; {method['trades']} trades; DD {method['max_drawdown_pct']:.2f}%; {method['verdict']}")
        summaries = []
        good = [r for r in results if r.get("result", {}).get("status") == "ok"]
        for key, name in METHODS.items():
            selected = [next(m for m in r["result"]["methods"] if m["key"] == key) for r in good]
            deltas = []
            for r in good:
                baseline = r["result"]["methods"][0]
                method = next(m for m in r["result"]["methods"] if m["key"] == key)
                deltas.extend(b["return_pct"]-a["return_pct"] for a, b in zip(baseline["windows"], method["windows"]))
            summaries.append({"key": key, "name": name, "tickers": len(selected),
                              "trades": sum(m["trades"] for m in selected),
                              "positive_tickers": sum(m["return_pct"] > 0 for m in selected),
                              "positive_windows": sum(m["positive_windows"] for m in selected),
                              "tested_windows": 4*len(selected),
                              "median_return_pct": median(m["return_pct"] for m in selected) if selected else None,
                              "median_stress_return_pct": median(m["stress_return_pct"] for m in selected) if selected else None,
                              "median_drawdown_pct": median(m["max_drawdown_pct"] for m in selected) if selected else None,
                              "median_change_vs_baseline_pp": median(deltas) if deltas else None,
                              "windows_better_than_baseline": sum(d > 0 for d in deltas),
                              "preliminary_positive_tickers": sum(m["verdict"] == "positive_unproven" for m in selected)})
        report = {"schema_version": 1, "at": frozen_at.isoformat(),
                  "protocol": PROTOCOL_VERSION, "protocol_sha256": protocol_hash, "rules": RULES,
                  "replayed_from": replayed_from,
                  "summaries": summaries, "results": results,
                  "limitations": ["Exploratory reuse of already inspected history, not untouched holdout.",
                                  "Recent append is retrospective too, not future data after protocol freeze.",
                                  "No portfolio aggregation across simultaneous stock positions.",
                                  "OHLC execution assumes available liquidity; historical spreads are absent.",
                                  "Mark-to-close drawdown may miss intrabar losses. Taxes, dividends, tick rounding and corporate actions are not modeled.",
                                  "No private trader identities or time-stamped public trade disclosures are in these inputs.",
                                  "Five hypotheses and seven selected stocks introduce multiple-testing and selection bias."]}
        directory.mkdir(exist_ok=True)
        path = directory/f"method-study-{frozen_at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, default=lambda x: x.isoformat(), allow_nan=False), encoding="utf-8")
        self.stdout.write(f"Report and input snapshot: {path}")
