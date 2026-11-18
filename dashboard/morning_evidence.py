"""Read and reproduce the held-out replay for the exact bundled morning model."""

import json
from datetime import datetime
from math import isclose
from pathlib import Path
from statistics import mean

from django.conf import settings

from . import morning_model as model

REPORT_NAME = "morning-verified-replay-v1.json"


def load_replay(artifact):
    """Never attach the bundled results to a changed model."""
    path = Path(settings.BASE_DIR) / "dashboard" / "ai_models" / REPORT_NAME
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if (
            report["artifact_sha256"] == artifact["artifact_sha256"]
            and model.digest(report["evidence"]) == report["evidence_sha256"]
        ):
            return report["evidence"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def replay(artifact, directory):
    """Score the frozen model on the source-checked archive; no network I/O."""
    from .morning_inputs import load_history

    rows = []
    for ticker in artifact["tickers"]:
        history = load_history(directory, ticker)
        if history["sources"] != artifact["sources"][ticker]:
            raise ValueError(f"История {ticker} отличается от данных зафиксированной модели.")
        examples, _ = model.build_examples(history["candles"], ticker, history["instrument"]["lot"])
        rows.extend(examples)

    groups = model.split(rows)
    refit = model.fit(groups["train"], groups["calibration"])
    for key in ("center", "scale", "weights", "interval_abs_pct", "train_mean_pct"):
        observed, saved = refit[key], artifact["model"][key]
        pairs = zip(observed, saved, strict=True) if isinstance(observed, list) else [(observed, saved)]
        if not all(isclose(left, right, abs_tol=1e-10, rel_tol=1e-10) for left, right in pairs):
            raise ValueError("Зафиксированные параметры не воспроизвелись на обучающих данных.")
    last_label = max(
        datetime.fromisoformat(row["label_end"])
        for name in ("train", "calibration")
        for row in groups[name]
        if row["actual_price"] is not None
    )
    periods = {}
    for name in ("test", "later"):
        known = [row for row in groups[name] if row["actual_price"] is not None]
        if not known or min(datetime.fromisoformat(row["at"]) for row in known) <= last_label:
            raise ValueError("Границы независимой проверочной выборки не подтверждены.")
        periods[name] = score_period(known, artifact["model"])
    evidence = {
        "archive_data": "Сохранённые 15-минутные биржевые свечи Т-Банка по семи акциям",
        "percent_definition": "Средняя абсолютная ошибка в процентах от фактической цены",
        "limitations": "Эти даты следуют за обучением и настройкой диапазона. История ранее изучалась в проекте.",
        "periods": periods,
    }
    return {
        "artifact_sha256": artifact["artifact_sha256"],
        "evidence": evidence,
        "evidence_sha256": model.digest(evidence),
    }


def score_period(known, frozen_model):
    examples = sorted(known, key=lambda row: (row["target_at"], row["ticker"]))
    price_errors, pct_errors, baseline_errors, baseline_pct_errors = [], [], [], []
    example = None
    for row in examples:
        forecast = model.forecast(frozen_model, row)
        predicted, actual = forecast["predicted_price"], row["actual_price"]
        error = abs(predicted - actual)
        baseline_error = abs(row["anchor_price"] - actual)
        price_errors.append(error)
        pct_errors.append(100 * error / actual)
        baseline_errors.append(baseline_error)
        baseline_pct_errors.append(100 * baseline_error / actual)
        if row["ticker"] == "SBER" or example is None:
            example = {
                "ticker": row["ticker"],
                "target_at": row["target_at"],
                "predicted_price": predicted,
                "actual_price": actual,
                "error_rub": error,
                "error_pct": 100 * error / actual,
            }
    return {
        "samples": len(examples),
        "days": len({row["target_at"][:10] for row in examples}),
        "first_target_at": examples[0]["target_at"],
        "last_target_at": examples[-1]["target_at"],
        "mae_rub": mean(price_errors),
        "mae_pct": mean(pct_errors),
        "baseline_mae_rub": mean(baseline_errors),
        "baseline_mae_pct": mean(baseline_pct_errors),
        "example": example,
    }
