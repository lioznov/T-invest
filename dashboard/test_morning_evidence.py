import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .morning_evidence import REPORT_NAME, load_replay, score_period
from .morning_model import digest


class MorningEvidenceTests(SimpleTestCase):
    def test_report_is_only_available_for_matching_artifact_and_payload(self):
        evidence = {"periods": {"test": {"samples": 133}}}
        report = {
            "artifact_sha256": "original",
            "evidence": evidence,
            "evidence_sha256": digest(evidence),
        }
        with TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            path = Path(folder) / "dashboard" / "ai_models" / REPORT_NAME
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(load_replay({"artifact_sha256": "original"}), evidence)
            self.assertIsNone(load_replay({"artifact_sha256": "different"}))
            evidence["periods"]["test"]["samples"] = 0
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertIsNone(load_replay({"artifact_sha256": "original"}))

    def test_errors_use_actual_price_and_dated_example(self):
        rows = [{
            "ticker": "SBER",
            "target_at": "2026-10-20T10:00:00+03:00",
            "actual_price": 200.0,
            "anchor_price": 160.0,
        }]
        with patch("dashboard.morning_evidence.model.forecast", return_value={"predicted_price": 190.0}):
            result = score_period(rows, {})
        self.assertEqual(result["mae_rub"], 10.0)
        self.assertEqual(result["mae_pct"], 5.0)
        self.assertEqual(result["baseline_mae_pct"], 20.0)
        self.assertEqual(result["example"]["error_pct"], 5.0)
        self.assertEqual(result["example"]["target_at"], rows[0]["target_at"])
