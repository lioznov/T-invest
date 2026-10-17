"""Fail-closed display of preregistered joint evidence; no trade approval."""
import hashlib
import json
import math
from pathlib import Path
from django.conf import settings
from .joint_study import VERSION, VARIANTS, NAMES, RULES, summarize, matched
from .management.commands.study_joint import hashes
from .microstructure_archive import utc_timestamp


NOTE = ("Средняя условная сделка, не доходность счёта. Минутный пробой — отдельный эксперимент, не паттерны 15м сайта. "
        "Вход по следующей цене продавца, выход через 3 минуты по цене покупателя. Комиссия и проскальзывание "
        "по 0,05% за каждую сторону, стресс ×2. Разрывы связи, плохие котировки и остановки торгов — неизвестные исходы. "
        "Полнота ленты, очередь и исполнение не гарантированы. Сделки обезличены: число разных трейдеров неизвестно. "
        "Положительный результат одного дня не подтверждает прибыльность; LONG не разрешён.")


def load_joint_research(ticker, phase="replay"):
    empty = {"status": "insufficient", "entry_allowed": False, "variants": [],
             "title": "Новые данные после фиксации правил" if phase == "forward" else "График + покупки + стакан: совместный тест",
             "message": "Проверенного отчёта совместного теста пока нет; отсутствие сделок — не нулевая доходность."}
    directory = (Path(settings.BASE_DIR)/"data").resolve()
    def read(path, limit):
        if path.resolve().parent != directory or path.stat().st_size > limit:
            raise ValueError("Invalid path/size")
        raw = path.read_bytes()
        if len(raw) > limit:
            raise ValueError("Oversized report")
        return raw, json.loads(raw)
    try:
        if phase not in ("replay", "forward"):
            return empty
        protocol_raw, protocol = read(directory/"joint-stream-protocol-v1.json", 50000)
        if (protocol["version"] != VERSION or protocol["variants"] != list(VARIANTS)
                or protocol["rules"] != RULES or protocol["source_sha256"] != hashes()
                or protocol["entry_allowed"] is not False or ticker not in protocol["tickers"]):
            return empty
        paths = sorted(directory.glob(f"joint-stream-study-{phase}-*.json"), reverse=True)
        if not paths:
            return empty
        _, report = read(paths[0], 2_000_000)
        if (report["version"] != VERSION or report["phase"] != phase or report["entry_allowed"] is not False
                or report["protocol_sha256"] != hashlib.sha256(protocol_raw).hexdigest()
                or report["source_sha256"] != hashes() or report["session"] != protocol["session"]
                or report["tickers"] != protocol["tickers"] or set(report["outcomes"]) != set(VARIANTS)):
            return empty
        input_path = directory/report["input_file"]
        if input_path.resolve().parent != directory or input_path.stat().st_size > RULES["max_input_bytes"]:
            return empty
        checksum = hashlib.sha256()
        with input_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1_000_000), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != report["input_sha256"]:
            return empty
        if phase == "forward":
            # A future label needs evidence of the input window, not just a
            # filename or positive summary. Never relabel the replay as holdout.
            _, inputs = read(input_path, RULES["max_input_bytes"])
            start, end, assessed = (utc_timestamp(protocol[k]) for k in ("forward_start", "forward_end", "at"))
            if (not assessed < start < end or inputs["phase"] != phase
                    or inputs["version"] != VERSION or inputs["protocol_sha256"] != report["protocol_sha256"]
                    or utc_timestamp(inputs["window_start"]) != start or utc_timestamp(inputs["window_end"]) != end
                    or type(report["forward_complete"]) is not bool
                    or report["forward_complete"] != (utc_timestamp(report["at"]) >= end)):
                return empty
            for segment in inputs["segments"]:
                for name in ("rows", "extras"):
                    if any(not start <= utc_timestamp(r["received_at"]) <= min(end, utc_timestamp(report["at"])) for r in segment[name]):
                        return empty
        def valid_outcome(r):
            utc_timestamp(r["signal_at"])
            if r["status"] == "unverified":
                return bool(isinstance(r.get("reason"), str) and not any(k in r for k in ("return_pct", "stress_return_pct")))
            return r["status"] == "verified_quote_replay" and all(
                type(r[k]) in (int, float) and math.isfinite(r[k])
                for k in ("return_pct", "stress_return_pct", "gross_quote_return_pct"))
        outcomes, candidates = report["outcomes"], report["candidates"]
        if not isinstance(candidates, list) or len(candidates) > 10000:
            return empty
        for values in outcomes.values():
            if not isinstance(values, list) or len(values) > 10000 or any(
                    r["ticker"] not in protocol["tickers"] or not valid_outcome(r) for r in values):
                return empty
            if phase == "forward" and any(not start <= utc_timestamp(r["signal_at"]) < min(end, utc_timestamp(report["at"])) for r in values):
                return empty
        for c in candidates:
            if (c["ticker"] not in protocol["tickers"] or set(c["filters"]) != set(VARIANTS)
                    or c["filters"]["chart"] is not True or any(type(v) is not bool for v in c["filters"].values())
                    or not valid_outcome(c["outcome"] | {"signal_at": c["signal_at"]})):
                return empty
            if phase == "forward" and not start <= utc_timestamp(c["signal_at"]) < min(end, utc_timestamp(report["at"])):
                return empty
        if report["summaries"] != {k: summarize(v) for k, v in outcomes.items()}:
            return empty
        if report["matched"] != {k: matched(candidates, k) for k in VARIANTS[1:]}:
            return empty
        rows = []
        for key in VARIANTS:
            rows.append({"key": key, "name": NAMES[key],
                         "stock": summarize([r for r in outcomes[key] if r["ticker"] == ticker]),
                         "group": summarize(outcomes[key])})
        output = {**empty, "status": "ok", "ticker": ticker, "variants": rows, "note": NOTE,
                "at": report["at"], "coverage": report["coverage"].get(ticker),
                "message": "Три версии одного минутного пробоя на одинаковом архиве. Только исследование; прибыльность не подтверждена.",
                "matched": {k: matched([c for c in candidates if c["ticker"] == ticker], k) for k in VARIANTS[1:]},
                "forward": {"start": protocol["forward_start"], "end": protocol["forward_end"],
                            "message": "Правила зафиксированы до будущего окна. Новые наблюдения показаны отдельно ниже; сбор не продлевается автоматически."}}
        if phase == "forward":
            return {**output, "title": "Новые данные после фиксации правил",
                    "phase": phase, "window_complete": report["forward_complete"],
                    "message": "Промежуточная проверка новых наблюдений. Окно ещё не завершено; прибыльность не подтверждена." if not report["forward_complete"] else
                               "Окно новых наблюдений завершилось, но полнота данных и прибыльность требуют отдельной оценки.",
                    "forward": {**output["forward"], "message": "Результат получен по зафиксированным правилам, без автоматического продления сбора."}}
        # Keep the original research and later observations visibly separate.
        output["new_observations"] = load_joint_research(ticker, "forward")
        return output
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return empty
