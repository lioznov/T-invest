"""Frozen external OHLCV overnight forecast validation (research only)."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np


DATASET = Path(__file__).resolve().parents[1] / "data" / "external-validation-us-stocks-2022-2024.zip"
FEATURES = (
    "intraday_return", "intraday_range", "return_1", "return_5", "return_20",
    "volatility_20", "close_vs_mean_20", "volume_vs_prior_mean_20",
)
SPLITS = (
    {"id": "A", "train_end": date(2022, 12, 31), "test_start": date(2023, 1, 1), "test_end": date(2023, 6, 30)},
    {"id": "B", "train_end": date(2023, 6, 30), "test_start": date(2023, 7, 1), "test_end": date(2023, 12, 31)},
    {"id": "C", "train_end": date(2023, 12, 31), "test_start": date(2024, 1, 1), "test_end": date(2024, 12, 31)},
)
HOLDOUT_TICKERS = frozenset(("AAPL", "AMZN", "MSFT", "TSLA"))
ALPHA = 10.0


def dataset_rows(path=DATASET):
    raw = Path(path).read_bytes()
    md5 = hashlib.md5(raw).hexdigest()
    if md5 != "e6ce23469860ef737c8a703a0b479174":
        raise ValueError("External archive checksum mismatch")
    result = defaultdict(list)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            ticker = Path(name).stem.upper()
            rows = list(csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig", newline="")))
            bars = []
            for row in rows:
                item = {"date": date.fromisoformat(row["date"])}
                for key in ("open", "high", "low", "close", "volume"):
                    item[key] = float(row[key])
                if not all(math.isfinite(item[k]) and item[k] > 0 for k in ("open", "high", "low", "close", "volume")):
                    continue
                bars.append(item)
            bars.sort(key=lambda x: x["date"])
            if any(a["date"] >= b["date"] for a, b in zip(bars, bars[1:])):
                raise ValueError(f"Duplicate or unordered date for {ticker}")
            result[ticker] = bars
    if len(result) != 16:
        raise ValueError("Unexpected ticker count")
    return result, md5, hashlib.sha256(raw).hexdigest()


def make_examples(dataset):
    by_ticker = {}
    for ticker, bars in dataset.items():
        examples = []
        closes = np.asarray([b["close"] for b in bars], dtype=float)
        volumes = np.asarray([b["volume"] for b in bars], dtype=float)
        returns = np.diff(np.log(closes), prepend=np.nan)
        for i in range(20, len(bars) - 1):
            hist_returns = returns[i - 19:i + 1]
            prior_volume = volumes[i - 20:i]
            if not np.isfinite(hist_returns).all() or not np.isfinite(prior_volume).all():
                continue
            b = bars[i]
            x = np.asarray([
                b["close"] / b["open"] - 1,
                (b["high"] - b["low"]) / b["close"],
                closes[i] / closes[i - 1] - 1,
                closes[i] / closes[i - 5] - 1,
                closes[i] / closes[i - 20] - 1,
                float(np.std(hist_returns, ddof=0)),
                closes[i] / float(np.mean(closes[i - 19:i + 1])) - 1,
                volumes[i] / float(np.mean(prior_volume)) - 1,
            ], dtype=float)
            y = bars[i + 1]["open"] / b["close"] - 1
            if np.isfinite(x).all() and math.isfinite(y):
                examples.append({"date": b["date"], "target_date": bars[i + 1]["date"], "x": x, "y": float(y)})
        by_ticker[ticker] = examples
    return by_ticker


def fit_ridge(train):
    x = np.vstack([row["x"] for row in train])
    y = np.asarray([row["y"] for row in train], dtype=float)
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-12] = 1.0
    z = (x - mean) / scale
    y_mean = float(y.mean())
    coef = np.linalg.solve(z.T @ z + ALPHA * np.eye(z.shape[1]), z.T @ (y - y_mean))
    return mean, scale, y_mean, coef


def predict(model, row):
    mean, scale, y_mean, coef = model
    return float(((row["x"] - mean) / scale) @ coef + y_mean)


def metrics(train, test):
    model = fit_ridge(train)
    pred = np.asarray([predict(model, r) for r in test])
    actual = np.asarray([r["y"] for r in test])
    train_sign = float(np.mean([r["y"] for r in train]))
    nonzero = actual != 0
    direction = float(np.mean(np.sign(pred[nonzero]) == np.sign(actual[nonzero]))) if nonzero.any() else None
    sign_baseline = float(np.mean(np.sign(actual[nonzero]) == (1 if train_sign >= 0 else -1))) if nonzero.any() else None
    per_ticker = {}
    cursor = 0
    grouped = defaultdict(list)
    for row, p in zip(test, pred):
        grouped[row["ticker"]].append((row["y"], p))
    for ticker, pairs in grouped.items():
        yy = np.asarray([v[0] for v in pairs]); pp = np.asarray([v[1] for v in pairs])
        per_ticker[ticker] = {
            "n": len(pairs),
            "mae_pct": float(np.mean(np.abs(pp - yy)) * 100),
            "naive_mae_pct": float(np.mean(np.abs(yy)) * 100),
            "rmse_pct": float(np.sqrt(np.mean((pp - yy) ** 2)) * 100),
            "naive_rmse_pct": float(np.sqrt(np.mean(yy ** 2)) * 100),
            "direction_pct": float(np.mean(np.sign(pp[yy != 0]) == np.sign(yy[yy != 0])) * 100) if np.any(yy != 0) else None,
        }
        cursor += len(pairs)
    return {
        "train_n": len(train), "test_n": len(test), "test_tickers": len(grouped),
        "pooled_mae_pct": float(np.mean(np.abs(pred - actual)) * 100),
        "pooled_naive_mae_pct": float(np.mean(np.abs(actual)) * 100),
        "pooled_rmse_pct": float(np.sqrt(np.mean((pred - actual) ** 2)) * 100),
        "pooled_naive_rmse_pct": float(np.sqrt(np.mean(actual ** 2)) * 100),
        "equal_ticker_mae_pct": float(np.mean([v["mae_pct"] for v in per_ticker.values()])),
        "equal_ticker_naive_mae_pct": float(np.mean([v["naive_mae_pct"] for v in per_ticker.values()])),
        "equal_ticker_rmse_pct": float(np.mean([v["rmse_pct"] for v in per_ticker.values()])),
        "equal_ticker_naive_rmse_pct": float(np.mean([v["naive_rmse_pct"] for v in per_ticker.values()])),
        "direction_pct": direction * 100 if direction is not None else None,
        "train_mean_sign_baseline_pct": sign_baseline * 100 if sign_baseline is not None else None,
        "per_ticker": per_ticker,
    }


def run(path=DATASET):
    dataset, md5, sha256 = dataset_rows(path)
    examples = make_examples(dataset)
    flat = []
    for ticker, rows in examples.items():
        flat.extend({**row, "ticker": ticker} for row in rows)
    folds = []
    for split in SPLITS:
        train = [r for r in flat if r["target_date"] <= split["train_end"]]
        test = [r for r in flat if split["test_start"] <= r["date"] <= split["test_end"]
                and r["target_date"] <= split["test_end"]]
        folds.append({"id": split["id"], "train_end": split["train_end"].isoformat(),
                      "test_start": split["test_start"].isoformat(), "test_end": split["test_end"].isoformat(),
                      **metrics(train, test)})
    train = [r for r in flat if r["ticker"] not in HOLDOUT_TICKERS and r["target_date"] <= date(2023, 12, 31)]
    test = [r for r in flat if r["ticker"] in HOLDOUT_TICKERS and date(2024, 1, 1) <= r["date"] <= date(2024, 12, 31)
            and r["target_date"] <= date(2024, 12, 31)]
    unseen = {"id": "unseen_tickers_2024", "heldout_tickers": sorted(HOLDOUT_TICKERS), **metrics(train, test)}
    return {"dataset_md5": md5, "dataset_sha256": sha256,
            "ticker_rows": {ticker: len(rows) for ticker, rows in dataset.items()},
            "valid_examples_by_ticker": {ticker: len(rows) for ticker, rows in examples.items()},
            "feature_names": FEATURES, "ridge_alpha": ALPHA,
            "folds": folds, "unseen_ticker_transfer": unseen}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2, sort_keys=True))
