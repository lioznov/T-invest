"""Bounded read-only v3 evidence; no broker calls or trading authorization."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from .candle_patterns_v3 import VARIANTS
from .pattern_flow_research import _model_valid, _quality_valid
from .pattern_flow_study import PERIODS, TICKERS

SOURCES = {"candle_patterns_v3.py", "pattern_study.py", "risk.py", "signals.py", "method_study.py",
           "strategy_search.py", "pattern_flow_study.py", "management/commands/study_patterns.py",
           "management/commands/study_pattern_flow.py", "management/commands/study_candle_patterns.py"}


def load_candle_research(ticker):
    empty = {"status": "insufficient", "patterns": [], "entry_allowed": False,
             "message": "Проверяемого отчёта по дополнительным свечным паттернам пока нет."}
    directory = (Path(settings.BASE_DIR)/"data").resolve()
    try:
        paths = sorted(directory.glob("candle-patterns-study-*.json"), reverse=True)

        def read(path, limit):
            if not path.resolve().is_relative_to(directory) or path.stat().st_size > limit:
                raise ValueError("Invalid report path/size")
            data = path.read_bytes()
            if len(data) > limit:
                raise ValueError("Invalid report size")
            return data, json.loads(data)

        if not paths:
            return empty
        raw, protocol = read(directory/"candle-patterns-protocol-v3.json", 50000)
        source = Path(__file__).resolve().parent
        if (set(protocol["source_sha256"]) != SOURCES
                or protocol["source_sha256"] != {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in SOURCES}
                or protocol["spec"]["version"] != "candle_patterns_v3"
                or protocol["spec"]["variants"] != VARIANTS):
            return empty
        _, saved = read(paths[0], 2_000_000)
        if saved["version"] != "candle_patterns_v3" or saved["protocol_sha256"] != hashlib.sha256(raw).hexdigest():
            return empty
        if datetime.fromisoformat(saved["at"]).tzinfo is None:
            return empty
        results = saved["results"]
        if len(results) != len(TICKERS) or {r["ticker"] for r in results} != set(TICKERS):
            return empty
        for row in results:
            if not _quality_valid(row["quality"]) or set(row["variants"]) != set(VARIANTS):
                return empty
            for models in row["variants"].values():
                if set(models) != set(PERIODS) or not all(_model_valid(m) for m in models.values()):
                    return empty
        row = next((r for r in results if r["ticker"] == ticker), None)
        if row is None:
            return {**empty, "message": "Акция не участвовала в дополнительной проверке. Отсутствие результатов не означает нулевую доходность."}
        patterns = []
        for key, variant in VARIANTS.items():
            model = row["variants"][key]["recent"]
            enough = model["trades"] > 0
            patterns.append({"key": key, "name": variant["name"], "trades": model["trades"],
                             **{k: model[k] if enough else None for k in ("return_pct", "stress_return_pct", "win_rate_pct")},
                             "verdict": "insufficient" if not enough else "failed" if model["return_pct"] <= 0 or model["stress_return_pct"] <= 0 or model["unverified_trades"] else "exploratory"})
        return {"status": "ok", "entry_allowed": False, "patterns": patterns, "at": saved["at"],
                "message": "Дополнительные свечные паттерны: 1 сентября — 2 октября 2026, с расходами и двойными расходами. Ноль сделок — недостаточно данных, а не прибыль 0%.",
                "note": saved["note"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        return empty
