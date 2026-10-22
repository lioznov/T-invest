"""Bounded reader of compact exploratory research; never reads raw candle archives."""
import hashlib
import json
import re
from datetime import datetime
from math import isfinite
from pathlib import Path

from django.conf import settings


PROTOCOL = "pattern_study_v1"
MAX_SUMMARY_BYTES = 2_000_000
SOURCE_MODULES = ("price_patterns.py", "pattern_study.py", "risk.py", "signals.py")
VERDICTS = {"exploratory", "failed", "insufficient"}
NOTE = ("Это повторное исследование уже просмотренной истории, а не независимая проверка. "
        "Результат не является рекомендацией покупать или вероятностью заработка; "
        "исторический стакан и реальные исполнения отсутствуют.")


def _current_hashes():
    directory = Path(__file__).resolve().parent
    return {name: hashlib.sha256((directory/name).read_bytes()).hexdigest()
            for name in SOURCE_MODULES}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def _valid_summary(saved, hashes):
    """Reject unsupported schemas, stale source versions and non-finite metrics."""
    if (not isinstance(saved, dict) or not isinstance(saved.get("schema_version"), int) or saved.get("schema_version") != 1
            or isinstance(saved.get("schema_version"), bool) or saved.get("protocol") != PROTOCOL):
        return False
    at = datetime.fromisoformat(saved["at"])
    if at.tzinfo is None:
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", saved["protocol_sha256"]):
        return False
    source = saved["source"]
    if (not isinstance(source, dict) or not isinstance(source.get("file"), str)
            or Path(source["file"]).name != source["file"]
            or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])):
        return False
    recorded = saved["source_sha256"]
    if not isinstance(recorded, dict) or any(recorded.get(k) != v for k, v in hashes.items()):
        return False
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in recorded.values()):
        return False
    results = saved["results"]
    if not isinstance(results, list) or not 1 <= len(results) <= 20:
        return False
    seen = set()
    for row in results:
        ticker = row["ticker"]
        if (not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", ticker)
                or ticker in seen or row["status"] not in ("ok", "insufficient")):
            return False
        seen.add(ticker)
        patterns = row["patterns"]
        if not isinstance(patterns, list) or len(patterns) != 6:
            return False
        keys = set()
        for pattern in patterns:
            if (not isinstance(pattern["key"], str) or not 1 <= len(pattern["key"]) <= 80
                    or pattern["key"] in keys or not isinstance(pattern["name"], str)
                    or not 1 <= len(pattern["name"]) <= 200 or pattern["verdict"] not in VERDICTS):
                return False
            keys.add(pattern["key"])
            trades = pattern["trades"]
            if isinstance(trades, bool) or not isinstance(trades, int) or not 0 <= trades <= 1_000_000:
                return False
            for key in ("return_pct", "stress_return_pct", "win_rate_pct", "max_drawdown_pct"):
                value = pattern[key]
                if value is not None and not _number(value):
                    return False
            if pattern["verdict"] != "insufficient":
                if any(pattern[k] is None for k in ("return_pct", "stress_return_pct", "max_drawdown_pct")):
                    return False
                if not 0 <= pattern["max_drawdown_pct"] <= 100:
                    return False
                if trades and (pattern["win_rate_pct"] is None or not 0 <= pattern["win_rate_pct"] <= 100):
                    return False
    return True


def load_pattern_research(ticker):
    """Return latest valid summary for one ticker, failing closed without raw JSON."""
    empty = {"status": "insufficient", "message": "Нет проверяемого компактного отчёта.",
             "at": None, "patterns": [], "note": NOTE}
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", ticker.upper()):
        return {**empty, "message": "Некорректный тикер для исследования."}
    ticker = ticker.upper()
    directory = (Path(settings.BASE_DIR)/"data").resolve()
    try:
        paths = sorted(directory.glob("pattern-study-summary-*.json"), reverse=True)
        if not paths:
            return empty
        hashes = _current_hashes()
        for path in paths:
            try:
                if not path.resolve().is_relative_to(directory) or path.stat().st_size > MAX_SUMMARY_BYTES:
                    continue
                raw = path.read_bytes()
                if len(raw) > MAX_SUMMARY_BYTES:
                    continue
                saved = json.loads(raw.decode("utf-8"))
                if not _valid_summary(saved, hashes):
                    continue
                row = next((r for r in saved["results"] if r["ticker"] == ticker), None)
                if row is None:
                    return {**empty, "at": saved["at"], "message": "Эта акция отсутствует в последнем исследовании; результат не считается нулевым."}
                enough = row["status"] == "ok" and any(p["trades"] for p in row["patterns"])
                return {"status": "ok" if enough else "insufficient",
                        "message": "Исследовательские результаты за 1 сентября — 2 октября 2026, не торговая рекомендация." if enough else
                                   "Недостаточно проверяемых данных или сделок по этой акции.",
                        "at": saved["at"], "patterns": row["patterns"], "note": NOTE}
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
    except (OSError, ValueError, TypeError):
        pass
    return empty
